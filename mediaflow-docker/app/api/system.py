"""系统信息、配置查询、目录浏览端点。"""

from __future__ import annotations

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
async def health_check(force_refresh: bool = Query(default=False, description="强制跳过 30s 缓存重新探测")) -> dict:
    """健康检查，含各外部服务连通性状态（含 MP 代理的 TMDB 与豆瓣主备通道状态）。"""
    global _DOUBAN_DETAIL_CACHE
    settings = get_settings()
    services: dict[str, str] = {}
    now = time.time()
    if force_refresh:
        _HEALTH_CACHE.clear()
        _DOUBAN_DETAIL_CACHE = None

    # 1. CD2 连通性
    if "cd2" in _HEALTH_CACHE and _HEALTH_CACHE["cd2"][1] > now:
        services["cd2"] = _HEALTH_CACHE["cd2"][0]
    elif settings.cd2_url and settings.cd2_token:
        try:
            from app.clients.clouddrive2 import CloudDrive2Client
            cd2 = CloudDrive2Client()
            # 真实通过 gRPC-Web 发送网络 RPC 请求，杜绝本地硬编码假在线
            files = await cd2.list_files("/", force_refresh=True)
            status = "online" if isinstance(files, list) else "error"
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
        try:
            from app.clients.tmdb import TMDBClient
            t_client = TMDBClient()
            probe_ok = await t_client.verify_connectivity()
            await t_client.close()
            status = "online" if probe_ok else "error: TMDB API响应异常"
        except Exception as te:
            status = f"error: {str(te)[:50]}"
        services["tmdb"] = status
        _HEALTH_CACHE["tmdb"] = (status, now + _CACHE_TTL)
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
            saved["test"] = await douban_gateway.test_channels(mp_client)
        finally:
            if mp_client is not None:
                await mp_client.close()

    return saved


@router.post("/douban/test")
async def test_douban_gateway_channels(payload: dict = Body(default={})) -> dict:
    """立即实测 MoviePilot 主通道、Cloudflare 反代及 Cookie 直连三通道（支持草稿测试，不落盘）。"""
    from app.clients import douban_gateway

    settings = get_settings()
    mp_client = None
    if settings.mp_url and settings.mp_token:
        from app.clients.moviepilot import MoviePilotClient
        mp_client = MoviePilotClient()
    try:
        draft = payload if isinstance(payload, dict) and payload else None
        return await douban_gateway.test_channels(mp_client, draft=draft)
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
