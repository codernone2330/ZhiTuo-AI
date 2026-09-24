"""Correlate external integration events with API outcome audits.

Revision ID: 20260925_0011
Revises: 20260925_0010
"""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0011"
down_revision = "20260925_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integration_audits", sa.Column("trace_id", sa.String(80), nullable=True))
    op.create_index("ix_integration_audits_trace_id", "integration_audits", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_integration_audits_trace_id", table_name="integration_audits")
    op.drop_column("integration_audits", "trace_id")
