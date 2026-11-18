"""补缺站点规则与作品类型偏好模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GapFillSiteRule(Base):
    """补缺站点规则持久化（区分动漫/非动漫，各自具备独立勾选与有序优先级）。"""

    __tablename__ = "gap_fill_site_rules"

    # 主键规则: f"{instance_key}:{category}"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instance_key: Mapped[str] = mapped_column(String(64), index=True, comment="MoviePilot实例指纹")
    category: Mapped[str] = mapped_column(String(32), index=True, comment="anime / non_anime")

    # 有序站点 ID 列表 JSON: [site_id1, site_id2, ...]
    selected_site_ids_json: Mapped[str] = mapped_column(Text, default="[]", comment="已勾选站点有序列表")

    # 规则状态: active / paused / uninitialized
    status: Mapped[str] = mapped_column(String(32), default="uninitialized", comment="规则状态")

    # 版本号(用于乐观锁防多窗口并发覆盖)
    version: Mapped[int] = mapped_column(Integer, default=1, comment="规则版本号")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GapFillWorkPreference(Base):
    """作品级补缺类型偏好（用户人工指定动漫/非动漫，跨初检复用）。"""

    __tablename__ = "gap_fill_work_preferences"

    # 主键: f"{instance_key}:{work_key}"，work_key 优先为 "tmdb_{tmdb_id}"，缺失时为 "name_{clean_name}"
    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    instance_key: Mapped[str] = mapped_column(String(64), index=True)
    tmdb_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    show_name: Mapped[str] = mapped_column(String(512), index=True)

    # 用户指定的类型: anime / non_anime
    category: Mapped[str] = mapped_column(String(32), comment="anime / non_anime")
    source: Mapped[str] = mapped_column(String(32), default="manual", comment="manual")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
