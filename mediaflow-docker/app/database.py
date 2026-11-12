"""异步数据库引擎与会话管理（SQLAlchemy 2.0 + aiosqlite）。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


_settings = get_settings()
_db_path = Path(_settings.data_dir) / "mediaflow.db"
_db_url = f"sqlite+aiosqlite:///{_db_path}"

engine = create_async_engine(_db_url, echo=_settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：提供异步数据库会话，请求结束后自动关闭。"""
    async with async_session() as session:
        yield session


def _migrate_legacy_columns(sync_conn) -> None:
    """为历史 SQLite 表按需幂等补齐新增列（防止升级后 no such column 报错）。"""
    migrations: dict[str, list[tuple[str, str]]] = {
        "season_records": [
            ("evidence_json", "TEXT"),
            ("conflict_json", "TEXT"),
            ("mapping_json", "TEXT"),
            ("last_checked_at", "DATETIME"),
        ],
    }
    for table_name, columns in migrations.items():
        rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row[1] for row in rows}
        for col_name, col_type in columns:
            if col_name not in existing:
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                )


async def init_db() -> None:
    """根据 Base.metadata 创建所有尚不存在的表并补齐历史迁移列。"""
    import app.models  # noqa: F401 - 确保所有 ORM 模型已注册到 Base.metadata

    Path(_settings.data_dir).mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_legacy_columns)
