from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.modules.auth.dependencies import IdentityContext
from app.modules.customers.audit import customer_timeline
from app.modules.customers.models import Customer, CustomerEvent
from app.modules.customers.service import _customer_statement
from app.modules.visits.models import Visit
from app.modules.visits.service import serialize_visit


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def get_customer_journey(
    session: Session, identity: IdentityContext, customer_ref: str
) -> dict:
    customer = session.scalar(
        _customer_statement(identity).where(Customer.external_id == customer_ref)
    )
    if customer is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "客户不存在或不在授权范围内", 404)

    visit_query = select(Visit).where(Visit.customer_id == customer.id)
    if "customer_manager" in identity.role_codes:
        visit_query = visit_query.where(Visit.owner_name == identity.user.display_name)
    visits = session.scalars(visit_query.order_by(Visit.created_at, Visit.id)).all()
    serialized_visits = [serialize_visit(visit, customer) for visit in visits]

    scores = session.scalars(
        select(CustomerEvent)
        .where(
            CustomerEvent.customer_id == customer.id,
            CustomerEvent.action.in_(("score_baseline", "score_updated")),
        )
        .order_by(CustomerEvent.created_at.desc(), CustomerEvent.id.desc())
    ).all()
    score_log = [
        {
            "id": str(event.id),
            "time": _iso_utc(event.created_at),
            "fromScore": (event.before_data or {}).get("score"),
            "toScore": (event.after_data or {}).get("score"),
            "reasons": (event.after_data or {}).get("reasons") or [],
            "trigger": (event.after_data or {}).get("trigger") or event.reason,
            "provenance": (event.after_data or {}).get("provenance") or (
                "import_baseline" if event.action == "score_baseline" else "server_change"
            ),
            "ruleVersion": (event.after_data or {}).get("ruleVersion") or "unknown",
        }
        for event in scores
    ]
    deals = [
        {
            "visitId": visit.external_id,
            "product": visit.deal_product,
            "amount": str(visit.deal_amount),
            "remark": visit.deal_remark,
            "time": _iso_utc(visit.completed_at),
            "simulated": bool((visit.extra_data or {}).get("simulated")),
        }
        for visit in visits
        if visit.status == "completed" and visit.deal_amount > 0
    ]
    return {
        "customerId": customer.external_id,
        "customerName": customer.name,
        "currentStage": customer.stage,
        "currentScore": customer.score,
        "scoreLog": score_log,
        "visits": serialized_visits,
        "deals": deals,
        "events": customer_timeline(session, customer),
        "summary": {
            "plannedVisits": sum(visit.status == "pending" for visit in visits),
            "completedVisits": sum(visit.status == "completed" for visit in visits),
            "canceledVisits": sum(visit.status == "canceled" for visit in visits),
            "dealCount": len(deals),
            "dealAmount": str(sum((Decimal(deal["amount"]) for deal in deals), Decimal("0"))),
        },
    }
