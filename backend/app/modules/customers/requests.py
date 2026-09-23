import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.modules.auth.dependencies import IdentityContext
from app.modules.customers.models import Customer, CustomerRequest
from app.modules.customers.schemas import CustomerRequestCreate, CustomerRequestDecision
from app.modules.customers.service import (
    _assert_customer_organization,
    _customer_statement,
)
from app.modules.organizations.models import Organization
from app.modules.users.models import User


def _customer_for_request(session: Session, identity: IdentityContext, ref: str) -> Customer:
    customer = session.scalar(
        _customer_statement(identity).where(Customer.external_id == ref)
    )
    if customer is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "客户不存在或不在授权范围内", 404)
    return customer


def _can_approve(identity: IdentityContext, request: CustomerRequest) -> bool:
    return bool(
        request.requester_id != identity.user.id
        and request.approver_org_id == identity.organization.id
        and (
            identity.is_super_admin
            or identity.is_group_admin
            or identity.is_org_admin
            or "department_manager" in identity.role_codes
        )
    )


def serialize_request(session: Session, request: CustomerRequest) -> dict:
    customer = session.get(Customer, request.customer_id)
    requester = session.get(User, request.requester_id)
    reviewer = session.get(User, request.reviewer_id) if request.reviewer_id else None
    requester_org = session.get(Organization, request.requester_org_id)
    approver_org = session.get(Organization, request.approver_org_id)
    before = request.before_data or {}
    after = request.after_data or {}
    before_org = (
        session.get(Organization, uuid.UUID(before["organizationId"]))
        if before.get("organizationId")
        else None
    )
    after_org = (
        session.get(Organization, uuid.UUID(after["organizationId"]))
        if after.get("organizationId")
        else None
    )
    return {
        "id": str(request.id),
        "kind": request.kind,
        "customerId": customer.external_id if customer else None,
        "customerName": customer.name if customer else "客户已移除",
        "requesterId": str(request.requester_id),
        "requesterName": requester.display_name if requester else "未知申请人",
        "requesterOrgId": str(request.requester_org_id),
        "requesterOrgCode": requester_org.code if requester_org else None,
        "approverOrgId": str(request.approver_org_id),
        "approverOrgCode": approver_org.code if approver_org else None,
        "reason": request.reason,
        "status": request.status,
        "before": {**before, "organizationCode": before_org.code if before_org else None},
        "after": {**after, "organizationCode": after_org.code if after_org else None},
        "createdAt": request.created_at.isoformat(),
        "reviewedAt": request.reviewed_at.isoformat() if request.reviewed_at else None,
        "reviewerId": str(request.reviewer_id) if request.reviewer_id else None,
        "reviewerName": reviewer.display_name if reviewer else None,
        "reviewComment": request.review_comment,
    }


def create_request(
    session: Session,
    identity: IdentityContext,
    customer_ref: str,
    payload: CustomerRequestCreate,
) -> dict:
    if not (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
        or "customer_manager" in identity.role_codes
    ):
        raise AppError(CommonErrorCode.FORBIDDEN, "当前角色不能申请客户变更", 403)
    customer = _customer_for_request(session, identity, customer_ref)
    pending = session.scalar(
        select(CustomerRequest.id).where(
            CustomerRequest.customer_id == customer.id,
            CustomerRequest.status == "pending",
        )
    )
    if pending:
        raise AppError(CommonErrorCode.CONFLICT, "该客户已有待审批申请", 409)
    if payload.kind == "ownership":
        if not payload.targetOrganizationId or not payload.targetOwnerName:
            raise AppError(CommonErrorCode.INVALID_ARGUMENT, "请填写目标集客部与客户经理", 400)
        target = _assert_customer_organization(session, identity, payload.targetOrganizationId)
        if target.id == customer.organization_id and payload.targetOwnerName == customer.owner_name:
            raise AppError(CommonErrorCode.INVALID_ARGUMENT, "客户归属未发生变化", 400)
        after = {
            "organizationId": str(target.id),
            "ownerName": payload.targetOwnerName,
        }
    else:
        after = {}
    approver_org_id = (
        identity.organization.id
        if "customer_manager" in identity.role_codes
        else identity.organization.parent_id or identity.organization.id
    )
    request = CustomerRequest(
        customer_id=customer.id,
        kind=payload.kind,
        requester_id=identity.user.id,
        requester_org_id=identity.organization.id,
        approver_org_id=approver_org_id,
        reason=payload.reason.strip(),
        status="pending",
        before_data={
            "organizationId": str(customer.organization_id),
            "ownerName": customer.owner_name,
        },
        after_data=after,
    )
    session.add(request)
    session.commit()
    session.refresh(request)
    return serialize_request(session, request)


def list_requests(session: Session, identity: IdentityContext) -> list[dict]:
    criteria = [CustomerRequest.requester_id == identity.user.id]
    if (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
    ):
        criteria.append(CustomerRequest.approver_org_id == identity.organization.id)
    requests = session.scalars(
        select(CustomerRequest)
        .where(or_(*criteria))
        .order_by(CustomerRequest.created_at.desc())
        .limit(500)
    ).all()
    return [serialize_request(session, request) for request in requests]


def decide_request(
    session: Session,
    identity: IdentityContext,
    request_id: uuid.UUID,
    payload: CustomerRequestDecision,
) -> dict:
    request = session.scalar(
        select(CustomerRequest).where(CustomerRequest.id == request_id).with_for_update()
    )
    if request is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "审批申请不存在", 404)
    if request.status != "pending":
        raise AppError(CommonErrorCode.CONFLICT, "该申请已审批", 409)
    if not _can_approve(identity, request):
        raise AppError(CommonErrorCode.FORBIDDEN, "仅直属上级可审批，申请人不能自批", 403)
    if not payload.approve and not payload.comment.strip():
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "驳回时必须填写原因", 400)
    customer = session.get(Customer, request.customer_id)
    if customer is None or customer.deleted_at is not None:
        raise AppError(CommonErrorCode.CONFLICT, "客户已不存在", 409)
    before = request.before_data or {}
    if (
        str(customer.organization_id) != before.get("organizationId")
        or customer.owner_name != before.get("ownerName")
    ):
        raise AppError(CommonErrorCode.CONFLICT, "客户归属已变化，请驳回后重新申请", 409)
    now = datetime.now(timezone.utc)
    if payload.approve:
        if request.kind == "ownership":
            after = request.after_data or {}
            target = session.get(Organization, uuid.UUID(after["organizationId"]))
            if target is None or not target.is_active:
                raise AppError(CommonErrorCode.CONFLICT, "目标组织已不可用", 409)
            owner_name = after["ownerName"]
            owner = session.scalar(
                select(User).where(
                    User.organization_id == target.id,
                    User.display_name == owner_name,
                    User.is_active.is_(True),
                )
            )
            extra = dict(customer.extra_data or {})
            flow = list(extra.get("crmFlow") or [])
            flow.append({
                "id": f"crm-approved-{request.id}",
                "time": now.isoformat(),
                "action": "客户转派" if customer.organization_id != target.id else "负责人变更",
                "fromOrgId": str(customer.organization_id),
                "fromLabel": customer.owner_name,
                "toOrgId": str(target.id),
                "toLabel": owner_name,
                "operator": identity.user.display_name,
                "reason": f"审批通过：{request.reason}",
            })
            extra["crmFlow"] = flow
            extra["assignmentReason"] = "上级审核通过的归属变更"
            extra["assignmentConfidence"] = "人工审核"
            customer.extra_data = extra
            customer.organization_id = target.id
            customer.owner_user_id = owner.id if owner else None
            customer.owner_name = owner_name
            customer.province = target.province
            customer.city = target.city
            customer.district = target.district
            customer.version += 1
        elif request.kind == "delete":
            customer.deleted_at = now
            customer.version += 1
    request.status = "approved" if payload.approve else "rejected"
    request.reviewer_id = identity.user.id
    request.reviewed_at = now
    request.review_comment = payload.comment.strip() or "审核通过"
    session.commit()
    session.refresh(request)
    return serialize_request(session, request)
