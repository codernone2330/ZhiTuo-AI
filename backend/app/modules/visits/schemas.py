from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

VisitMethod = Literal["上门拜访", "电话沟通", "视频会议", "联合拜访"]
VisitStatus = Literal["pending", "completed", "canceled"]


class VisitCreate(BaseModel):
    customerId: str = Field(min_length=1, max_length=100)
    ownerName: str = Field(min_length=1, max_length=80)
    time: datetime
    method: VisitMethod
    purpose: str = Field(min_length=2, max_length=200)
    notes: str = Field("", max_length=2000)
    source: str = Field("手工创建", max_length=100)
    overrideReason: str = Field("", max_length=300)


class VisitUpdate(BaseModel):
    version: int = Field(ge=1)
    ownerName: str = Field(min_length=1, max_length=80)
    time: datetime
    method: VisitMethod
    purpose: str = Field(min_length=2, max_length=200)
    notes: str = Field("", max_length=2000)


class VisitCancel(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=300)


class VisitComplete(BaseModel):
    version: int = Field(ge=1)
    outcome: Literal["形成明确商机", "需要继续跟进", "客户暂缓", "暂无需求", "已达成合作"]
    stage: Literal["线索入池", "已联系", "已拜访", "方案沟通", "商机立项", "已成交"]
    notes: str = Field(min_length=2, max_length=2000)
    nextAction: str = Field("", max_length=500)
    dealProduct: str = Field("", max_length=100)
    dealAmount: Decimal = Field(Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    dealRemark: str = Field("", max_length=300)
    aiStructured: dict | None = None

    @model_validator(mode="after")
    def validate_deal(self):
        if self.outcome == "已达成合作" or self.stage == "已成交":
            if not self.dealProduct.strip() or self.dealAmount <= 0:
                raise ValueError("成交必须填写产品和大于 0 的金额")
        return self


class VisitImportRow(VisitCreate):
    externalId: str = Field(min_length=1, max_length=100)
    status: VisitStatus = "pending"
    stage: str = Field("线索入池", max_length=30)
    createdAt: datetime | None = None
    createdBy: str = Field("演示数据迁移", max_length=80)
    canceledAt: datetime | None = None
    canceledBy: str | None = Field(None, max_length=80)
    cancelReason: str | None = Field(None, max_length=300)
    completedAt: datetime | None = None
    outcome: str | None = Field(None, max_length=30)
    nextAction: str | None = Field(None, max_length=500)
    dealProduct: str | None = Field(None, max_length=100)
    dealAmount: Decimal = Field(Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    dealRemark: str | None = Field(None, max_length=300)
    aiStructured: dict | None = None
    simulated: bool = False


class VisitImportRequest(BaseModel):
    rows: list[VisitImportRow] = Field(min_length=1, max_length=2000)
