import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Visit(TimestampMixin, Base):
    __tablename__ = "visits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_name: Mapped[str] = mapped_column(String(80), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    method: Mapped[str] = mapped_column(String(30))
    purpose: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    stage: Mapped[str] = mapped_column(String(30), default="线索入池")
    source: Mapped[str] = mapped_column(String(100), default="手工创建")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    updated_by: Mapped[str | None] = mapped_column(String(80))
    canceled_by: Mapped[str | None] = mapped_column(String(80))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(30))
    next_action: Mapped[str | None] = mapped_column(String(500))
    deal_product: Mapped[str | None] = mapped_column(String(100))
    deal_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    deal_remark: Mapped[str | None] = mapped_column(String(300))
    override_reason: Mapped[str | None] = mapped_column(String(300))
    conflict_task_id: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    extra_data: Mapped[dict] = mapped_column(JSON, default=dict)
