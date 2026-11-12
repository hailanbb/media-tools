"""季度记录模型 - 每条记录对应一部剧的一个季度。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SeasonRecord(Base):
    __tablename__ = "season_records"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid4().hex
    )
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"))
    show_name: Mapped[str] = mapped_column(String(512), comment="剧集名称")
    show_path: Mapped[str] = mapped_column(String(1024), comment="剧集根目录路径")
    tmdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    season_number: Mapped[int] = mapped_column(Integer)
    season_path: Mapped[str] = mapped_column(String(1024))
    total_episodes: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="来自 TMDB/豆瓣的总集数"
    )
    local_episode_count: Mapped[int] = mapped_column(Integer, comment="本地实际视频文件数")
    recognized_episodes_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="已识别集号 JSON 列表"
    )
    missing_episodes_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="缺失已播出集号 JSON 列表"
    )
    duplicate_episodes_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="重复集号 JSON 列表"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        comment="archive_ready / review_needed / download_needed / airing / unknown",
    )
    status_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="人类可读的状态说明"
    )
    archive_target_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="归档目标路径"
    )
    season_finished: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="该季是否已完结（全部播出）"
    )
    evidence_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="元数据与文件命名核对证据 JSON"
    )
    conflict_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="作品归属冲突或同名待确认状态 JSON"
    )
    mapping_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="跨条目季映射与编号口径 JSON"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="最近一次元数据或对账核对时间"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    # ── 关系 ──────────────────────────────────────────────
    task = relationship("Task", back_populates="seasons")
