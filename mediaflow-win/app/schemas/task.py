"""任务与剧季相关 Schema."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskSummary(BaseModel):
    """任务汇总统计."""

    archive_ready: int = 0
    review_needed: int = 0
    download_needed: int = 0
    download_pushed: int = 0
    airing: int = 0
    archive_check: int = 0
    already_archived: int = 0
    total_seasons: int = 0
    pending_directory_groups: int = 0


class TaskCreate(BaseModel):
    """创建初检任务的请求体."""

    source_path: str
    name: str | None = None  # 为 None 时自动取 path basename
    media_type: str = "auto"


class TaskOut(BaseModel):
    """初检任务输出."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_path: str
    media_type: str
    status: str
    created_at: datetime
    updated_at: datetime
    summary: TaskSummary | None = None


class SeasonOut(BaseModel):
    """单季记录输出."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    show_name: str
    show_path: str
    tmdb_id: str | None
    season_number: int
    season_path: str
    total_episodes: int | None
    local_episode_count: int
    recognized_episodes: list[int]
    missing_episodes: list[int]
    duplicate_episodes: list[int]
    status: str
    status_detail: str | None
    archive_target_path: str | None
    season_finished: bool
    evidence: dict[str, Any] | None = None
    conflict: dict[str, Any] | None = None
    mapping: dict[str, Any] | None = None
    gap_fill_state: dict[str, Any] | None = None
    last_checked_at: datetime | None = None
