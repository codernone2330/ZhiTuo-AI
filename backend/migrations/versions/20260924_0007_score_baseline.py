"""Backfill an explicitly marked baseline for existing customer scores.

Revision ID: 20260924_0007
Revises: 20260924_0006
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0007"
down_revision: str | None = "20260924_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    customers = sa.table(
        "customers",
        sa.column("id", sa.Uuid()),
        sa.column("imported_by", sa.Uuid()),
        sa.column("score", sa.Integer()),
        sa.column("extra_data", sa.JSON()),
    )
    events = sa.table(
        "customer_events",
        sa.column("id", sa.Uuid()),
        sa.column("customer_id", sa.Uuid()),
        sa.column("actor_id", sa.Uuid()),
        sa.column("request_id", sa.Uuid()),
        sa.column("action", sa.String()),
        sa.column("before_data", sa.JSON()),
        sa.column("after_data", sa.JSON()),
        sa.column("reason", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    scored = set(connection.scalars(
        sa.select(events.c.customer_id).where(
            events.c.action.in_(("score_baseline", "score_updated"))
        )
    ))
    now = datetime.now(timezone.utc)
    for customer in connection.execute(sa.select(customers)).mappings():
        if customer["id"] in scored:
            continue
        extra = customer["extra_data"] or {}
        connection.execute(events.insert().values(
            id=uuid.uuid4(), customer_id=customer["id"],
            actor_id=customer["imported_by"], request_id=None,
            action="score_baseline", before_data={},
            after_data={
                "score": customer["score"],
                "reasons": extra.get("reasons") or [],
                "provenance": "migration_baseline",
                "ruleVersion": "unknown",
            },
            reason="历史客户当前评分的迁移基线，非原始评分过程",
            created_at=now,
        ))


def downgrade() -> None:
    op.execute("DELETE FROM customer_events WHERE action = 'score_baseline' "
               "AND reason = '历史客户当前评分的迁移基线，非原始评分过程'")
