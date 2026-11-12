"""跨条目季映射与单季条目改绑模型 - 支持可追溯、单季隔离与一键撤销。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SeasonMapping(Base):
    """单季维度的作品/季号映射记录。"""

    __tablename__ = "season_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(512), index=True)
    source_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_season: Mapped[int] = mapped_column(Integer, index=True)
    show_path: Mapped[str] = mapped_column(Text, default="")
    season_path: Mapped[str] = mapped_column(Text, default="", index=True)
    mapping_kind: Mapped[str] = mapped_column(String(48), default="cross_entry_alias")
    target_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_douban_id: Mapped[str] = mapped_column(String(64), default="")
    target_season: Mapped[int] = mapped_column(Integer, default=1)
    target_title: Mapped[str] = mapped_column(String(512), default="")
    target_original_title: Mapped[str] = mapped_column(String(512), default="")
    search_keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    archive_aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    numbering_mode: Mapped[str] = mapped_column(String(64), default="independent")
    episode_offset: Mapped[int] = mapped_column(Integer, default=0)
    expected_total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
