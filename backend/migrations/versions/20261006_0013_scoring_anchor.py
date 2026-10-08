"""Add scoring anchor lock table for the enterprise scoring model.

Revision ID: 20261006_0013
Revises: 20260926_0012
"""

import sqlalchemy as sa
from alembic import op

revision = "20261006_0013"
down_revision = "20260926_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scoring_anchor_locks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("region", sa.String(length=120), nullable=False),
        sa.Column("anchor_mode", sa.String(length=20), nullable=False, server_default="auto"),
        sa.Column("size_anchor", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cap_anchor", sa.Float(), nullable=False, server_default="0"),
        sa.Column("size_pct", sa.Integer(), nullable=False, server_default="95"),
        sa.Column("cap_pct", sa.Integer(), nullable=False, server_default="95"),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scoring_anchor_locks_region", "scoring_anchor_locks", ["region"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_scoring_anchor_locks_region", table_name="scoring_anchor_locks")
    op.drop_table("scoring_anchor_locks")
