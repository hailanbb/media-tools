"""操作日志模型 - 记录每一次归档/清理/下载等操作。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid4().hex
    )
    task_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("tasks.id"), nullable=True
    )
    season_record_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("season_records.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(
        String(64),
        comment="archive / clean_duplicate / download / repair_transfer / bind_name / relocate_wrong_season",
    )
    status: Mapped[str] = mapped_column(
        String(16), comment="pending / executing / success / failed / cancelled"
    )
    detail_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="操作详情 JSON（移动/删除的文件等）"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
