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

    async def fetch_tv_aliases(self, tv_id: int) -> dict[str, Any]:
        """获取剧集英文译名与多语言别名（仅用于解释/辅助匹配，失败时安全降级）。"""
        aliases: list[str] = []
        en_name = ""
        translations_ok = False
        try:
            data, status_kind, _ = await self._request(
                f"/tv/{tv_id}",
                append_to_response="alternative_titles,translations",
            )
            if status_kind == "ok" and isinstance(data, dict):
                translations_ok = True
                for key in ("name", "original_name"):
                    v = str(data.get(key) or "").strip()
                    if v and v not in aliases:
                        aliases.append(v)
                alt_block = data.get("alternative_titles") or {}
                for item in alt_block.get("results") or []:
                    t = str(item.get("title") or "").strip() if isinstance(item, dict) else ""
                    if t and t not in aliases:
                        aliases.append(t)
                trans_block = data.get("translations") or {}
                for tr in trans_block.get("translations") or []:
                    if not isinstance(tr, dict):
                        continue
                    tr_data = tr.get("data") or {}
                    tname = str(tr_data.get("name") or tr_data.get("title") or "").strip()
                    if tname and tname not in aliases:
                        aliases.append(tname)
                    if str(tr.get("iso_639_1") or "").lower() == "en" and tname and not en_name:
                        en_name = tname
            if not en_name:
                en_data, en_status, _ = await self._request(f"/tv/{tv_id}", language="en-US")
                if en_status == "ok" and isinstance(en_data, dict):
                    translations_ok = True
                    en_name = str(en_data.get("name") or "").strip()
                    if en_name and en_name not in aliases:
                        aliases.append(en_name)
        except Exception:
            translations_ok = False
        return {
            "aliases": aliases,
            "en_name": en_name,
            "translations_ok": translations_ok,
        }

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

    # ── 电影（Movie）API ─────────────────────────────────────
    async def fetch_movie_detail(
        self, movie_id: int, language: str | None = None
    ) -> tuple[dict, str, str | None]:
        """获取电影详情并返回 `(data, status_kind, error_msg)`。"""
        params = {"language": language} if language else {}
        res, status_kind, err = await self._request(f"/movie/{movie_id}", **params)
        return (res if isinstance(res, dict) else {}), status_kind, err

    async def get_movie_detail(self, movie_id: int) -> dict:
        """获取电影详情字典。"""
        res = await self._get(f"/movie/{movie_id}")
        return res if isinstance(res, dict) else {}

    async def fetch_movie_release_dates(
        self, movie_id: int
    ) -> tuple[dict, str, str | None]:
        """获取电影多地区发行日期（按影院公映、数字版、实体蓝光等类型分类解析）。"""
        data, status_kind, err = await self._request(f"/movie/{movie_id}/release_dates")
        if status_kind != "ok" or not isinstance(data, dict):
            return {}, status_kind, err

        results = data.get("results") or []
        # 解析发行类型：1=首映, 2=有限院线, 3=院线公映, 4=数字发行(流媒体/WEB-DL), 5=实体发行(蓝光/DVD), 6=电视首播
        parsed_by_region: dict[str, list[dict]] = {}
        theatrical_dates: list[str] = []
        digital_dates: list[str] = []
        physical_dates: list[str] = []
        premiere_dates: list[str] = []

        for item in results:
            if not isinstance(item, dict):
                continue
            region = str(item.get("iso_3166_1") or "").upper()
            dates_list = item.get("release_dates") or []
            parsed_by_region[region] = []
            for d in dates_list:
                if not isinstance(d, dict):
                    continue
                rtype = int(d.get("type") or 0)
                raw_date = str(d.get("release_date") or "").strip()
                date_str = raw_date[:10] if len(raw_date) >= 10 else raw_date
                parsed_by_region[region].append({
                    "type": rtype,
                    "date": date_str,
                    "note": str(d.get("note") or ""),
                })
                if date_str:
                    if rtype == 1:
                        premiere_dates.append(date_str)
                    elif rtype in (2, 3):
                        theatrical_dates.append(date_str)
                    elif rtype == 4:
                        digital_dates.append(date_str)
                    elif rtype == 5:
                        physical_dates.append(date_str)

        summary = {
            "movie_id": movie_id,
            "by_region": parsed_by_region,
            "premiere_date": min(premiere_dates) if premiere_dates else None,
            "theatrical_date": min(theatrical_dates) if theatrical_dates else None,
            "digital_date": min(digital_dates) if digital_dates else None,
            "physical_date": min(physical_dates) if physical_dates else None,
        }
        return summary, "ok", None

    async def get_movie_release_dates(self, movie_id: int) -> dict:
        """获取电影多地区发行日期结构体。"""
        summary, _, _ = await self.fetch_movie_release_dates(movie_id)
        return summary

    async def fetch_movie_aliases(self, movie_id: int) -> dict[str, Any]:
        """获取电影多语言别名与英文名（用于资源检索与跨语言匹配）。"""
        aliases: list[str] = []
        en_name = ""
        translations_ok = False
        try:
            data, status_kind, _ = await self._request(
                f"/movie/{movie_id}",
                append_to_response="alternative_titles,translations",
            )
            if status_kind == "ok" and isinstance(data, dict):
                translations_ok = True
                for key in ("title", "original_title"):
                    v = str(data.get(key) or "").strip()
                    if v and v not in aliases:
                        aliases.append(v)
                alt_block = data.get("alternative_titles") or {}
                for item in alt_block.get("titles") or alt_block.get("results") or []:
                    t = str(item.get("title") or "").strip() if isinstance(item, dict) else ""
                    if t and t not in aliases:
                        aliases.append(t)
                trans_block = data.get("translations") or {}
                for tr in trans_block.get("translations") or []:
                    if not isinstance(tr, dict):
                        continue
                    tr_data = tr.get("data") or {}
                    tname = str(tr_data.get("title") or tr_data.get("name") or "").strip()
                    if tname and tname not in aliases:
                        aliases.append(tname)
                    if str(tr.get("iso_639_1") or "").lower() == "en" and tname and not en_name:
                        en_name = tname
            if not en_name:
                en_data, en_status, _ = await self._request(f"/movie/{movie_id}", language="en-US")
                if en_status == "ok" and isinstance(en_data, dict):
                    translations_ok = True
                    en_name = str(en_data.get("title") or "").strip()
                    if en_name and en_name not in aliases:
                        aliases.append(en_name)
        except Exception:
            translations_ok = False
        return {
            "aliases": aliases,
            "en_name": en_name,
            "translations_ok": translations_ok,
        }

    async def fetch_search_movie(
        self, query: str, language: str | None = None, year: int | str | None = None
    ) -> tuple[list[dict], str, str | None]:
        """搜索电影并返回 `(results, status_kind, error_msg)`。"""
        params: dict[str, Any] = {"query": query}
        if language:
            params["language"] = language
        if year and str(year)[:4].isdigit():
            params["year"] = int(str(year)[:4])
        data, status_kind, err = await self._request("/search/movie", **params)
        if isinstance(data, dict):
            return data.get("results", []) or [], status_kind, err
        return [], status_kind, err

    async def search_movie(self, query: str, year: int | str | None = None) -> list[dict]:
        """搜索电影列表。"""
        results, _, _ = await self.fetch_search_movie(query=query, year=year)
        return results

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
