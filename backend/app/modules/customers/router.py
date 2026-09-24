import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity
from app.modules.customers.audit import record_customer_event
from app.modules.customers.journey import get_customer_journey
from app.modules.customers.models import (
    Customer,
    CustomerEvent,
    CustomerImportBatch,
    CustomerRequest,
)
from app.modules.customers.requests import create_request, decide_request, list_requests
from app.modules.customers.schemas import (
    CustomerImportRequest,
    CustomerRequestCreate,
    CustomerRequestDecision,
    CustomerUpdate,
)
from app.modules.customers.service import (
    get_customer,
    import_customers,
    list_customers,
    update_customer,
)
from app.modules.visits.models import Visit

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _success(request: Request, data) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


@router.get("")
def customers(
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    search: str | None = Query(None, max_length=100),
    stage: str | None = Query(None, max_length=30),
    kind: str | None = Query(None, max_length=20),
    organizationId: uuid.UUID | None = None,
    includeDescendants: bool = True,
) -> dict:
    items, total = list_customers(
        session,
        identity,
        page,
        pageSize,
        search,
        stage,
        kind,
        organizationId,
        includeDescendants,
    )
    return _success(request, PageData.build(items, page, pageSize, total).model_dump())


@router.post("/import", status_code=status.HTTP_201_CREATED)
def customer_import(
    payload: CustomerImportRequest,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, import_customers(session, identity, payload))


@router.post("/imports/{batch_id}/rollback")
def rollback_demo_import(
    batch_id: uuid.UUID, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    """Revert only an untouched, explicitly-triggered HTML demo migration batch."""
    if not (identity.is_super_admin or identity.is_group_admin):
        raise AppError(CommonErrorCode.FORBIDDEN, "仅集团管理员可回滚演示数据", 403)
    batch = session.get(CustomerImportBatch, batch_id)
    if (
        batch is None
        or batch.status != "completed"
        or batch.source_name != "现有HTML客户数据手动迁移"
    ):
        raise AppError(CommonErrorCode.NOT_FOUND, "可回滚的演示导入批次不存在", 404)
    customers = session.scalars(select(Customer).where(Customer.import_batch_id == batch_id)).all()
    customer_ids = {customer.id for customer in customers}
    visits = (
        session.scalars(select(Visit).where(Visit.customer_id.in_(customer_ids))).all()
        if customer_ids
        else []
    )
    if any(customer.version != 1 or customer.deleted_at is not None for customer in customers):
        raise AppError(CommonErrorCode.CONFLICT, "客户已被修改，不能自动回滚", 409)
    if customer_ids and session.scalar(
        select(CustomerRequest.id).where(CustomerRequest.customer_id.in_(customer_ids)).limit(1)
    ):
        raise AppError(CommonErrorCode.CONFLICT, "客户已有审批记录，不能自动回滚", 409)
    if customer_ids and session.scalar(
        select(CustomerEvent.id)
        .where(
            CustomerEvent.customer_id.in_(customer_ids),
            CustomerEvent.action != "score_baseline",
        )
        .limit(1)
    ):
        raise AppError(CommonErrorCode.CONFLICT, "客户已有业务变更，不能自动回滚", 409)
    if any(
        visit.version != 1 or (visit.extra_data or {}).get("demoImportBatchId") != str(batch_id)
        for visit in visits
    ):
        raise AppError(CommonErrorCode.CONFLICT, "关联拜访已变化，不能自动回滚", 409)
    for visit in visits:
        session.delete(visit)
    now = datetime.now(timezone.utc)
    for customer in customers:
        customer.deleted_at = now
        customer.version += 1
        record_customer_event(
            session, customer, identity.user, "demo_import_rolled_back", reason=str(batch_id)
        )
    batch.status = "rolled_back"
    session.commit()
    return _success(
        request, {"rolledBackCustomers": len(customers), "rolledBackVisits": len(visits)}
    )


@router.get("/requests")
def customer_requests(request: Request, identity: CurrentIdentity, session: DbSession) -> dict:
    return _success(request, list_requests(session, identity))


@router.post("/requests/{request_id}/decision")
def customer_request_decision(
    request_id: uuid.UUID,
    payload: CustomerRequestDecision,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, decide_request(session, identity, request_id, payload))


@router.post("/{customer_ref}/requests", status_code=status.HTTP_201_CREATED)
def customer_request_create(
    customer_ref: str,
    payload: CustomerRequestCreate,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, create_request(session, identity, customer_ref, payload))


@router.get("/{customer_ref}/journey")
def customer_journey(
    customer_ref: str, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    return _success(request, get_customer_journey(session, identity, customer_ref))


@router.get("/{customer_ref}")
def customer_detail(
    customer_ref: str, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    return _success(request, get_customer(session, identity, customer_ref))


@router.patch("/{customer_ref}")
def customer_edit(
    customer_ref: str,
    payload: CustomerUpdate,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, update_customer(session, identity, customer_ref, payload))
