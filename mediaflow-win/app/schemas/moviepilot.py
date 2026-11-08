"""MoviePilot 代理相关 Schema."""

from pydantic import BaseModel


class SubscriptionItem(BaseModel):
    """MP 订阅条目."""

    id: int | str
    name: str
    year: str | None = None
    media_type: str  # tv / movie
    status: str  # active / completed
    season: int | None = None
    total_episodes: int | None = None
    current_episodes: int | None = None


class TransferQueueItem(BaseModel):
    """MP 转移队列条目."""

    id: int | str
    title: str
    filename: str
    status: str  # pending / stuck / completed / failed
    source_path: str | None = None
    target_path: str | None = None
    error_message: str | None = None


class ResourceSearchResult(BaseModel):
    """MP 资源搜索结果."""

    title: str
    size_bytes: int | None = None
    seeders: int | None = None
    site: str | None = None
    media_type: str | None = None
    season: int | None = None
    episodes: list[int] | None = None
    has_chinese_subtitle: bool = False
    download_url: str | None = None  # 仅服务端使用，不暴露给前端
