import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity, UserManagerIdentity
from app.modules.auth.models import Role
from app.modules.auth.service import public_identity
from app.modules.users.schemas import UserCreate, UserUpdate
from app.modules.users.service import (
    _manageable_user,
    create_user,
    deactivate_user,
    list_users,
    update_user,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _success(request: Request, data) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


@router.get("/roles")
def roles(request: Request, identity: CurrentIdentity, session: DbSession) -> dict:
    items = session.scalars(select(Role).order_by(Role.name)).all()
    if not identity.is_super_admin:
        items = [item for item in items if item.code not in {"super_admin", "group_admin"}]
    return _success(
        request,
        [
            {
                "id": str(item.id),
                "code": item.code,
                "name": item.name,
                "description": item.description,
            }
            for item in items
        ],
    )


@router.get("")
def users(
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    search: str | None = Query(None, max_length=80),
    organizationId: uuid.UUID | None = None,
    includeDescendants: bool = True,
) -> dict:
    items, total = list_users(
        session, identity, page, pageSize, search, organizationId, includeDescendants
    )
    return _success(request, PageData.build(items, page, pageSize, total).model_dump())


@router.get("/{user_id}")
def user_detail(
    user_id: uuid.UUID, request: Request, identity: CurrentIdentity, session: DbSession
) -> dict:
    if user_id == identity.user.id:
        user = identity.user
    else:
        if not (
            identity.is_super_admin
            or identity.is_group_admin
            or identity.is_org_admin
            or "department_manager" in identity.role_codes
        ):
            raise AppError(CommonErrorCode.FORBIDDEN, "无权查看其他用户", 403)
        user = _manageable_user(session, identity, user_id)
    return _success(request, public_identity(session, user))


@router.post("", status_code=201)
def add_user(
    payload: UserCreate, request: Request, identity: UserManagerIdentity, session: DbSession
) -> dict:
    return _success(request, public_identity(session, create_user(session, identity, payload)))


@router.patch("/{user_id}")
def edit_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    identity: UserManagerIdentity,
    session: DbSession,
) -> dict:
    return _success(
        request, public_identity(session, update_user(session, identity, user_id, payload))
    )


@router.delete("/{user_id}")
def remove_user(
    user_id: uuid.UUID,
    request: Request,
    identity: UserManagerIdentity,
    session: DbSession,
) -> dict:
    user = deactivate_user(session, identity, user_id)
    return _success(request, {"id": str(user.id), "deactivated": True})
