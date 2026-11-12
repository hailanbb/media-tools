"""逐集补缺持久化追踪与批次运行摘要模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GapFillEpisode(Base):
    """逐集维度的补缺意图、受理回执、对账与入库状态。"""

    __tablename__ = "gap_fill_episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    season_record_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    season_path: Mapped[str] = mapped_column(Text, index=True)
    show_name: Mapped[str] = mapped_column(String(512), default="")
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    search_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_number: Mapped[int] = mapped_column(Integer, index=True)
    search_season_number: Mapped[int] = mapped_column(Integer, default=0)
    numbering_mode: Mapped[str] = mapped_column(String(64), default="standard")
    identity_key: Mapped[str] = mapped_column(String(256), index=True)
    episode_number: Mapped[int] = mapped_column(Integer, index=True)
    batch_run_id: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(40), index=True, default="intent_submitting")
    outcome_code: Mapped[str] = mapped_column(String(48), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    mp_download_id: Mapped[str] = mapped_column(String(128), default="")
    torrent_hash: Mapped[str] = mapped_column(String(128), default="")
    torrent_title: Mapped[str] = mapped_column(Text, default="")
    site_id: Mapped[str] = mapped_column(String(64), default="")
    site_name: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    seeders: Mapped[int] = mapped_column(Integer, default=0)
    covered_episodes_json: Mapped[str] = mapped_column(Text, default="[]")
    search_diag_json: Mapped[str] = mapped_column(Text, default="{}")
    is_legacy_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GapFillSeasonRun(Base):
    """季维度的最近补缺推送快照与搜索诊断明细（持久化供刷新、重启恢复）。"""

    __tablename__ = "gap_fill_season_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    season_record_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    season_path: Mapped[str] = mapped_column(Text, index=True)
    show_name: Mapped[str] = mapped_column(String(512), default="")
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_number: Mapped[int] = mapped_column(Integer)
    identity_key: Mapped[str] = mapped_column(String(256), index=True)
    batch_run_id: Mapped[str] = mapped_column(String(64), default="")
    outcome_status: Mapped[str] = mapped_column(String(48), default="")
    summary_text: Mapped[str] = mapped_column(Text, default="")
    target_episodes_json: Mapped[str] = mapped_column(Text, default="[]")
    accepted_episodes_json: Mapped[str] = mapped_column(Text, default="[]")
    unverified_episodes_json: Mapped[str] = mapped_column(Text, default="[]")
    failed_episodes_json: Mapped[str] = mapped_column(Text, default="{}")
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    max_seeders_seen: Mapped[int] = mapped_column(Integer, default=0)
    min_seeders_required: Mapped[int] = mapped_column(Integer, default=2)
    sites_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    search_diag_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
