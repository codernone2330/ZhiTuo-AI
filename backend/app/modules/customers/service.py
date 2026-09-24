import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.modules.auth.dependencies import IdentityContext
from app.modules.customers.audit import (
    customer_timeline,
    record_customer_event,
    record_score_event,
)
from app.modules.customers.models import Customer, CustomerImportBatch
from app.modules.customers.schemas import CustomerImportRequest, CustomerImportRow, CustomerUpdate
from app.modules.organizations.models import Organization
from app.modules.users.models import User


def normalize_customer_name(value: str) -> str:
    return re.sub(r"[\s（）()·\-]", "", value).lower()


def _bool_value(value: bool | str) -> bool:
    return value is True or str(value).strip() in {"是", "true", "True", "1"}


def _scope_condition(identity: IdentityContext):
    if identity.is_super_admin or identity.is_group_admin:
        return None
    if identity.is_org_admin:
        return (Organization.path == identity.organization.path) | Organization.path.startswith(
            identity.organization.path + "/"
        )
    if "department_manager" in identity.role_codes:
        return Customer.organization_id == identity.organization.id
    if "customer_manager" in identity.role_codes:
        return (Customer.owner_user_id == identity.user.id) | (
            (Customer.owner_user_id.is_(None))
            & (Customer.organization_id == identity.organization.id)
            & (Customer.owner_name == identity.user.display_name)
        )
    return Customer.organization_id == identity.organization.id


def _assert_customer_organization(
    session: Session, identity: IdentityContext, organization_id: uuid.UUID
) -> Organization:
    organization = session.get(Organization, organization_id)
    if organization is None or not organization.is_active:
        raise AppError(CommonErrorCode.NOT_FOUND, "客户归属组织不存在或已停用", 404)
    if organization.level != "department" or not organization.code.endswith("-enterprise"):
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "政企客户必须归属市级或区县集客部", 400)
    if not identity.organization_in_scope(organization):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权向该组织导入客户", 403)
    return organization


def _can_import(identity: IdentityContext) -> bool:
    return bool(
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
        or "customer_manager" in identity.role_codes
    )


def _customer_statement(identity: IdentityContext):
    statement = (
        select(Customer)
        .join(Organization, Customer.organization_id == Organization.id)
        .where(Customer.deleted_at.is_(None))
    )
    condition = _scope_condition(identity)
    return statement.where(condition) if condition is not None else statement


def list_customers(
    session: Session,
    identity: IdentityContext,
    page: int,
    page_size: int,
    search: str | None,
    stage: str | None,
    kind: str | None,
    organization_id: uuid.UUID | None,
    include_descendants: bool,
) -> tuple[list[dict], int]:
    statement = _customer_statement(identity)
    if organization_id:
        organization = session.get(Organization, organization_id)
        if organization is None or not identity.organization_in_scope(organization):
            raise AppError(CommonErrorCode.FORBIDDEN, "无权查看该组织客户", 403)
        if include_descendants:
            statement = statement.where(
                (Organization.path == organization.path)
                | Organization.path.startswith(organization.path + "/")
            )
        else:
            statement = statement.where(Customer.organization_id == organization.id)
    if search:
        keyword = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                Customer.name.ilike(keyword),
                Customer.industry.ilike(keyword),
                Customer.contact_name.ilike(keyword),
                Customer.contact_phone.ilike(keyword),
                Customer.need.ilike(keyword),
                Customer.owner_name.ilike(keyword),
            )
        )
    if stage:
        statement = statement.where(Customer.stage == stage)
    if kind:
        statement = statement.where(Customer.kind == kind)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    records = session.scalars(
        statement.order_by(Customer.score.desc(), Customer.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    organizations = {
        item.id: item
        for item in session.scalars(
            select(Organization).where(
                Organization.id.in_({record.organization_id for record in records})
            )
        ).all()
    }
    items = [
        serialize_customer(item, organizations.get(item.organization_id), True)
        for item in records
    ]
    return items, total


def get_customer(session: Session, identity: IdentityContext, customer_ref: str) -> dict:
    statement = _customer_statement(identity)
    try:
        customer_id = uuid.UUID(customer_ref)
        statement = statement.where(
            (Customer.id == customer_id) | (Customer.external_id == customer_ref)
        )
    except ValueError:
        statement = statement.where(Customer.external_id == customer_ref)
    customer = session.scalar(statement)
    if customer is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "客户不存在或不在授权范围内", 404)
    organization = session.get(Organization, customer.organization_id)
    data = serialize_customer(customer, organization, include_extra=True)
    data["auditEvents"] = customer_timeline(session, customer)
    return data


def update_customer(
    session: Session, identity: IdentityContext, customer_ref: str, payload: CustomerUpdate
) -> dict:
    if not _can_import(identity) and "customer_manager" not in identity.role_codes:
        raise AppError(CommonErrorCode.FORBIDDEN, "当前角色不能编辑客户", 403)
    customer = session.scalar(
        _customer_statement(identity).where(Customer.external_id == customer_ref)
    )
    if customer is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "客户不存在或不在授权范围内", 404)
    if payload.version != customer.version:
        raise AppError(CommonErrorCode.CONFLICT, "客户已被其他人修改，请刷新后重试", 409)
    if payload.stage not in {"线索入池", "已联系", "已拜访", "方案沟通", "商机立项", "已成交"}:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "经营阶段不正确", 400)
    normalized = normalize_customer_name(payload.name)
    conditions = [
        (Customer.organization_id == customer.organization_id)
        & (Customer.normalized_name == normalized),
    ]
    if payload.phone:
        conditions.append(
            (Customer.organization_id == customer.organization_id)
            & (Customer.contact_phone == payload.phone)
        )
    duplicate = session.scalar(
        select(Customer.id).where(Customer.id != customer.id, or_(*conditions))
    )
    if duplicate:
        raise AppError(CommonErrorCode.CONFLICT, "企业名称或电话与现有客户重复", 409)
    if payload.stage != customer.stage and not payload.stageReason.strip():
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "经营阶段变化必须填写流转说明", 400)
    before = {
        "name": customer.name, "industry": customer.industry,
        "contact": customer.contact_name, "phone": customer.contact_phone,
        "need": customer.need, "stage": customer.stage,
        "potential": customer.potential, "score": customer.score,
        "nextAction": customer.next_action,
        "isQianBaiWanGroup": customer.is_qian_bai_wan_group,
        "isKeyAccount": customer.is_key_account,
    }
    old_stage = customer.stage
    old_reasons = list((customer.extra_data or {}).get("reasons") or [])
    now = datetime.now(timezone.utc)
    extra_data = dict(customer.extra_data or {})
    if payload.stage != old_stage:
        history = list(extra_data.get("stageHistory") or [])
        history.append({
            "id": f"stage-edit-{uuid.uuid4().hex}", "time": now.isoformat(),
            "fromStage": old_stage, "toStage": payload.stage,
            "operator": identity.user.display_name, "owner": customer.owner_name,
            "source": "CRM手工更新", "note": payload.stageReason.strip(),
        })
        extra_data["stageHistory"] = history
    result = session.execute(
        update(Customer)
        .where(Customer.id == customer.id, Customer.version == payload.version)
        .values(
            name=payload.name.strip(),
            normalized_name=normalized,
            industry=payload.industry or "待分类",
            contact_name=payload.contact,
            contact_phone=payload.phone,
            need=payload.need,
            stage=payload.stage,
            potential=payload.potential,
            score=customer.score,
            next_action=payload.nextAction,
            is_qian_bai_wan_group=_bool_value(payload.isQianBaiWanGroup),
            is_key_account=_bool_value(payload.isKeyAccount),
            extra_data=extra_data,
            version=Customer.version + 1,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise AppError(CommonErrorCode.CONFLICT, "客户已被其他人修改，请刷新后重试", 409)
    session.refresh(customer)
    from app.modules.opportunities.service import _reasons, _score

    organization = session.get(Organization, customer.organization_id)
    customer.score = _score(customer)
    customer.extra_data = {**extra_data, "reasons": _reasons(customer, organization)}
    after = {
        "name": customer.name, "industry": customer.industry,
        "contact": customer.contact_name, "phone": customer.contact_phone,
        "need": customer.need, "stage": customer.stage,
        "potential": customer.potential, "score": customer.score,
        "nextAction": customer.next_action,
        "isQianBaiWanGroup": customer.is_qian_bai_wan_group,
        "isKeyAccount": customer.is_key_account,
    }
    if before != after:
        record_customer_event(session, customer, identity.user, "customer_edited", before, after,
                              payload.stageReason.strip() or None, at=now)
    if old_stage != customer.stage:
        record_customer_event(session, customer, identity.user, "stage_changed",
                              {"stage": old_stage}, {"stage": customer.stage},
                              payload.stageReason.strip(), at=now)
    record_score_event(
        session, customer, identity.user, before["score"], old_reasons,
        "CRM 客户资料编辑", at=now,
    )
    if payload.ownershipRequest:
        from app.modules.customers.requests import create_request
        from app.modules.customers.schemas import CustomerRequestCreate

        request_data = payload.ownershipRequest
        create_request(
            session, identity, customer_ref,
            CustomerRequestCreate(
                kind="ownership", reason=request_data.reason,
                targetOrganizationId=request_data.targetOrganizationId,
                targetOwnerName=request_data.targetOwnerName,
            ),
            customer=customer, commit=False,
        )
    session.commit()
    session.refresh(customer)
    return serialize_customer(customer, organization, True)


def _external_id(row: CustomerImportRow) -> str:
    return row.externalId or f"C-{uuid.uuid4().hex[:12].upper()}"


def import_customers(
    session: Session, identity: IdentityContext, payload: CustomerImportRequest
) -> dict:
    if not _can_import(identity):
        raise AppError(CommonErrorCode.FORBIDDEN, "当前角色不能导入客户", 403)
    batch = CustomerImportBatch(
        imported_by=identity.user.id,
        source_name=payload.sourceName,
        total_rows=len(payload.rows),
    )
    session.add(batch)
    session.flush()
    inserted = duplicate = rejected = 0
    duplicate_items: list[dict] = []
    organization_cache: dict[uuid.UUID, Organization] = {}
    for index, row in enumerate(payload.rows, start=1):
        try:
            organization = organization_cache.get(row.organizationId)
            if organization is None:
                organization = _assert_customer_organization(
                    session, identity, row.organizationId
                )
                organization_cache[row.organizationId] = organization
            if (
                "customer_manager" in identity.role_codes
                and row.ownerName != identity.user.display_name
            ):
                raise AppError(CommonErrorCode.FORBIDDEN, "客户经理只能导入本人负责的客户", 403)
            normalized = normalize_customer_name(row.name)
            external_id = _external_id(row)
            duplicate_conditions = [
                Customer.external_id == external_id,
                (Customer.organization_id == organization.id)
                & (Customer.normalized_name == normalized),
            ]
            if row.phone:
                duplicate_conditions.append(
                    (Customer.contact_phone == row.phone)
                    & (Customer.organization_id == organization.id)
                )
            duplicate_customer = session.scalar(
                select(Customer).where(or_(*duplicate_conditions))
            )
            if duplicate_customer:
                duplicate += 1
                duplicate_items.append(
                    {"row": index, "name": row.name, "existingId": duplicate_customer.external_id}
                )
                continue
            owner = session.scalar(
                select(User).where(
                    User.organization_id == organization.id,
                    User.display_name == (row.ownerName or ""),
                    User.is_active.is_(True),
                )
            )
            customer = Customer(
                external_id=external_id,
                name=row.name.strip(),
                normalized_name=normalized,
                organization_id=organization.id,
                owner_user_id=owner.id if owner else None,
                owner_name=row.ownerName or (owner.display_name if owner else "待分配"),
                kind=row.kind,
                customer_type=row.customerType,
                carrier=row.carrier,
                industry=row.industry,
                province=row.province or organization.province,
                city=row.city or organization.city,
                district=row.area or organization.district,
                address=row.address,
                contact_name=row.contact,
                contact_phone=row.phone,
                need=row.need,
                stage=row.stage,
                potential=row.potential,
                score=row.score,
                next_action=row.nextAction,
                is_qian_bai_wan_group=_bool_value(row.isQianBaiWanGroup),
                is_key_account=_bool_value(row.isKeyAccount),
                source=row.source,
                import_batch_id=batch.id,
                imported_by=identity.user.id,
                source_created_at=row.createdAt,
                extra_data={
                    "reasons": row.reasons,
                    "tags": row.tags,
                    "sources": row.sources,
                    "lastContact": row.lastContact.isoformat() if row.lastContact else None,
                    "stageHistory": row.stageHistory,
                    "crmFlow": row.crmFlow,
                    "assignmentReason": row.assignmentReason,
                    "assignmentConfidence": row.assignmentConfidence,
                },
            )
            session.add(customer)
            session.flush()
            record_score_event(
                session, customer, identity.user, None, None,
                f"客户导入：{payload.sourceName}",
            )
            inserted += 1
        except AppError:
            raise
        except Exception as exc:
            rejected += 1
            session.rollback()
            raise AppError(
                CommonErrorCode.INVALID_ARGUMENT,
                f"第 {index} 行导入失败",
                400,
                str(exc),
            ) from exc
    batch.inserted_rows = inserted
    batch.duplicate_rows = duplicate
    batch.rejected_rows = rejected
    batch.status = "completed"
    session.commit()
    return {
        "batchId": str(batch.id),
        "total": len(payload.rows),
        "inserted": inserted,
        "duplicates": duplicate,
        "rejected": rejected,
        "duplicateItems": duplicate_items[:100],
    }


def serialize_customer(
    customer: Customer, organization: Organization | None, include_extra: bool = False
) -> dict:
    data = {
        "id": customer.external_id,
        "backendId": str(customer.id),
        "externalId": customer.external_id,
        "version": customer.version,
        "name": customer.name,
        "organizationId": str(customer.organization_id),
        "organization": {
            "id": str(organization.id) if organization else str(customer.organization_id),
            "code": organization.code if organization else None,
            "name": organization.name if organization else None,
            "path": organization.path if organization else None,
        },
        "ownerName": customer.owner_name,
        "ownerUserId": str(customer.owner_user_id) if customer.owner_user_id else None,
        "kind": customer.kind,
        "customerType": customer.customer_type,
        "carrier": customer.carrier,
        "industry": customer.industry,
        "province": customer.province,
        "city": customer.city,
        "area": customer.district,
        "address": customer.address,
        "contact": customer.contact_name,
        "phone": customer.contact_phone,
        "need": customer.need,
        "stage": customer.stage,
        "potential": customer.potential,
        "score": customer.score,
        "nextAction": customer.next_action,
        "isQianBaiWanGroup": "是" if customer.is_qian_bai_wan_group else "否",
        "isKeyAccount": "是" if customer.is_key_account else "否",
        "source": customer.source,
        "createdAt": (customer.source_created_at or customer.created_at).isoformat(),
        "updatedAt": customer.updated_at.isoformat(),
    }
    if include_extra:
        data["extraData"] = customer.extra_data or {}
    return data
