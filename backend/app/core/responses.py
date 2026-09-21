from math import ceil
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageData(BaseModel, Generic[T]):
    items: list[T]
    page: int = Field(ge=1)
    pageSize: int = Field(ge=1, le=200)
    total: int = Field(ge=0)
    totalPages: int = Field(ge=0)

    @classmethod
    def build(cls, items: list[T], page: int, page_size: int, total: int):
        return cls(
            items=items,
            page=page,
            pageSize=page_size,
            total=total,
            totalPages=ceil(total / page_size) if total else 0,
        )


class SuccessResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T
    traceId: str | None = None
