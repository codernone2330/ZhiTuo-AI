import uuid

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{2,80}$")
    employeeNo: str = Field(min_length=2, max_length=80)
    displayName: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    organizationId: uuid.UUID
    roleCode: str = Field(min_length=2, max_length=50)


class UserUpdate(BaseModel):
    displayName: str | None = Field(default=None, min_length=1, max_length=80)
    employeeNo: str | None = Field(default=None, min_length=2, max_length=80)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    organizationId: uuid.UUID | None = None
    roleCode: str | None = Field(default=None, min_length=2, max_length=50)
    isActive: bool | None = None
