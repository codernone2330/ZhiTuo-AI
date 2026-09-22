from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity
from app.modules.auth.schemas import LoginRequest
from app.modules.auth.service import (
    access_token_ttl_seconds,
    authenticate,
    issue_session,
    public_identity,
    refresh_access_token,
    revoke_session,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
COOKIE_NAME = "zhituo_refresh_token"


def _success(request: Request, data: dict) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


def _set_refresh_cookie(response: Response, token: str, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
        path=f"{settings.api_v1_prefix}/auth",
    )


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, session: DbSession) -> dict:
    user = authenticate(session, payload.username, payload.password)
    access_token, refresh_token, _ = issue_session(session, user)
    refresh_max_age = get_settings().refresh_token_expire_days * 24 * 60 * 60
    _set_refresh_cookie(response, refresh_token, refresh_max_age)
    return _success(
        request,
        {
            "accessToken": access_token,
            "tokenType": "bearer",
            "expiresIn": access_token_ttl_seconds(),
            "user": public_identity(session, user),
        },
    )


@router.post("/refresh")
def refresh(
    request: Request,
    session: DbSession,
    refresh_token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> dict:
    if not refresh_token:
        from app.core.exceptions import AppError, CommonErrorCode

        raise AppError(CommonErrorCode.UNAUTHORIZED, "缺少刷新凭证", 401)
    token = refresh_access_token(session, refresh_token)
    return _success(
        request,
        {"accessToken": token, "tokenType": "bearer", "expiresIn": access_token_ttl_seconds()},
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: DbSession,
    refresh_token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> dict:
    revoke_session(session, refresh_token)
    response.delete_cookie(COOKIE_NAME, path=f"{get_settings().api_v1_prefix}/auth")
    return _success(request, {"loggedOut": True})


@router.get("/me")
def me(request: Request, identity: CurrentIdentity, session: DbSession) -> dict:
    return _success(request, public_identity(session, identity.user))
