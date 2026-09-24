import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.modules.auth.dependencies import IdentityContext
from app.modules.customers.audit import record_customer_event, record_score_event
from app.modules.customers.models import Customer, CustomerEvent
from app.modules.customers.service import _scope_condition
from app.modules.opportunities.service import _reasons, _score
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from app.modules.visits.models import Visit
from app.modules.visits.schemas import (
    VisitCancel,
    VisitComplete,
    VisitCreate,
    VisitImportRequest,
    VisitUpdate,
)

STAGES = ("线索入池", "已联系", "已拜访", "方案沟通", "商机立项", "已成交")


def _visit_statement(identity: IdentityContext):
    statement = (
        select(Visit)
        .join(Customer, Visit.customer_id == Customer.id)
        .join(Organization, Customer.organization_id == Organization.id)
        .where(Customer.deleted_at.is_(None))
    )
    condition = _scope_condition(identity)
    if condition is not None:
        statement = statement.where(condition)
    if "customer_manager" in identity.role_codes:
        statement = statement.where(Visit.owner_name == identity.user.display_name)
    return statement


def _editable(identity: IdentityContext) -> None:
    if not (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
        or "customer_manager" in identity.role_codes
    ):
        raise AppError(CommonErrorCode.FORBIDDEN, "当前角色不能管理拜访任务", 403)


def _customer(
    session: Session, identity: IdentityContext, ref: str, lock: bool = False
) -> Customer:
    from app.modules.customers.service import _customer_statement

    statement = _customer_statement(identity).where(Customer.external_id == ref)
    customer = session.scalar(statement.with_for_update(of=Customer) if lock else statement)
    if customer is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "客户不存在或不在授权范围内", 404)
    return customer


def _owner(session: Session, identity: IdentityContext, customer: Customer, name: str):
    if "customer_manager" in identity.role_codes and name != identity.user.display_name:
        raise AppError(CommonErrorCode.FORBIDDEN, "客户经理只能处理本人任务", 403)
    owner = session.scalar(
        select(User).where(
            User.organization_id == customer.organization_id,
            User.display_name == name,
            User.is_active.is_(True),
        )
    )
    return owner.id if owner else None


def _task(session: Session, identity: IdentityContext, ref: str, lock: bool = False) -> Visit:
    statement = _visit_statement(identity).where(Visit.external_id == ref)
    task = session.scalar(statement.with_for_update() if lock else statement)
    if task is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "任务不存在或不在授权范围内", 404)
    return task


def _open(task: Visit, version: int) -> None:
    if task.version != version:
        raise AppError(CommonErrorCode.CONFLICT, "任务已被其他人修改，请刷新后重试", 409)
    if task.status != "pending":
        raise AppError(CommonErrorCode.CONFLICT, "任务已结束，不能再次修改", 409)


def _future(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "拜访时间必须包含时区", 400)
    if value < datetime.now(timezone.utc):
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "拜访时间不能早于当前时间", 400)
    return value


def serialize_visit(task: Visit, customer: Customer) -> dict:
    extra = task.extra_data or {}
    return {
        "id": task.external_id,
        "backendId": str(task.id),
        "customerId": customer.external_id,
        "owner": task.owner_name,
        "time": task.scheduled_at.isoformat(),
        "method": task.method,
        "purpose": task.purpose,
        "status": task.status,
        "notes": task.notes,
        "stage": task.stage,
        "source": task.source,
        "createdAt": task.created_at.isoformat(),
        "createdBy": task.created_by,
        "updatedAt": task.updated_at.isoformat(),
        "updatedBy": task.updated_by,
        "canceledAt": task.canceled_at.isoformat() if task.canceled_at else None,
        "canceledBy": task.canceled_by,
        "cancelReason": task.cancel_reason,
        "completedAt": task.completed_at.isoformat() if task.completed_at else None,
        "outcome": task.outcome,
        "nextAction": task.next_action,
        "dealProduct": task.deal_product or "",
        "dealAmount": float(task.deal_amount or 0),
        "dealRemark": task.deal_remark or "",
        "overrideReason": task.override_reason or "",
        "conflictTaskId": task.conflict_task_id or "",
        "version": task.version,
        "aiStructured": extra.get("aiStructured"),
        "simulated": bool(extra.get("simulated")),
        "opportunityScoreAtPlan": extra.get("opportunityScoreAtPlan"),
        "opportunityReasonsAtPlan": extra.get("opportunityReasonsAtPlan") or [],
        "opportunityStageAtPlan": extra.get("opportunityStageAtPlan"),
        "opportunityScoreEventIdAtPlan": extra.get("opportunityScoreEventIdAtPlan"),
        "scoreSnapshotProvenance": extra.get("scoreSnapshotProvenance"),
    }


def list_visits(
    session: Session, identity: IdentityContext, page: int, page_size: int
) -> tuple[list[dict], int]:
    statement = _visit_statement(identity)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    tasks = session.scalars(
        statement.order_by(Visit.scheduled_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    customers = {
        item.id: item
        for item in session.scalars(
            select(Customer).where(Customer.id.in_({task.customer_id for task in tasks}))
        ).all()
    }
    return [serialize_visit(task, customers[task.customer_id]) for task in tasks], total


def get_visit(session: Session, identity: IdentityContext, ref: str) -> dict:
    task = _task(session, identity, ref)
    return serialize_visit(task, session.get(Customer, task.customer_id))


def create_visit(session: Session, identity: IdentityContext, payload: VisitCreate) -> dict:
    _editable(identity)
    customer = _customer(session, identity, payload.customerId, lock=True)
    owner_id = _owner(session, identity, customer, payload.ownerName)
    conflict = session.scalar(
        select(Visit).where(Visit.customer_id == customer.id, Visit.status == "pending")
    )
    if conflict and len(payload.overrideReason.strip()) < 5:
        raise AppError(
            CommonErrorCode.CONFLICT,
            "该客户已有未完成任务，重复创建需填写至少 5 字原因",
            409,
        )
    score_event = session.scalar(
        select(CustomerEvent)
        .where(
            CustomerEvent.customer_id == customer.id,
            CustomerEvent.action.in_(("score_baseline", "score_updated")),
        )
        .order_by(CustomerEvent.created_at.desc(), CustomerEvent.id.desc())
        .limit(1)
    )
    task = Visit(
        external_id=f"v-{uuid.uuid4().hex[:16]}",
        customer_id=customer.id,
        owner_user_id=owner_id,
        owner_name=payload.ownerName,
        scheduled_at=_future(payload.time),
        method=payload.method,
        purpose=payload.purpose.strip(),
        status="pending",
        notes=payload.notes,
        stage=customer.stage,
        source=payload.source,
        created_by=identity.user.display_name,
        override_reason=payload.overrideReason.strip() or None,
        conflict_task_id=conflict.external_id if conflict else None,
        deal_amount=Decimal("0"),
        extra_data={
            "opportunityScoreAtPlan": customer.score,
            "opportunityReasonsAtPlan": list(
                (customer.extra_data or {}).get("reasons")
                or _reasons(customer, session.get(Organization, customer.organization_id))
            ),
            "opportunityStageAtPlan": customer.stage,
            "opportunityScoreEventIdAtPlan": str(score_event.id) if score_event else None,
            "scoreSnapshotProvenance": "server_plan_snapshot",
        },
    )
    customer.next_action = payload.purpose.strip()
    customer.version += 1
    session.add(task)
    record_customer_event(
        session, customer, identity.user, "visit_planned", {},
        {
            "visitId": task.external_id, "time": task.scheduled_at.isoformat(),
            "owner": task.owner_name, "purpose": task.purpose,
            "opportunityScoreAtPlan": customer.score,
            "opportunityScoreEventIdAtPlan": str(score_event.id) if score_event else None,
        },
        task.source,
    )
    session.commit()
    session.refresh(task)
    return serialize_visit(task, customer)


def update_visit(
    session: Session, identity: IdentityContext, ref: str, payload: VisitUpdate
) -> dict:
    _editable(identity)
    task = _task(session, identity, ref, lock=True)
    _open(task, payload.version)
    customer = session.get(Customer, task.customer_id)
    before = {
        "visitId": task.external_id, "time": task.scheduled_at.isoformat(),
        "owner": task.owner_name, "purpose": task.purpose,
    }
    task.owner_user_id = _owner(session, identity, customer, payload.ownerName)
    task.owner_name = payload.ownerName
    task.scheduled_at = _future(payload.time)
    task.method = payload.method
    task.purpose = payload.purpose.strip()
    task.notes = payload.notes
    task.updated_by = identity.user.display_name
    task.version += 1
    customer.next_action = task.purpose
    customer.version += 1
    record_customer_event(
        session, customer, identity.user, "visit_updated", before,
        {
            "visitId": task.external_id, "time": task.scheduled_at.isoformat(),
            "owner": task.owner_name, "purpose": task.purpose,
        },
        "拜访计划调整",
    )
    session.commit()
    session.refresh(task)
    return serialize_visit(task, customer)


def cancel_visit(
    session: Session, identity: IdentityContext, ref: str, payload: VisitCancel
) -> dict:
    _editable(identity)
    task = _task(session, identity, ref, lock=True)
    _open(task, payload.version)
    task.status = "canceled"
    task.canceled_at = datetime.now(timezone.utc)
    task.canceled_by = identity.user.display_name
    task.cancel_reason = payload.reason.strip()
    task.version += 1
    record_customer_event(
        session, session.get(Customer, task.customer_id), identity.user,
        "visit_canceled", {"visitId": task.external_id, "status": "pending"},
        {"visitId": task.external_id, "status": "canceled"},
        task.cancel_reason, at=task.canceled_at,
    )
    session.commit()
    session.refresh(task)
    return serialize_visit(task, session.get(Customer, task.customer_id))


def complete_visit(
    session: Session, identity: IdentityContext, ref: str, payload: VisitComplete
) -> dict:
    _editable(identity)
    task = _task(session, identity, ref, lock=True)
    _open(task, payload.version)
    customer = session.get(Customer, task.customer_id)
    now = datetime.now(timezone.utc)
    is_deal = payload.outcome == "已达成合作" or payload.stage == "已成交"
    stage = "已成交" if is_deal else payload.stage
    old_stage = customer.stage
    old_score = customer.score
    old_reasons = list((customer.extra_data or {}).get("reasons") or [])
    task.status = "completed"
    task.completed_at = now
    task.outcome = "已达成合作" if is_deal else payload.outcome
    task.stage = stage
    task.notes = payload.notes
    task.next_action = payload.nextAction
    task.deal_product = payload.dealProduct.strip() if is_deal else ""
    task.deal_amount = payload.dealAmount if is_deal else Decimal("0")
    task.deal_remark = payload.dealRemark if is_deal else ""
    task.extra_data = {**(task.extra_data or {}), "aiStructured": payload.aiStructured}
    task.version += 1
    extra = dict(customer.extra_data or {})
    history = list(extra.get("stageHistory") or [])
    history.append(
        {
            "id": f"stage-visit-{task.external_id}",
            "time": now.isoformat(),
            "fromStage": customer.stage,
            "toStage": stage,
            "operator": identity.user.display_name,
            "owner": task.owner_name,
            "source": "拜访结果回填",
            "note": payload.notes,
        }
    )
    extra["stageHistory"] = history
    extra["lastContact"] = now.isoformat()
    if payload.aiStructured and payload.aiStructured.get("need"):
        customer.need = str(payload.aiStructured["need"])[:2000]
    customer.stage = stage
    customer.next_action = payload.nextAction or "待制定下一步"
    if is_deal:
        customer.potential = "高"
    elif payload.outcome == "形成明确商机" and customer.potential == "低":
        customer.potential = "中"
    customer.extra_data = extra
    customer.score = _score(customer)
    customer.extra_data = {
        **extra,
        "reasons": _reasons(customer, session.get(Organization, customer.organization_id)),
    }
    customer.version += 1
    if old_stage != customer.stage:
        record_customer_event(
            session, customer, identity.user, "stage_changed",
            {"stage": old_stage}, {"stage": customer.stage},
            f"拜访结果回填：{task.external_id}", at=now,
        )
    record_customer_event(
        session, customer, identity.user, "visit_completed",
        {"visitId": task.external_id, "status": "pending"},
        {
            "visitId": task.external_id, "status": "completed",
            "outcome": task.outcome, "stage": customer.stage,
            "notes": task.notes,
        },
        task.outcome, at=now,
    )
    if is_deal:
        record_customer_event(
            session, customer, identity.user, "deal_recorded", {},
            {
                "visitId": task.external_id, "product": task.deal_product,
                "amount": str(task.deal_amount), "remark": task.deal_remark,
            },
            "拜访结果确认成交", at=now,
        )
    record_score_event(
        session, customer, identity.user, old_score, old_reasons,
        f"拜访结果回填：{task.external_id}", at=now,
    )
    session.commit()
    session.refresh(task)
    return {"visit": serialize_visit(task, customer), "customerVersion": customer.version}


def import_visits(session: Session, identity: IdentityContext, payload: VisitImportRequest) -> dict:
    if not (identity.is_super_admin or identity.is_group_admin):
        raise AppError(CommonErrorCode.FORBIDDEN, "仅集团管理员可迁移原有拜访记录", 403)
    refs = {row.customerId for row in payload.rows}
    customers = {
        item.external_id: item
        for item in session.scalars(select(Customer).where(Customer.external_id.in_(refs))).all()
    }
    existing = set(
        session.scalars(
            select(Visit.external_id).where(
                Visit.external_id.in_({row.externalId for row in payload.rows})
            )
        ).all()
    )
    inserted = duplicates = skipped = 0
    for row in payload.rows:
        if row.externalId in existing:
            duplicates += 1
            continue
        customer = customers.get(row.customerId)
        if customer is None or customer.deleted_at is not None:
            skipped += 1
            continue
        owner_id = _owner(session, identity, customer, row.ownerName)
        task = Visit(
            external_id=row.externalId,
            customer_id=customer.id,
            owner_user_id=owner_id,
            owner_name=row.ownerName,
            scheduled_at=row.time,
            method=row.method,
            purpose=row.purpose,
            status=row.status,
            notes=row.notes,
            stage=row.stage,
            source=row.source,
            created_by=row.createdBy,
            canceled_at=row.canceledAt,
            canceled_by=row.canceledBy,
            cancel_reason=row.cancelReason,
            completed_at=row.completedAt,
            outcome=row.outcome,
            next_action=row.nextAction,
            deal_product=row.dealProduct,
            deal_amount=row.dealAmount,
            deal_remark=row.dealRemark,
            override_reason=row.overrideReason,
            extra_data={
                "aiStructured": row.aiStructured,
                "simulated": row.simulated,
                "scoreSnapshotProvenance": "legacy_import_unavailable",
            },
        )
        if row.createdAt:
            task.created_at = row.createdAt
        session.add(task)
        existing.add(row.externalId)
        inserted += 1
    session.commit()
    return {"inserted": inserted, "duplicates": duplicates, "skipped": skipped}
