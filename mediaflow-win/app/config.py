"""应用配置 - 基于 pydantic-settings，支持环境变量与 .env 文件。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

VERSION = "2.5.15"



class Settings(BaseSettings):
    """MediaFlow 全局配置。所有字段均可通过 MF_ 前缀的环境变量覆盖。"""

    model_config = SettingsConfigDict(env_prefix="MF_", env_file=".env")

    # ── 基础 ──────────────────────────────────────────────
    app_name: str = "MediaFlow"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: str = "/app/data"
    auth_token: str | None = None

    # ── CloudDrive2 连接 ──────────────────────────────────
    # 使用 gRPC-Web + Token 认证（与现有 tv-workflow 一致）
    cd2_url: str = ""
    cd2_token: str = ""

    # ── MoviePilot 连接 ───────────────────────────────────
    # 使用 X-API-KEY 认证
    mp_url: str = ""
    mp_token: str = ""

    # ── TMDB ──────────────────────────────────────────────
    tmdb_api_key: str = "ed74a00b26fb863a58d378b2e97131b1"

    # ── 路径映射 ──────────────────────────────────────────
    source_root: str = ""       # 源目录根路径，如 /115/Library_incs/nas
    archive_root: str = ""      # 归档目录根路径，如 /115/Library_incs/nas.finish
    library_root: str = ""      # 媒体库根路径（Emby/Jellyfin 扫描根），如 /115/Library_incs/nas.finish


@lru_cache
def get_settings() -> Settings:
    """返回全局单例配置，首次调用时从环境/文件加载。"""
    return Settings()  # type: ignore[call-arg]
