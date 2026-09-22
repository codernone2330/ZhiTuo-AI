import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.modules.auth.dependencies import IdentityContext
from app.modules.organizations.models import Organization


def scoped_organizations(session: Session, identity: IdentityContext) -> list[Organization]:
    statement = select(Organization).where(Organization.is_active.is_(True))
    if not (identity.is_super_admin or identity.is_group_admin):
        if identity.is_org_admin:
            statement = statement.where(
                (Organization.path == identity.organization.path)
                | Organization.path.startswith(identity.organization.path + "/")
            )
        else:
            statement = statement.where(Organization.id == identity.organization.id)
    return list(
        session.scalars(statement.order_by(Organization.path, Organization.sort_order)).all()
    )


def get_scoped_organization(
    session: Session, identity: IdentityContext, organization_id: uuid.UUID
) -> Organization:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "组织不存在", 404)
    if not identity.organization_in_scope(organization):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权访问该组织", 403)
    return organization


def serialize_organization(organization: Organization, children: list[dict] | None = None) -> dict:
    return {
        "id": str(organization.id),
        "code": organization.code,
        "name": organization.name,
        "level": organization.level,
        "parentId": str(organization.parent_id) if organization.parent_id else None,
        "path": organization.path,
        "province": organization.province,
        "city": organization.city,
        "district": organization.district,
        "isActive": organization.is_active,
        "children": children or [],
    }


def organization_tree(organizations: list[Organization]) -> list[dict]:
    by_parent: dict[uuid.UUID | None, list[Organization]] = {}
    ids = {organization.id for organization in organizations}
    for organization in organizations:
        parent_id = organization.parent_id if organization.parent_id in ids else None
        by_parent.setdefault(parent_id, []).append(organization)
    for values in by_parent.values():
        values.sort(key=lambda item: (item.sort_order, item.name))

    def build(parent_id: uuid.UUID | None) -> list[dict]:
        return [
            serialize_organization(item, build(item.id)) for item in by_parent.get(parent_id, [])
        ]

    return build(None)
