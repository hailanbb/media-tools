"""系统信息、配置查询、目录浏览端点。"""

from __future__ import annotations

import asyncio
import time
from fastapi import APIRouter, Body, HTTPException, Query

from app.config import get_settings, VERSION

router = APIRouter()
_VERSION = VERSION

# 连通性探测缓存 (状态, 过期时间戳)
_HEALTH_CACHE: dict[str, tuple[str, float]] = {}
_DOUBAN_DETAIL_CACHE: tuple[dict, float] | None = None
_CACHE_TTL = 30.0  # 30 秒缓存


@router.get("/version")
async def app_version():
    """不依赖外部健康探测的运行版本；首页与本接口共用唯一版本源。"""
    from fastapi.responses import JSONResponse
    return JSONResponse({"version": VERSION}, headers={"Cache-Control": "no-store"})


@router.get("/health")
async def health_check(
    force_refresh: bool = Query(default=False, description="强制跳过 30s 缓存重新探测"),
    scope: str = Query(default="all", description="探测范围：all | douban"),
) -> dict:
    """健康检查，含各外部服务连通性状态（支持并发探测与 scope=douban 极速专项重探）。"""
    global _DOUBAN_DETAIL_CACHE
    settings = get_settings()
    services: dict[str, str] = {}
    now = time.time()
    douban_only = str(scope or "all").strip().lower() == "douban"
    if force_refresh:
        if douban_only:
            _HEALTH_CACHE.pop("douban", None)
            _DOUBAN_DETAIL_CACHE = None
        else:
            _HEALTH_CACHE.clear()
            _DOUBAN_DETAIL_CACHE = None

    async def _check_cd2() -> str:
        if douban_only and "cd2" in _HEALTH_CACHE:
            return _HEALTH_CACHE["cd2"][0]
        if not douban_only and "cd2" in _HEALTH_CACHE and _HEALTH_CACHE["cd2"][1] > now:
            return _HEALTH_CACHE["cd2"][0]
        if douban_only:
            return "online" if (settings.cd2_url and settings.cd2_token) else "not_configured"
        if settings.cd2_url and settings.cd2_token:
            try:
                from app.clients.clouddrive2 import CloudDrive2Client
                cd2 = CloudDrive2Client()
                files = await cd2.list_files("/", force_refresh=True)
                status = "online" if isinstance(files, list) else "error"
                await cd2.close()
            except Exception as e:
                status = f"error: {str(e)[:80]}"
            _HEALTH_CACHE["cd2"] = (status, now + _CACHE_TTL)
            return status
        return "not_configured"

    async def _check_mp() -> tuple[str, bool]:
        if "moviepilot" in _HEALTH_CACHE and (douban_only or _HEALTH_CACHE["moviepilot"][1] > now):
            st = _HEALTH_CACHE["moviepilot"][0]
            return st, st.startswith("online")
        if settings.mp_url and settings.mp_token:
            try:
                from app.clients.moviepilot import MoviePilotClient
                mp = MoviePilotClient()
                info = await mp.get_system_info()
                version = info.get("version", "?")
                online = bool(info.get("online"))
                status = f"online (v{version})" if online else "error"
                await mp.close()
            except Exception as e:
                status = f"error: {str(e)[:80]}"
                online = False
            _HEALTH_CACHE["moviepilot"] = (status, now + _CACHE_TTL)
            return status, online
        return "not_configured", False

    async def _check_tmdb(mp_online_hint: bool | None = None) -> str:
        if douban_only and "tmdb" in _HEALTH_CACHE:
            return _HEALTH_CACHE["tmdb"][0]
        if not douban_only and "tmdb" in _HEALTH_CACHE and _HEALTH_CACHE["tmdb"][1] > now:
            return _HEALTH_CACHE["tmdb"][0]
        if douban_only:
            return "online" if settings.tmdb_api_key else "not_configured"
        if settings.tmdb_api_key:
            try:
                from app.clients.tmdb import TMDBClient
                t_client = TMDBClient()
                probe_ok = await t_client.verify_connectivity()
                await t_client.close()
                status = "online" if probe_ok else "error: TMDB API响应异常"
            except Exception as te:
                status = f"error: {str(te)[:50]}"
            _HEALTH_CACHE["tmdb"] = (status, now + _CACHE_TTL)
            return status
        if mp_online_hint:
            try:
                from app.clients.moviepilot import MoviePilotClient
                mp = MoviePilotClient()
                seasons = await mp.get_tmdb_seasons(263311)
                await mp.close()
                status = "online" if isinstance(seasons, list) and len(seasons) > 0 else "error"
            except Exception:
                status = "error"
            _HEALTH_CACHE["tmdb"] = (status, now + _CACHE_TTL)
            return status
        return "not_configured"

    if douban_only:
        services["cd2"] = await _check_cd2()
        mp_status, mp_online = await _check_mp()
        services["moviepilot"] = mp_status
        services["tmdb"] = await _check_tmdb(mp_online)
    else:
        # 并发探测 CD2、MoviePilot 与独立 TMDB，消除旧版全串行等待
        if settings.tmdb_api_key:
            cd2_st, (mp_status, mp_online), tmdb_st = await asyncio.gather(
                _check_cd2(), _check_mp(), _check_tmdb(None)
            )
        else:
            cd2_st, (mp_status, mp_online) = await asyncio.gather(_check_cd2(), _check_mp())
            tmdb_st = await _check_tmdb(mp_online)
        services["cd2"] = cd2_st
        services["moviepilot"] = mp_status
        services["tmdb"] = tmdb_st

    # 4. 豆瓣通道连通性（MP 主通道 + 故障自动切备选 CF 反代/Cookie + 恢复自动回切）
    douban_status: dict = {}
    if not force_refresh and "douban" in _HEALTH_CACHE and _HEALTH_CACHE["douban"][1] > now and _DOUBAN_DETAIL_CACHE and _DOUBAN_DETAIL_CACHE[1] > now:
        services["douban"] = _HEALTH_CACHE["douban"][0]
        douban_status = _DOUBAN_DETAIL_CACHE[0]
    else:
        from app.clients import douban_gateway
        mp_client = None
        if mp_online and settings.mp_url and settings.mp_token:
            from app.clients.moviepilot import MoviePilotClient
            mp_client = MoviePilotClient()
        try:
            douban_status = await douban_gateway.check_health(
                mp_client,
                mp_online=mp_online,
                force=force_refresh,
            )
        finally:
            if mp_client is not None:
                await mp_client.close()
        services["douban"] = str(douban_status.get("status") or "error")
        _HEALTH_CACHE["douban"] = (services["douban"], now + _CACHE_TTL)
        _DOUBAN_DETAIL_CACHE = (douban_status, now + _CACHE_TTL)

    return {"ok": True, "version": _VERSION, "services": services, "douban_status": douban_status}


@router.get("/douban/config")
async def get_douban_gateway_config() -> dict:
    """获取豆瓣备选通道（Cloudflare 反代 + Cookie-Editor 导入）的脱敏配置与状态。"""
    from app.clients import douban_gateway
    return {"ok": True, **douban_gateway.get_public_config()}


@router.post("/douban/config")
async def update_douban_gateway_config(payload: dict = Body(default={})) -> dict:
    """保存豆瓣备选通道配置并刷新健康缓存，可选同时执行三通道探测。"""
    global _DOUBAN_DETAIL_CACHE
    from app.clients import douban_gateway

    try:
        saved = douban_gateway.save_runtime_config(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _HEALTH_CACHE.pop("douban", None)
    _DOUBAN_DETAIL_CACHE = None

    if payload and payload.get("run_test"):
        settings = get_settings()
        mp_client = None
        if settings.mp_url and settings.mp_token:
            from app.clients.moviepilot import MoviePilotClient
            mp_client = MoviePilotClient()
        try:
            test_query = str(payload.get("test_query") or "").strip()
            saved["test"] = await douban_gateway.test_channels(mp_client, test_query=test_query)
        finally:
            if mp_client is not None:
                await mp_client.close()

    return saved


@router.post("/douban/test")
async def test_douban_gateway_channels(payload: dict = Body(default={})) -> dict:
    """立即实测 MoviePilot 主通道、Cloudflare 反代及 Cookie 直连三通道（支持草稿参数与自定义影视剧名称，不落盘）。"""
    from app.clients import douban_gateway

    settings = get_settings()
    mp_client = None
    if settings.mp_url and settings.mp_token:
        from app.clients.moviepilot import MoviePilotClient
        mp_client = MoviePilotClient()
    try:
        draft = payload if isinstance(payload, dict) and payload else None
        test_query = str((payload or {}).get("test_query") or "").strip() if isinstance(payload, dict) else ""
        return await douban_gateway.test_channels(mp_client, draft=draft, test_query=test_query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if mp_client is not None:
            await mp_client.close()


@router.get("/config/roots")
async def get_roots() -> dict:
    """返回已配置的源路径、归档路径与补缺参数。"""
    settings = get_settings()
    return {
        "source_root": settings.source_root,
        "archive_root": settings.archive_root,
        "library_root": settings.library_root,
        "gap_fill_min_seeders": settings.gap_fill_min_seeders,
        "gap_fill_extra_sites": settings.gap_fill_extra_sites,
        "version": _VERSION,
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
