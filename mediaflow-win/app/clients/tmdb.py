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

    async def _get(self, path: str, **extra_params: Any) -> Any:
        resp = await self._client.get(f"{self.BASE_URL}{path}", params=extra_params)
        resp.raise_for_status()
        return resp.json()

    async def get_tv_detail(self, tv_id: int) -> dict:
        """获取电视剧详情（含季列表概要）。"""
        return await self._get(f"/tv/{tv_id}")

    async def get_season_detail(self, tv_id: int, season_number: int) -> dict:
        """获取某一季的详细信息（含各集列表）。"""
        return await self._get(f"/tv/{tv_id}/season/{season_number}")

    async def get_episode_detail(
        self, tv_id: int, season_number: int, episode_number: int
    ) -> dict:
        """获取单集详情。"""
        return await self._get(
            f"/tv/{tv_id}/season/{season_number}/episode/{episode_number}"
        )

    async def search_tv(self, query: str) -> list[dict]:
        """搜索电视剧。"""
        data = await self._get("/search/tv", query=query)
        return data.get("results", [])

    async def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        await self._client.aclose()
