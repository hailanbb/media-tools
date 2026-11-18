"""人工确认持久化模型 — 独立记录用户对作品归属、本季总集数与播出状态的确认事实。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ManualSeasonConfirmation(Base):
    """单季人工确认事实记录（来源标注 manual_confirmation，绝不伪装成 TMDB 官方缓存）。"""

    __tablename__ = "manual_season_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(512), index=True)
    task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    season_record_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    show_path: Mapped[str] = mapped_column(Text, default="")
    season_path: Mapped[str] = mapped_column(Text, default="", index=True)
    scope_paths_json: Mapped[str] = mapped_column(Text, default="[]")
    show_name: Mapped[str] = mapped_column(String(512), default="")
    tmdb_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    season_number: Mapped[int] = mapped_column(Integer, default=1, index=True)

    # 逐项确认事实（未确认字段保持未知/None）
    identity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broadcast_status: Mapped[str] = mapped_column(String(32), default="unknown")  # "finished" | "airing" | "unknown"
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="manual_confirmation")

    # 确认时已识别正片集号快照与后续新实质矛盾记录
    recognized_snapshot_json: Mapped[str] = mapped_column(Text, default="[]")
    contradiction_json: Mapped[str] = mapped_column(Text, default="{}")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
