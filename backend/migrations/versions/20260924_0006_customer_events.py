"""Add append-only CRM customer audit events.

Revision ID: 20260924_0006
Revises: 20260923_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0006"
down_revision: str | None = "20260923_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "customer_id", sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "actor_id", sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "request_id", sa.Uuid(),
            sa.ForeignKey("customer_requests.id", ondelete="RESTRICT"),
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("before_data", sa.JSON(), nullable=False),
        sa.Column("after_data", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("customer_id", "actor_id", "request_id", "action"):
        op.create_index(f"ix_customer_events_{column}", "customer_events", [column])


def downgrade() -> None:
    op.drop_table("customer_events")
