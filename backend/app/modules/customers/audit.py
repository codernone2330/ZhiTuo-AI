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
