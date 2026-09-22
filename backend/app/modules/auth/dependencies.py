import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.core.security import decode_token
from app.db.session import get_db
from app.modules.auth.models import Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class IdentityContext:
    user: User
    organization: Organization
    role_codes: frozenset[str]

    @property
    def is_super_admin(self) -> bool:
        return "super_admin" in self.role_codes

    @property
    def is_group_admin(self) -> bool:
        return "group_admin" in self.role_codes

    @property
    def is_org_admin(self) -> bool:
        return "org_admin" in self.role_codes

    @property
    def can_manage_users(self) -> bool:
        return self.is_super_admin

    def organization_in_scope(self, organization: Organization) -> bool:
        if self.is_super_admin or self.is_group_admin:
            return True
        if self.is_org_admin:
            return organization.path == self.organization.path or organization.path.startswith(
                self.organization.path + "/"
            )
        return organization.id == self.organization.id


def _unauthorized(message: str = "登录状态无效或已过期") -> AppError:
    return AppError(CommonErrorCode.UNAUTHORIZED, message, 401)


def get_current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[Session, Depends(get_db)],
) -> IdentityContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("请先登录")
    try:
        payload = decode_token(credentials.credentials, "access")
        user_id = uuid.UUID(str(payload["sub"]))
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise _unauthorized() from exc

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("账号不存在或已停用")
    organization = session.get(Organization, user.organization_id)
    if organization is None or not organization.is_active:
        raise _unauthorized("账号所属组织不可用")
    role_codes = frozenset(
        session.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
        ).all()
    )
    return IdentityContext(user=user, organization=organization, role_codes=role_codes)


CurrentIdentity = Annotated[IdentityContext, Depends(get_current_identity)]


def require_user_manager(identity: CurrentIdentity) -> IdentityContext:
    if not identity.can_manage_users:
        raise AppError(CommonErrorCode.FORBIDDEN, "只有超级管理员可以管理登录用户", 403)
    return identity


UserManagerIdentity = Annotated[IdentityContext, Depends(require_user_manager)]
