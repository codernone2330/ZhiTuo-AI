import uuid
from typing import Literal

from pydantic import BaseModel, Field

OrganizationLevel = Literal["group", "province", "city", "district", "department"]


class OrganizationItem(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    level: OrganizationLevel
    parentId: uuid.UUID | None
    path: str
    province: str | None
    city: str | None
    district: str | None
    isActive: bool
    children: list["OrganizationItem"] = Field(default_factory=list)
