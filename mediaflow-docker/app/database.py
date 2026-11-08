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


async def init_db() -> None:
    """根据 Base.metadata 创建所有尚不存在的表。"""
    Path(_settings.data_dir).mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
