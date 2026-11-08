"""系统信息、配置查询、目录浏览端点。"""

from __future__ import annotations

import time
from fastapi import APIRouter, Query

from app.config import get_settings

router = APIRouter()
_VERSION = "0.1.0"

# 连通性探测缓存 (状态, 过期时间戳)
_HEALTH_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 30.0  # 30 秒缓存


@router.get("/health")
async def health_check() -> dict:
    """健康检查，含各外部服务连通性状态（含 MP 代理的 TMDB 与豆瓣状态）。"""
    settings = get_settings()
    services: dict[str, str] = {}
    now = time.time()

    # 1. CD2 连通性
    if "cd2" in _HEALTH_CACHE and _HEALTH_CACHE["cd2"][1] > now:
        services["cd2"] = _HEALTH_CACHE["cd2"][0]
    elif settings.cd2_url and settings.cd2_token:
        try:
            from app.clients.clouddrive2 import CloudDrive2Client
            cd2 = CloudDrive2Client()
            info = await cd2.get_file_info("/")
            status = "online" if info else "error"
            await cd2.close()
        except Exception as e:
            status = f"error: {str(e)[:80]}"
        services["cd2"] = status
        _HEALTH_CACHE["cd2"] = (status, now + _CACHE_TTL)
    else:
        services["cd2"] = "not_configured"

    # 2. MoviePilot 连通性
    mp_online = False
    if "moviepilot" in _HEALTH_CACHE and _HEALTH_CACHE["moviepilot"][1] > now:
        services["moviepilot"] = _HEALTH_CACHE["moviepilot"][0]
        mp_online = services["moviepilot"].startswith("online")
    elif settings.mp_url and settings.mp_token:
        try:
            from app.clients.moviepilot import MoviePilotClient
            mp = MoviePilotClient()
            info = await mp.get_system_info()
            version = info.get("version", "?")
            mp_online = bool(info.get("online"))
            status = f"online (v{version})" if mp_online else "error"
            await mp.close()
        except Exception as e:
            status = f"error: {str(e)[:80]}"
        services["moviepilot"] = status
        _HEALTH_CACHE["moviepilot"] = (status, now + _CACHE_TTL)
    else:
        services["moviepilot"] = "not_configured"

    # 3. TMDB 通道连通性（优先支持独立 API Key，若无则经 MoviePilot 代理通道检测）
    if "tmdb" in _HEALTH_CACHE and _HEALTH_CACHE["tmdb"][1] > now:
        services["tmdb"] = _HEALTH_CACHE["tmdb"][0]
    elif settings.tmdb_api_key:
        services["tmdb"] = "online"
        _HEALTH_CACHE["tmdb"] = ("online", now + _CACHE_TTL)
    elif mp_online:
        try:
            from app.clients.moviepilot import MoviePilotClient
            mp = MoviePilotClient()
            # 探测 TMDB 代理服务
            seasons = await mp.get_tmdb_seasons(263311)
            await mp.close()
            status = "online" if isinstance(seasons, list) and len(seasons) > 0 else "error"
        except Exception:
            status = "error"
        services["tmdb"] = status
        _HEALTH_CACHE["tmdb"] = (status, now + _CACHE_TTL)
    else:
        services["tmdb"] = "not_configured"

    # 4. 豆瓣通道连通性（通过 MoviePilot 代理源探测）
    if "douban" in _HEALTH_CACHE and _HEALTH_CACHE["douban"][1] > now:
        services["douban"] = _HEALTH_CACHE["douban"][0]
    elif mp_online:
        try:
            from app.clients.moviepilot import MoviePilotClient
            mp = MoviePilotClient()
            # 探测豆瓣媒体检索服务
            douban_res = await mp.search_media("庆余年", source="douban")
            await mp.close()
            status = "online" if isinstance(douban_res, list) and len(douban_res) > 0 else "error"
        except Exception:
            status = "error"
        services["douban"] = status
        _HEALTH_CACHE["douban"] = (status, now + _CACHE_TTL)
    else:
        services["douban"] = "not_configured"

    return {"ok": True, "version": _VERSION, "services": services}


@router.get("/config/roots")
async def get_roots() -> dict:
    """返回已配置的源路径与归档路径。"""
    settings = get_settings()
    return {
        "source_root": settings.source_root,
        "archive_root": settings.archive_root,
        "library_root": settings.library_root,
    }


@router.get("/config/directories")
async def list_directories(path: str = Query(..., description="要浏览的目录路径")) -> dict:
    """前端文件夹浏览器 — 通过 CD2 列出指定路径下的子目录。"""
    settings = get_settings()
    if not settings.cd2_url or not settings.cd2_token:
        return {"path": path, "folders": [], "error": "CloudDrive2 未配置"}

    try:
        from app.clients.clouddrive2 import CloudDrive2Client
        cd2 = CloudDrive2Client()
        items = await cd2.list_subdirectories(path)
        await cd2.close()
        return {
            "path": path,
            "folders": [
                {"name": item["name"], "path": item["path"]}
                for item in items
            ],
        }
    except Exception as e:
        return {"path": path, "folders": [], "error": str(e)[:200]}



@router.get("/logs")
async def get_recent_logs(lines: int = Query(default=100, ge=10, le=1000)) -> dict:
    """读取最近的运行日志（支持前端即时查看或运维排查）。"""
    from pathlib import Path
    settings = get_settings()
    log_file = Path(settings.data_dir) / "logs" / "mediaflow.log"
    if not log_file.exists():
        return {"total_lines": 0, "lines": []}

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            recent = all_lines[-lines:]
        return {
            "total_lines": len(all_lines),
            "returned_lines": len(recent),
            "lines": [l.rstrip("\r\n") for l in recent],
        }
    except Exception as e:
        return {"total_lines": 0, "returned_lines": 0, "lines": [], "error": str(e)}
