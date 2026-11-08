"""任务模型 - 用户发起的一次媒体库扫描/整理任务。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid4().hex
    )
    name: Mapped[str] = mapped_column(String(512), comment="显示名称，通常为用户选择的文件夹名")
    source_path: Mapped[str] = mapped_column(String(1024), comment="用户选择的待检视目录路径")
    media_type: Mapped[str] = mapped_column(String(16), default="auto", comment="auto/tv/anime")
    status: Mapped[str] = mapped_column(
        String(16), default="pending", comment="pending/running/completed/failed"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="JSON 汇总统计")

    # ── 关系 ──────────────────────────────────────────────
    seasons = relationship(
        "SeasonRecord", back_populates="task", cascade="all, delete-orphan"
    )
