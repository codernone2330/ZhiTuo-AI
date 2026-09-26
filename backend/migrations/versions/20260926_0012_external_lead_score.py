"""Store qichacha opportunity score and risk detail on external leads.

Revision ID: 20260926_0012
Revises: 20260925_0011
"""

import sqlalchemy as sa
from alembic import op

revision = "20260926_0012"
down_revision = "20260925_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_leads", sa.Column("score", sa.Integer(), nullable=True))
    op.add_column("external_leads", sa.Column("score_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("external_leads", "score_detail")
    op.drop_column("external_leads", "score")
