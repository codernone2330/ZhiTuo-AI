import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity
from app.modules.organizations.models import Organization
from app.modules.organizations.service import (
    get_scoped_organization,
    organization_tree,
    scoped_organizations,
    serialize_organization,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _success(request: Request, data) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


@router.get("/tree")
def tree(request: Request, identity: CurrentIdentity, session: DbSession) -> dict:
    return _success(request, organization_tree(scoped_organizations(session, identity)))


@router.get("/{organization_id}")
def detail(
    organization_id: uuid.UUID,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    return _success(
        request,
        serialize_organization(get_scoped_organization(session, identity, organization_id)),
    )


@router.get("/{organization_id}/children")
def children(
    organization_id: uuid.UUID,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    organization = get_scoped_organization(session, identity, organization_id)
    items = list(
        session.scalars(
            select(Organization)
            .where(Organization.parent_id == organization.id, Organization.is_active.is_(True))
            .order_by(Organization.sort_order, Organization.name)
        ).all()
    )
    items = [item for item in items if identity.organization_in_scope(item)]
    return _success(request, [serialize_organization(item) for item in items])
