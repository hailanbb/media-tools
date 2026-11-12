"""同一 TMDB ID 多目录索引与组级人工确认状态模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DirectoryGroup(Base):
    """同一分类与同一 TMDB ID 下的多目录/异名目录组级复核与确认状态。"""

    __tablename__ = "directory_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid4().hex)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    tmdb_id: Mapped[str] = mapped_column(String(32), index=True)
    category_key: Mapped[str] = mapped_column(String(512), default="", index=True)
    group_kind: Mapped[str] = mapped_column(
        String(48), default="multi_source_dirs", index=True
    )  # multi_source_dirs / multi_archive_dirs / alias_dir_confirm
    title: Mapped[str] = mapped_column(String(512), default="")
    paths_signature: Mapped[str] = mapped_column(String(128), default="", index=True)
    paths_json: Mapped[str] = mapped_column(Text, default="[]")
    suggested_target_path: Mapped[str] = mapped_column(Text, default="")
    confirmed_target_path: Mapped[str] = mapped_column(Text, default="")
    confirmation_mode: Mapped[str] = mapped_column(
        String(48), default=""
    )  # "" / use_archive_target / intentional_coexist
    status: Mapped[str] = mapped_column(
        String(32), default="pending_confirm", index=True
    )  # pending_confirm / confirmed / coexist
    invalidated_reason: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    season_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
