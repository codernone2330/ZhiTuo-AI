import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity
from app.modules.opportunities.service import (
    get_opportunity,
    list_opportunities,
    refresh_opportunities,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _success(request: Request, data) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


@router.get("")
def opportunities(
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    organizationId: uuid.UUID | None = None,
) -> dict:
    items, total = list_opportunities(session, identity, page, pageSize, organizationId)
    return _success(request, PageData.build(items, page, pageSize, total).model_dump())


@router.post("/refresh")
def opportunity_refresh(
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
    organizationId: uuid.UUID | None = None,
) -> dict:
    return _success(request, refresh_opportunities(session, identity, organizationId))


@router.get("/{customer_ref}")
def opportunity_detail(
    customer_ref: str, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    return _success(request, get_opportunity(session, identity, customer_ref))
