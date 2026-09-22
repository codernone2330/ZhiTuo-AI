from datetime import datetime, timezone
from typing import Any

import jwt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppError, CommonErrorCode
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    token_digest,
    verify_password,
)
from app.modules.auth.models import AuthSession, Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User


def role_codes(session: Session, user_id) -> list[str]:
    return list(
        session.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.code)
        ).all()
    )


def public_identity(session: Session, user: User) -> dict[str, Any]:
    organization = session.get(Organization, user.organization_id)
    return {
        "id": str(user.id),
        "username": user.username,
        "employeeNo": user.employee_no,
        "displayName": user.display_name,
        "isActive": user.is_active,
        "roles": role_codes(session, user.id),
        "organization": {
            "id": str(organization.id),
            "code": organization.code,
            "name": organization.name,
            "level": organization.level,
            "path": organization.path,
        },
    }


def authenticate(session: Session, username: str, password: str) -> User:
    account = username.strip()
    users = list(
        session.scalars(
            select(User).where(
                User.is_active.is_(True),
                or_(
                    User.username == account,
                    User.employee_no == account,
                    User.display_name == account,
                ),
            )
        ).all()
    )
    user = users[0] if len(users) == 1 else None
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise AppError(CommonErrorCode.UNAUTHORIZED, "账号或密码错误", 401)
    user.last_login_at = datetime.now(timezone.utc)
    return user


def issue_session(session: Session, user: User) -> tuple[str, str, datetime]:
    access_token = create_access_token(str(user.id))
    refresh_token, expires_at = create_refresh_token(str(user.id))
    session.add(
        AuthSession(
            user_id=user.id,
            token_hash=token_digest(refresh_token),
            expires_at=expires_at,
        )
    )
    session.commit()
    return access_token, refresh_token, expires_at


def refresh_access_token(session: Session, refresh_token: str) -> str:
    try:
        payload = decode_token(refresh_token, "refresh")
    except jwt.PyJWTError as exc:
        raise AppError(CommonErrorCode.UNAUTHORIZED, "刷新凭证无效或已过期", 401) from exc
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_digest(refresh_token))
    )
    now = datetime.now(timezone.utc)
    expires_at = auth_session.expires_at if auth_session is not None else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or expires_at <= now
        or str(auth_session.user_id) != str(payload.get("sub"))
    ):
        raise AppError(CommonErrorCode.UNAUTHORIZED, "刷新会话已失效", 401)
    user = session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise AppError(CommonErrorCode.UNAUTHORIZED, "账号不存在或已停用", 401)
    return create_access_token(str(user.id))


def revoke_session(session: Session, refresh_token: str | None) -> None:
    if not refresh_token:
        return
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_digest(refresh_token))
    )
    if auth_session is not None and auth_session.revoked_at is None:
        auth_session.revoked_at = datetime.now(timezone.utc)
        session.commit()


def access_token_ttl_seconds() -> int:
    return get_settings().access_token_expire_minutes * 60
