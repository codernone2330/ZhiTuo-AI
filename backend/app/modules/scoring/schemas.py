import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ScorePreviewRequest(BaseModel):
    """对待入库的企业清单做评分预览（不写库，仅锁定锚点）。"""

    rows: list[dict[str, Any]] = Field(min_length=1, max_length=50000)
    region: str | None = Field(None, max_length=120)
    asOf: date | None = None
    topN: int | None = Field(None, ge=1, le=5000)


class ScoreRefreshRequest(BaseModel):
    """对已入库客户按其工商档案重新评分并落库。"""

    organizationId: uuid.UUID | None = None
    # 覆盖配置里的评分基准日（不传则用 model_config.json 的 as_of）。
    # 基准日固定时重复刷新是幂等的；显式改动它才会让分数随时间变化。
    asOf: date | None = None


class ScoreRefreshResult(BaseModel):
    total: int
    scored: int
    changed: int
    # 仅 scoreDetail 里的 asOf 等元数据被补正的条数（分数未变，不计入 changed）
    metadataUpdated: int = 0
    skipped: int
    tierCounts: dict[str, int]
    anchors: dict[str, float]
    asOf: str | None = None
    updatedAt: str
