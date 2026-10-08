import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ScoreAnchorLock(TimestampMixin, Base):
    """评分模型锚点锁（按区域一条）。

    为什么必须持久化（见 handoff `04_开发集成指南.md`）：
    它保证**同一区域多次评分结果一致**。若不落盘，每次请求都重新标定，
    会出现「今天 A 公司 82 分、明天 79 分」的诡异现象。

    落盘策略：按 `region` 存一条记录，`region` 变更时才重标。
    """

    __tablename__ = "scoring_anchor_locks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    anchor_mode: Mapped[str] = mapped_column(String(20), default="auto")
    size_anchor: Mapped[float] = mapped_column(Float, default=0.0)
    cap_anchor: Mapped[float] = mapped_column(Float, default=0.0)
    size_pct: Mapped[int] = mapped_column(Integer, default=95)
    cap_pct: Mapped[int] = mapped_column(Integer, default=95)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    calibrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def as_lock(self) -> dict:
        """转成引擎 resolve_anchor 需要的锁结构。"""
        return {"size_anchor": self.size_anchor, "cap_anchor": self.cap_anchor}
