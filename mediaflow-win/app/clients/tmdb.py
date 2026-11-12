"""TMDB 客户端 — 直接调用 TMDB API v3 获取剧集信息。

作为 MoviePilot 的补充数据源，主要用于：
- 确认剧集总集数
- 判断季度是否完结
- 获取各集播出日期
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class TMDBClient:
    """TMDB v3 API 异步客户端。"""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.tmdb_api_key
        self._client = httpx.AsyncClient(
            timeout=15.0,
            params={"api_key": self.api_key, "language": "zh-CN"},
        )

    async def _request(self, path: str, **extra_params: Any) -> tuple[Any, str, str | None]:
        """请求 TMDB API 并明确区分成功、未收录 (404) 与网络/服务端故障。"""
        try:
            resp = await self._client.get(f"{self.BASE_URL}{path}", params=extra_params)
            if resp.status_code == 404:
                return None, "not_found", "TMDB 返回 404（条目或季不存在）"
            resp.raise_for_status()
            return resp.json(), "ok", None
        except httpx.TimeoutException as exc:
            return None, "error", f"TMDB 请求超时: {exc}"
        except httpx.HTTPStatusError as exc:
            return None, "error", f"TMDB HTTP {exc.response.status_code}"
        except Exception as exc:
            return None, "error", f"TMDB 请求异常: {exc}"

    async def _get(self, path: str, **extra_params: Any) -> Any:
        data, _, _ = await self._request(path, **extra_params)
        return data

    async def fetch_tv_detail(
        self, tv_id: int, language: str | None = None
    ) -> tuple[dict, str, str | None]:
        """获取电视剧详情并返回 `(data, status_kind, error_msg)`。"""
        params = {"language": language} if language else {}
        res, status_kind, err = await self._request(f"/tv/{tv_id}", **params)
        return (res if isinstance(res, dict) else {}), status_kind, err

    async def fetch_season_detail(
        self, tv_id: int, season_number: int, language: str | None = None
    ) -> tuple[dict, str, str | None]:
        """获取单季详情并返回 `(data, status_kind, error_msg)`。"""
        params = {"language": language} if language else {}
        res, status_kind, err = await self._request(
            f"/tv/{tv_id}/season/{season_number}", **params
        )
        return (res if isinstance(res, dict) else {}), status_kind, err

    async def fetch_search_tv(
        self, query: str, language: str | None = None
    ) -> tuple[list[dict], str, str | None]:
        """搜索电视剧并返回 `(results, status_kind, error_msg)`。"""
        params: dict[str, Any] = {"query": query}
        if language:
            params["language"] = language
        data, status_kind, err = await self._request("/search/tv", **params)
        if isinstance(data, dict):
            return data.get("results", []) or [], status_kind, err
        return [], status_kind, err

    async def get_tv_detail(self, tv_id: int) -> dict:
        """获取电视剧详情（含季列表概要）。"""
        res = await self._get(f"/tv/{tv_id}")
        return res if isinstance(res, dict) else {}

    async def get_season_detail(self, tv_id: int, season_number: int) -> dict:
        """获取某一季的详细信息（含各集列表）。"""
        res = await self._get(f"/tv/{tv_id}/season/{season_number}")
        return res if isinstance(res, dict) else {}

    async def get_episode_detail(
        self, tv_id: int, season_number: int, episode_number: int
    ) -> dict:
        """获取单集详情。"""
        res = await self._get(
            f"/tv/{tv_id}/season/{season_number}/episode/{episode_number}"
        )
        return res if isinstance(res, dict) else {}

    async def search_tv(self, query: str) -> list[dict]:
        """搜索电视剧。"""
        data = await self._get("/search/tv", query=query)
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    async def verify_connectivity(self) -> bool:
        """真实探测 TMDB API 连通性 (P2)."""
        try:
            data = await self._get("/configuration")
            return isinstance(data, dict) and bool(data)
        except Exception:
            return False

    async def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        await self._client.aclose()
