"""Add customer read chain and import audit.

Revision ID: 20260922_0003
Revises: 20260922_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0003"
down_revision: str | None = "20260922_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("imported_by", sa.Uuid(), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("inserted_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
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
        sa.ForeignKeyConstraint(["imported_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_import_batches_imported_by", "customer_import_batches", ["imported_by"]
    )
    op.create_index("ix_customer_import_batches_status", "customer_import_batches", ["status"])
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("owner_name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("customer_type", sa.String(30), nullable=False),
        sa.Column("carrier", sa.String(50), nullable=True),
        sa.Column("industry", sa.String(100), nullable=False),
        sa.Column("province", sa.String(50), nullable=True),
        sa.Column("city", sa.String(50), nullable=True),
        sa.Column("district", sa.String(50), nullable=True),
        sa.Column("address", sa.String(500), nullable=True),
        sa.Column("contact_name", sa.String(80), nullable=True),
        sa.Column("contact_phone", sa.String(50), nullable=True),
        sa.Column("need", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("potential", sa.String(10), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("next_action", sa.String(500), nullable=False),
        sa.Column("is_qian_bai_wan_group", sa.Boolean(), nullable=False),
        sa.Column("is_key_account", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column("imported_by", sa.Uuid(), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["import_batch_id"], ["customer_import_batches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["imported_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )
    for name, columns in [
        ("ix_customers_external_id", ["external_id"]),
        ("ix_customers_name", ["name"]),
        ("ix_customers_normalized_name", ["normalized_name"]),
        ("ix_customers_organization_id", ["organization_id"]),
        ("ix_customers_owner_user_id", ["owner_user_id"]),
        ("ix_customers_kind", ["kind"]),
        ("ix_customers_stage", ["stage"]),
        ("ix_customers_contact_phone", ["contact_phone"]),
        ("ix_customers_imported_by", ["imported_by"]),
        ("ix_customers_org_stage", ["organization_id", "stage"]),
        ("ix_customers_org_kind", ["organization_id", "kind"]),
    ]:
        op.create_index(name, "customers", columns)


def downgrade() -> None:
    op.drop_table("customers")
    op.drop_table("customer_import_batches")
