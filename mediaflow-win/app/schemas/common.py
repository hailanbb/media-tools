"""通用响应模型."""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class StatusResponse(BaseModel):
    """通用状态响应."""

    ok: bool
    message: str = ""


class PaginatedResponse(BaseModel, Generic[T]):
    """分页响应包装器."""

    items: list[T]
    total: int
    page: int = 1
    page_size: int = 50
