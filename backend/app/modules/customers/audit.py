import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.customers.models import Customer, CustomerEvent
from app.modules.users.models import User


def record_customer_event(
    session: Session,
    customer: Customer,
    actor: User,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
    request_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> CustomerEvent:
    event = CustomerEvent(
        customer_id=customer.id,
        actor_id=actor.id,
        request_id=request_id,
        action=action,
        before_data=before or {},
        after_data=after or {},
        reason=reason,
        created_at=at or datetime.now(timezone.utc),
    )
    session.add(event)
    return event


def record_score_event(
    session: Session,
    customer: Customer,
    actor: User,
    before_score: int | None,
    before_reasons: list[str] | None,
    trigger: str,
    at: datetime | None = None,
) -> CustomerEvent | None:
    after_reasons = list((customer.extra_data or {}).get("reasons") or [])
    if before_score == customer.score and (before_reasons or []) == after_reasons:
        return None
    return record_customer_event(
        session, customer, actor,
        "score_baseline" if before_score is None else "score_updated",
        {} if before_score is None else {
            "score": before_score, "reasons": before_reasons or [],
        },
        {
            "score": customer.score, "reasons": after_reasons, "trigger": trigger,
            "provenance": "import_baseline" if before_score is None else "server_change",
            "ruleVersion": "imported_unverified" if before_score is None else "rule_v1",
        },
        trigger,
        at=at,
    )


def customer_timeline(session: Session, customer: Customer) -> list[dict]:
    events = session.scalars(
        select(CustomerEvent)
        .where(CustomerEvent.customer_id == customer.id)
        .order_by(CustomerEvent.created_at.desc(), CustomerEvent.id.desc())
        .limit(200)
    ).all()
    users = {
        user.id: user
        for user in session.scalars(
            select(User).where(User.id.in_({event.actor_id for event in events}))
        ).all()
    }
    return [
        {
            "id": str(event.id),
            "action": event.action,
            "actorId": str(event.actor_id),
            "actorName": (
                users[event.actor_id].display_name
                if event.actor_id in users else "未知用户"
            ),
            "requestId": str(event.request_id) if event.request_id else None,
            "before": event.before_data,
            "after": event.after_data,
            "reason": event.reason,
            "createdAt": (
                event.created_at if event.created_at.tzinfo
                else event.created_at.replace(tzinfo=timezone.utc)
            ).isoformat(),
        }
        for event in events
    ]
