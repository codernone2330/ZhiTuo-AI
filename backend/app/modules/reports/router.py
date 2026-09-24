from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.db.session import get_db
from app.modules.ai.models import IntegrationAudit
from app.modules.auth.dependencies import CurrentIdentity
from app.modules.customers.service import _customer_statement
from app.modules.organizations.models import Organization
from app.modules.visits.models import Visit

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _week(value: date) -> tuple[datetime, datetime]:
    monday = value - timedelta(days=value.weekday())
    start = datetime.combine(monday, time.min, tzinfo=BUSINESS_TIMEZONE)
    return start, start + timedelta(days=7)


def _metrics(visits: list[Visit], start: datetime, end: datetime) -> dict:
    completed = [
        v
        for v in visits
        if v.status == "completed"
        and v.completed_at
        and start
        <= (
            v.completed_at.replace(tzinfo=timezone.utc)
            if v.completed_at.tzinfo is None
            else v.completed_at
        )
        < end
    ]
    deals = [
        v for v in completed if v.outcome == "已达成合作" or v.deal_amount and v.deal_amount > 0
    ]
    return {
        "completedVisits": len(completed),
        "visitedCustomers": len({v.customer_id for v in completed}),
        "dealCount": len(deals),
        "dealAmount": str(sum((v.deal_amount or Decimal("0") for v in deals), Decimal("0"))),
    }


def _change(current, base):
    a, b = Decimal(str(current)), Decimal(str(base))
    return None if b == 0 else round(float((a - b) / abs(b) * 100), 1)


@router.get("/weekly")
def weekly(
    request: Request,
    identity: CurrentIdentity,
    session: Db,
    weekStart: date | None = None,
    organizationId: str | None = None,
    owner: str | None = None,
):
    statement = _customer_statement(identity)
    if organizationId:
        import uuid

        try:
            org = session.get(Organization, uuid.UUID(organizationId))
        except ValueError as exc:
            raise AppError(CommonErrorCode.INVALID_ARGUMENT, "组织 ID 格式错误", 400) from exc
        if org is None or not identity.organization_in_scope(org):
            raise AppError(CommonErrorCode.FORBIDDEN, "无权查看该组织报表", 403)
        statement = statement.where(
            (Organization.path == org.path) | Organization.path.startswith(org.path + "/")
        )
    customers = session.scalars(statement).all()
    by_id = {row.id: row for row in customers}
    visits = (
        session.scalars(select(Visit).where(Visit.customer_id.in_(by_id))).all() if by_id else []
    )
    if "customer_manager" in identity.role_codes:
        visits = [v for v in visits if v.owner_name == identity.user.display_name]
    if owner:
        visits = [v for v in visits if v.owner_name == owner]
    start, end = _week(weekStart or datetime.now(BUSINESS_TIMEZONE).date())
    current = _metrics(visits, start, end)
    previous = _metrics(visits, start - timedelta(days=7), start)
    last_year = _metrics(visits, start - timedelta(days=364), end - timedelta(days=364))
    details = []
    owners = defaultdict(list)
    for visit in visits:
        if visit.status != "completed" or not visit.completed_at:
            continue
        completed_at = (
            visit.completed_at.replace(tzinfo=timezone.utc)
            if visit.completed_at.tzinfo is None
            else visit.completed_at
        )
        if not start <= completed_at < end:
            continue
        customer = by_id[visit.customer_id]
        details.append(
            {
                "visitId": visit.external_id,
                "customerId": customer.external_id,
                "customerName": customer.name,
                "organizationId": str(customer.organization_id),
                "owner": visit.owner_name,
                "completedAt": completed_at.isoformat(),
                "outcome": visit.outcome,
                "dealAmount": str(visit.deal_amount or 0),
            }
        )
        owners[visit.owner_name].append(visit)
    owner_rows = [
        {"owner": name, **_metrics(rows, start, end)} for name, rows in sorted(owners.items())
    ]
    comparisons = {
        key: {
            "current": current[key],
            "momBase": previous[key],
            "yoyBase": last_year[key],
            "momPercent": _change(current[key], previous[key]),
            "yoyPercent": _change(current[key], last_year[key]),
        }
        for key in current
    }
    return {
        "success": True,
        "data": {
            "weekStart": start.date().isoformat(),
            "weekEnd": (end - timedelta(days=1)).date().isoformat(),
            "organizationId": organizationId or str(identity.organization.id),
            "owner": owner,
            "current": current,
            "previous": previous,
            "lastYear": last_year,
            "comparisons": comparisons,
            "owners": owner_rows,
            "details": details,
        },
        "traceId": request.state.trace_id,
    }


@router.get("/audit")
def report_audit(
    request: Request, identity: CurrentIdentity, session: Db, limit: int = Query(50, ge=1, le=200)
):
    statement = select(IntegrationAudit).join(
        Organization, IntegrationAudit.organization_id == Organization.id
    )
    if identity.is_super_admin or identity.is_group_admin:
        pass
    elif identity.is_org_admin:
        statement = statement.where(
            (Organization.path == identity.organization.path)
            | Organization.path.startswith(identity.organization.path + "/")
        )
    else:
        statement = statement.where(IntegrationAudit.actor_id == identity.user.id)
    rows = session.scalars(
        statement.order_by(IntegrationAudit.created_at.desc()).limit(limit)
    ).all()
    return {
        "success": True,
        "data": [
            {
                "id": str(row.id),
                "action": row.action,
                "targetId": row.target_id,
                "detail": row.detail,
                "createdAt": row.created_at.isoformat(),
                "actorId": str(row.actor_id),
            }
            for row in rows
        ],
        "traceId": request.state.trace_id,
    }
