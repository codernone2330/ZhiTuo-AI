from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity
from app.modules.visits.schemas import (
    VisitCancel,
    VisitComplete,
    VisitCreate,
    VisitImportRequest,
    VisitUpdate,
)
from app.modules.visits.service import (
    cancel_visit,
    complete_visit,
    create_visit,
    get_visit,
    import_visits,
    list_visits,
    update_visit,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _success(request: Request, data) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


@router.get("")
def visits(
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
) -> dict:
    items, total = list_visits(session, identity, page, pageSize)
    return _success(request, PageData.build(items, page, pageSize, total).model_dump())


@router.post("", status_code=status.HTTP_201_CREATED)
def visit_create(
    payload: VisitCreate, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    return _success(request, create_visit(session, identity, payload))


@router.post("/import", status_code=status.HTTP_201_CREATED)
def visit_import(
    payload: VisitImportRequest, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    return _success(request, import_visits(session, identity, payload))


@router.get("/{visit_ref}")
def visit_detail(
    visit_ref: str, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    return _success(request, get_visit(session, identity, visit_ref))


@router.patch("/{visit_ref}")
def visit_edit(
    visit_ref: str,
    payload: VisitUpdate,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, update_visit(session, identity, visit_ref, payload))


@router.post("/{visit_ref}/cancel")
def visit_cancel(
    visit_ref: str,
    payload: VisitCancel,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, cancel_visit(session, identity, visit_ref, payload))


@router.post("/{visit_ref}/complete")
def visit_complete(
    visit_ref: str,
    payload: VisitComplete,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(request, complete_visit(session, identity, visit_ref, payload))
