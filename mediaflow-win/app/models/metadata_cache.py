"""元数据持久化快照缓存 - 隔离空结果与网络超时，避免误覆盖已核实元数据。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MetadataCache(Base):
    """TMDB/豆瓣季与剧集元数据缓存条目。"""

    __tablename__ = "metadata_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    media_id: Mapped[str] = mapped_column(String(64), index=True)
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
