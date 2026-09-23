import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CustomerImportBatch(TimestampMixin, Base):
    __tablename__ = "customer_import_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    imported_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    source_name: Mapped[str] = mapped_column(String(255), default="frontend-adapter")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    inserted_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="completed", index=True)


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_org_stage", "organization_id", "stage"),
        Index("ix_customers_org_kind", "organization_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(200), index=True)
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_name: Mapped[str] = mapped_column(String(80), default="待分配")
    kind: Mapped[str] = mapped_column(String(20), default="新客", index=True)
    customer_type: Mapped[str] = mapped_column(String(30), default="政企客户")
    carrier: Mapped[str | None] = mapped_column(String(50))
    industry: Mapped[str] = mapped_column(String(100), default="待分类")
    province: Mapped[str | None] = mapped_column(String(50))
    city: Mapped[str | None] = mapped_column(String(50))
    district: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(String(500))
    contact_name: Mapped[str | None] = mapped_column(String(80))
    contact_phone: Mapped[str | None] = mapped_column(String(50), index=True)
    need: Mapped[str] = mapped_column(Text, default="需求待识别")
    stage: Mapped[str] = mapped_column(String(30), default="线索入池", index=True)
    potential: Mapped[str] = mapped_column(String(10), default="中")
    score: Mapped[int] = mapped_column(Integer, default=60)
    next_action: Mapped[str] = mapped_column(String(500), default="完成首次触达")
    is_qian_bai_wan_group: Mapped[bool] = mapped_column(Boolean, default=False)
    is_key_account: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(255), default="手工录入")
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer_import_batches.id", ondelete="SET NULL"), nullable=True
    )
    imported_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomerRequest(TimestampMixin, Base):
    __tablename__ = "customer_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), index=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    requester_org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    approver_org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    before_data: Mapped[dict] = mapped_column(JSON, default=dict)
    after_data: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    review_comment: Mapped[str | None] = mapped_column(String(300))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
