import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppError, CommonErrorCode
from app.core.security import hash_password
from app.modules.auth.dependencies import IdentityContext
from app.modules.auth.models import AuthSession, Role, UserRole
from app.modules.auth.service import public_identity, role_codes
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from app.modules.users.schemas import UserCreate, UserUpdate


def _require_shared_password(password: str) -> None:
    if get_settings().app_env.lower() in {"local", "development", "test"}:
        return
    if len(password) < 12 or password in {"szyd123456", "123456"}:
        raise AppError(
            CommonErrorCode.INVALID_ARGUMENT,
            "共享环境口令至少 12 位且不能使用演示口令",
            400,
        )


def _target_organization(
    session: Session, identity: IdentityContext, organization_id: uuid.UUID
) -> Organization:
    organization = session.get(Organization, organization_id)
    if organization is None or not organization.is_active:
        raise AppError(CommonErrorCode.NOT_FOUND, "目标组织不存在或已停用", 404)
    if not identity.organization_in_scope(organization):
        raise AppError(CommonErrorCode.FORBIDDEN, "不能将用户分配到授权范围以外的组织", 403)
    return organization


def _role(
    session: Session, identity: IdentityContext, code: str, organization: Organization
) -> Role:
    role = session.scalar(select(Role).where(Role.code == code))
    if role is None:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "角色不存在", 400)
    if code == "super_admin":
        if not identity.is_super_admin:
            raise AppError(CommonErrorCode.FORBIDDEN, "只有超级管理员可以授予超级管理员角色", 403)
        if organization.level != "group":
            raise AppError(CommonErrorCode.INVALID_ARGUMENT, "超级管理员必须归属集团组织", 400)
    elif code == "group_admin":
        if not identity.is_super_admin:
            raise AppError(CommonErrorCode.FORBIDDEN, "只有超级管理员可以授予集团管理员角色", 403)
        if organization.level != "group":
            raise AppError(CommonErrorCode.INVALID_ARGUMENT, "集团管理员必须归属集团组织", 400)
    elif code == "org_admin" and organization.level == "department":
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "组织管理员不能归属部门节点", 400)
    elif code == "department_manager" and organization.level != "department":
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "部门负责人必须归属部门节点", 400)
    elif code == "customer_manager" and not organization.code.endswith("-enterprise"):
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "客户经理必须归属集客部", 400)
    return role


def _assert_unique(session: Session, username: str | None, employee_no: str | None, except_id=None):
    if username:
        statement = select(User.id).where(User.username == username)
        if except_id:
            statement = statement.where(User.id != except_id)
        if session.scalar(statement):
            raise AppError(CommonErrorCode.CONFLICT, "登录账号已存在", 409)
    if employee_no:
        statement = select(User.id).where(User.employee_no == employee_no)
        if except_id:
            statement = statement.where(User.id != except_id)
        if session.scalar(statement):
            raise AppError(CommonErrorCode.CONFLICT, "工号已存在", 409)


def _manageable_user(session: Session, identity: IdentityContext, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "用户不存在", 404)
    organization = session.get(Organization, user.organization_id)
    if organization is None or not identity.organization_in_scope(organization):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权管理该用户", 403)
    if "super_admin" in role_codes(session, user.id) and not identity.is_super_admin:
        raise AppError(CommonErrorCode.FORBIDDEN, "只有超级管理员可以管理超级管理员账号", 403)
    return user


def list_users(
    session: Session,
    identity: IdentityContext,
    page: int,
    page_size: int,
    search: str | None,
    organization_id: uuid.UUID | None,
    include_descendants: bool,
) -> tuple[list[dict], int]:
    statement = select(User).join(Organization, User.organization_id == Organization.id)
    count_statement = (
        select(func.count())
        .select_from(User)
        .join(Organization, User.organization_id == Organization.id)
    )
    conditions = []
    if identity.is_super_admin or identity.is_group_admin:
        pass
    elif identity.is_org_admin:
        conditions.append(
            (Organization.path == identity.organization.path)
            | Organization.path.startswith(identity.organization.path + "/")
        )
    elif "department_manager" in identity.role_codes:
        conditions.append(User.organization_id == identity.organization.id)
    else:
        conditions.append(User.id == identity.user.id)

    if organization_id:
        organization = _target_organization(session, identity, organization_id)
        if include_descendants:
            conditions.append(
                (Organization.path == organization.path)
                | Organization.path.startswith(organization.path + "/")
            )
        else:
            conditions.append(User.organization_id == organization.id)
    if search:
        keyword = f"%{search.strip()}%"
        conditions.append(
            or_(
                User.username.ilike(keyword),
                User.employee_no.ilike(keyword),
                User.display_name.ilike(keyword),
            )
        )
    if conditions:
        statement = statement.where(*conditions)
        count_statement = count_statement.where(*conditions)
    total = int(session.scalar(count_statement) or 0)
    users = session.scalars(
        statement.order_by(User.created_at, User.username)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [public_identity(session, user) for user in users], total


def create_user(session: Session, identity: IdentityContext, payload: UserCreate) -> User:
    _require_shared_password(payload.password)
    organization = _target_organization(session, identity, payload.organizationId)
    role = _role(session, identity, payload.roleCode, organization)
    username = payload.username.strip()
    employee_no = payload.employeeNo.strip()
    _assert_unique(session, username, employee_no)
    user = User(
        username=username,
        employee_no=employee_no,
        display_name=payload.displayName.strip(),
        password_hash=hash_password(payload.password),
        organization_id=organization.id,
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    session.refresh(user)
    return user


def update_user(
    session: Session, identity: IdentityContext, user_id: uuid.UUID, payload: UserUpdate
) -> User:
    user = _manageable_user(session, identity, user_id)
    if payload.isActive is False and user.id == identity.user.id:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "不能停用当前登录账号", 400)
    if payload.isActive is False and "super_admin" in role_codes(session, user.id):
        active_super_admins = session.scalar(
            select(func.count())
            .select_from(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(User.is_active.is_(True), Role.code == "super_admin")
        )
        if int(active_super_admins or 0) <= 1:
            raise AppError(CommonErrorCode.CONFLICT, "不能停用最后一个超级管理员", 409)
    if payload.isActive is False and "group_admin" in role_codes(session, user.id):
        active_group_admins = session.scalar(
            select(func.count())
            .select_from(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(User.is_active.is_(True), Role.code == "group_admin")
        )
        if int(active_group_admins or 0) <= 1:
            raise AppError(CommonErrorCode.CONFLICT, "不能停用最后一个集团管理员", 409)
    organization = (
        _target_organization(session, identity, payload.organizationId)
        if payload.organizationId
        else session.get(Organization, user.organization_id)
    )
    current_role = role_codes(session, user.id)[0] if role_codes(session, user.id) else ""
    role = _role(session, identity, payload.roleCode or current_role, organization)
    _assert_unique(
        session, None, payload.employeeNo.strip() if payload.employeeNo else None, user.id
    )
    if payload.displayName is not None:
        user.display_name = payload.displayName.strip()
    if payload.employeeNo is not None:
        user.employee_no = payload.employeeNo.strip()
    if payload.password is not None:
        _require_shared_password(payload.password)
        user.password_hash = hash_password(payload.password)
    user.organization_id = organization.id
    if payload.isActive is not None:
        user.is_active = payload.isActive
    if payload.roleCode is not None:
        session.query(UserRole).filter(UserRole.user_id == user.id).delete()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    if not user.is_active:
        session.query(AuthSession).filter(
            AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
        ).update({AuthSession.revoked_at: func.now()}, synchronize_session=False)
    session.commit()
    session.refresh(user)
    return user


def deactivate_user(session: Session, identity: IdentityContext, user_id: uuid.UUID) -> User:
    return update_user(session, identity, user_id, UserUpdate(isActive=False))
