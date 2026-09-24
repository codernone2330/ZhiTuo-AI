"""Retain every shared-document revision for rollback and audit.

Revision ID: 20260925_0009
Revises: 20260924_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0009"
down_revision: str | None = "20260924_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("shared_documents.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "version"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    # Existing files have only their current binary; preserve this first known snapshot.
    op.execute(
        sa.text(
            "INSERT INTO document_versions "
            "(id, document_id, version, name, category, mime_type, size_bytes, "
            "content, created_by, created_at) "
            "SELECT id, id, version, name, category, mime_type, size_bytes, "
            "content, updated_by, updated_at FROM shared_documents"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
