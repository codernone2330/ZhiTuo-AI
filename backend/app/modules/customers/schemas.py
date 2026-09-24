import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CustomerImportRow(BaseModel):
    externalId: str | None = Field(None, max_length=100)
    name: str = Field(min_length=2, max_length=200)
    organizationId: uuid.UUID
    ownerName: str | None = Field(None, max_length=80)
    kind: str = "新客"
    customerType: str = "政企客户"
    carrier: str | None = Field(None, max_length=50)
    industry: str = Field("待分类", max_length=100)
    province: str | None = Field(None, max_length=50)
    city: str | None = Field(None, max_length=50)
    area: str | None = Field(None, max_length=50)
    address: str | None = Field(None, max_length=500)
    contact: str | None = Field(None, max_length=80)
    phone: str | None = Field(None, max_length=50)
    need: str = Field("需求待识别", max_length=2000)
    stage: str = Field("线索入池", max_length=30)
    potential: str = Field("中", max_length=10)
    score: int = Field(60, ge=0, le=100)
    nextAction: str = Field("完成首次触达", max_length=500)
    isQianBaiWanGroup: bool | str = False
    isKeyAccount: bool | str = False
    source: str = Field("前端导入", max_length=255)
    createdAt: datetime | None = None
    reasons: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    lastContact: datetime | None = None
    stageHistory: list[dict] = Field(default_factory=list)
    crmFlow: list[dict] = Field(default_factory=list)
    assignmentReason: str | None = Field(None, max_length=500)
    assignmentConfidence: str | None = Field(None, max_length=80)

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"新客", "猎户"}:
            raise ValueError("线索类型只能是新客或猎户")
        return value


class CustomerImportRequest(BaseModel):
    sourceName: str = Field("frontend-adapter", max_length=255)
    rows: list[CustomerImportRow] = Field(min_length=1, max_length=1000)


class CustomerUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    industry: str = Field(max_length=100)
    contact: str | None = Field(None, max_length=80)
    phone: str | None = Field(None, max_length=50)
    need: str = Field(min_length=1, max_length=2000)
    stage: str = Field(max_length=30)
    potential: str = Field(max_length=10)
    score: int = Field(ge=0, le=100)
    nextAction: str = Field(min_length=1, max_length=500)
    isQianBaiWanGroup: bool | str
    isKeyAccount: bool | str
    stageHistory: list[dict] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    stageReason: str = Field("", max_length=300)
    ownershipRequest: "CustomerOwnershipRequest | None" = None
    version: int = Field(ge=1)


class CustomerOwnershipRequest(BaseModel):
    targetOrganizationId: uuid.UUID
    targetOwnerName: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=5, max_length=300)


class CustomerRequestCreate(BaseModel):
    kind: Literal["ownership", "delete"]
    reason: str = Field(min_length=5, max_length=300)
    targetOrganizationId: uuid.UUID | None = None
    targetOwnerName: str | None = Field(None, max_length=80)


class CustomerRequestDecision(BaseModel):
    approve: bool
    comment: str = Field("", max_length=300)
