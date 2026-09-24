"""Add a cross-module API audit stream.

Revision ID: 20260925_0010
Revises: 20260925_0009
"""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0010"
down_revision = "20260925_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("actor_id", "organization_id", "action", "trace_id", "created_at"):
        op.create_index(f"ix_api_audit_{name}", "api_audit", [name])


def downgrade() -> None:
    op.drop_table("api_audit")
