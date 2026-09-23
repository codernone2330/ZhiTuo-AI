import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity
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
