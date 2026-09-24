"""Persist visit tasks and outcomes.

Revision ID: 20260923_0005
Revises: 20260923_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0005"
down_revision: str | None = "20260923_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "visits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("owner_name", sa.String(80), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(30), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("updated_by", sa.String(80)),
        sa.Column("canceled_by", sa.String(80)),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.String(300)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(30)),
        sa.Column("next_action", sa.String(500)),
        sa.Column("deal_product", sa.String(100)),
        sa.Column("deal_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("deal_remark", sa.String(300)),
        sa.Column("override_reason", sa.String(300)),
        sa.Column("conflict_task_id", sa.String(100)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("extra_data", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    for column in (
        "external_id",
        "customer_id",
        "owner_user_id",
        "owner_name",
        "scheduled_at",
        "status",
    ):
        op.create_index(f"ix_visits_{column}", "visits", [column], unique=column == "external_id")


def downgrade() -> None:
    op.drop_table("visits")
