"""媒体整理修复数据模型 — 持久化接管基线、文件修复项、执行计划与复用规则。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TransferRepairBaseline(Base):
    """MoviePilot 媒体整理修复接管基线与全局自动化设置。"""

    __tablename__ = "transfer_repair_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(128), default="default", index=True)
    takeover_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    baseline_history_id: Mapped[int] = mapped_column(Integer, default=0, comment="首次启用时的最大历史失败 ID")
    last_scanned_history_id: Mapped[int] = mapped_column(Integer, default=0, comment="最近一次扫描到的最大历史 ID")
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用新失败自动发现")
    auto_repair_enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否开启高确定性自动修复")
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1, comment="执行并发限制")
    batch_quota: Mapped[int] = mapped_column(Integer, default=20, comment="每轮最大提交文件数")
    retry_budget: Mapped[int] = mapped_column(Integer, default=3, comment="单计划最大重试预算")
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=10, comment="扫描周期(分钟)")
    config_json: Mapped[str] = mapped_column(Text, default="{}", comment="扩展设置快照")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransferRepairItem(Base):
    """以真实源文件为粒度的整理失败修复项（重试、幂等与核验单位）。"""

    __tablename__ = "transfer_repair_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid4().hex)
    instance_id: Mapped[str] = mapped_column(String(128), default="default", index=True)
    source_path: Mapped[str] = mapped_column(Text, index=True, comment="规范化源文件完整路径")
    file_basename: Mapped[str] = mapped_column(String(512), default="", index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    file_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage: Mapped[str] = mapped_column(String(64), default="")
    file_id: Mapped[str] = mapped_column(String(128), default="")

    # 关联 MP 历史与原始错误
    mp_history_ids_json: Mapped[str] = mapped_column(Text, default="[]", comment="关联的历史 ID 列表")
    latest_history_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    raw_error: Mapped[str] = mapped_column(Text, default="")
    error_category: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    main_block_reason: Mapped[str] = mapped_column(Text, default="", comment="主阻塞原因")

    # 识别与参数推导事实
    title: Mapped[str] = mapped_column(String(512), default="", index=True, comment="推导或确认的作品标题")
    media_type: Mapped[str] = mapped_column(String(32), default="tv", index=True, comment="tv / movie")
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    episode_numbers_json: Mapped[str] = mapped_column(Text, default="[]", comment="集号列表 [11, 12]")
    part: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分段标识 如 part1/part2/上/下")
    is_special: Mapped[bool] = mapped_column(Boolean, default=False)
    special_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 目标与执行方案
    target_storage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True, comment="预期落地目标路径")
    plan_json: Mapped[str] = mapped_column(Text, default="{}", comment="完整执行计划 JSON")
    plan_hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    # 状态生命周期
    # discovered / diagnosing / ready / needs_confirmation / waiting_source /
    # waiting_environment / conflict / protected / submitted / verifying / resolved / skipped
    status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)
    resolution_code: Mapped[str | None] = mapped_column(
        String(48), nullable=True, index=True, comment="repaired / already_present_verified / superseded_by_success / user_disposition"
    )
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="source_missing 等")

    # 执行与核验记录
    last_execution_json: Mapped[str] = mapped_column(Text, default="{}")
    verification_json: Mapped[str] = mapped_column(Text, default="{}")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 保护状态
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    protect_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransferRepairRule(Base):
    """媒体整理智能经验复用规则 — 记录用户对编号体系、季映射与发布模板的选择。"""

    __tablename__ = "transfer_repair_rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid4().hex)
    scope_kind: Mapped[str] = mapped_column(
        String(48), default="this_package", index=True, comment="this_run / this_package / this_dir_template"
    )
    scope_key: Mapped[str] = mapped_column(String(512), index=True, comment="作用域特征键 如发布模板特征或目录前缀")
    show_title: Mapped[str] = mapped_column(String(512), default="")
    media_type: Mapped[str] = mapped_column(String(32), default="tv")
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    season_number: Mapped[int] = mapped_column(Integer, default=1)
    numbering_mode: Mapped[str] = mapped_column(String(64), default="standard", comment="standard / offset / absolute")
    episode_offset: Mapped[int] = mapped_column(Integer, default=0)
    special_handling: Mapped[str] = mapped_column(String(64), default="special")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
