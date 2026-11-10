"""MoviePilot v2 异步 API 客户端。

基于 tv-workflow-Python 已验证的 REST 调用模式，
改用 httpx.AsyncClient 实现全异步。
自动适配 X-API-KEY 与 JWT Bearer 认证。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("mediaflow.moviepilot")


def parse_season_str(s_str: Any) -> set[int]:
    """解析 MoviePilot 转移历史或元数据中的季度字符串（如 'S01', 'S01,S02', '1'）."""
    if not s_str:
        return set()
    nums: set[int] = set()
    for m in re.finditer(r"S?(\d+)", str(s_str), re.IGNORECASE):
        try:
            nums.add(int(m.group(1)))
        except (ValueError, TypeError):
            continue
    return nums


def parse_episode_str(ep_str: Any) -> set[int]:
    """解析 MoviePilot 转移历史中的集数字符串（如 'E15', 'E01-E06', '15', '第1-5集'）."""
    if not ep_str:
        return set()
    eps: set[int] = set()
    for m in re.finditer(
        r"(?:E|EP|第)?(\d+)(?:\s*[-~～至到_\ufffd]\s*(?:E|EP|第)?(\d+))?",
        str(ep_str),
        re.IGNORECASE,
    ):
        try:
            start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
                low, high = min(start, end), max(start, end)
                eps.update(range(low, high + 1))
            else:
                eps.add(start)
        except (ValueError, TypeError):
            continue
    return eps


class MoviePilotClient:
    """MoviePilot v2.x 异步客户端。"""

    def __init__(self) -> None:
        settings = get_settings()
        self.origin = settings.mp_url.rstrip("/")
        self.token = settings.mp_token
        self._client = httpx.AsyncClient(timeout=60.0, verify=False)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # JWT（以 ey 开头）使用 Bearer，其他使用 X-API-KEY
        if self.token.startswith("ey") or len(self.token) > 100:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["X-API-KEY"] = self.token
        return headers

    async def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.get(
            f"{self.origin}{path}", params=clean, headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> Any:
        resp = await self._client.post(
            f"{self.origin}{path}", json=json, headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.delete(
            f"{self.origin}{path}", params=clean, headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    # ── 媒体搜索 ─────────────────────────────────────────
    async def search_media(self, title: str, source: str = "douban") -> list[dict]:
        return await self._get("/api/v1/media/search", source=source, title=title)

    async def get_douban_detail(self, douban_id: str) -> dict:
        return await self._get(f"/api/v1/douban/{douban_id}")

    async def get_douban_media_info(self, douban_id: str, type_name: str = "电视剧") -> dict:
        return await self._get(f"/api/v1/media/douban:{douban_id}", type_name=type_name)

    # ── TMDB 剧集信息 ────────────────────────────────────
    async def get_tmdb_seasons(self, tmdb_id: int) -> list[dict]:
        return await self._get(f"/api/v1/tmdb/seasons/{tmdb_id}")

    async def get_tmdb_episodes(self, tmdb_id: int, season: int) -> list[dict]:
        return await self._get(f"/api/v1/tmdb/{tmdb_id}/{season}")

    async def get_media_info(self, tmdb_id: int) -> dict:
        return await self._get(f"/api/v1/media/tmdb:{tmdb_id}", type_name="电视剧")

    # ── 资源搜索 ──────────────────────────────────────────
    async def search_resources(self, tmdb_id: int | None = None, season: int | None = None, title: str | None = None) -> list[dict]:
        if tmdb_id and tmdb_id > 0:
            params: dict[str, Any] = {"mtype": "电视剧"}
            if season is not None:
                params["season"] = season
            if title:
                params["title"] = title
            res = await self._get(f"/api/v1/search/media/tmdb:{tmdb_id}", **params)
        else:
            keyword = title or ""
            if season is not None:
                keyword = f"{keyword} S{str(season).zfill(2)}"
            res = await self._get("/api/v1/search/title", keyword=keyword)

        data = res.get("data") if isinstance(res, dict) and "data" in res else res
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("results") or []
        raise RuntimeError("资源搜索返回未知格式")

    # ── 订阅管理 ──────────────────────────────────────────
    async def list_subscriptions(self) -> list[dict]:
        res = await self._get("/api/v1/subscribe/")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data") or []
        return []

    async def delete_subscription(self, sub_id: int) -> dict:
        res = await self._delete(f"/api/v1/subscribe/{sub_id}")
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(res.get("message") or "MoviePilot 退订业务处理失败")
        return res

    # ── 转移队列 ──────────────────────────────────────────
    async def get_transfer_queue(self) -> list[dict]:
        return await self._get("/api/v1/transfer/queue")

    # ── 转移历史管理 ──────────────────────────────────────
    async def get_transfer_history(self, title: str | None = None, page: int = 1, count: int = 100) -> list[dict]:
        """获取 MoviePilot 媒体整理（转移历史）记录列表。"""
        params: dict[str, Any] = {"page": page, "count": count}
        if title:
            params["title"] = title
        res = await self._get("/api/v1/history/transfer", **params)
        if isinstance(res, dict):
            data = res.get("data")
            if isinstance(data, dict):
                return data.get("list") or data.get("items") or []
            if isinstance(data, list):
                return data
        if isinstance(res, list):
            return res
        return []

    async def get_all_transfer_history_for_title(self, title: str) -> list[dict]:
        """获取指定剧集标题的全部转移历史（自动处理分页）。"""
        all_records: list[dict] = []
        page = 1
        page_size = 50
        while True:
            records = await self.get_transfer_history(title=title, page=page, count=page_size)
            if not records:
                break
            all_records.extend(records)
            if len(records) < page_size:
                break
            page += 1
            if page > 20:  # 安全上限 1000 条
                break
        return all_records

    async def delete_transfer_history(
        self, record_id: int, delete_src: bool = False, delete_dest: bool = False
    ) -> bool:
        """从 MoviePilot 媒体整理历史中仅删除指定记录（严格默认不删源/目标文件）。"""
        # 安全防御：强行限制 delete_src / delete_dest 必须为布尔，严防传错
        del_src = bool(delete_src)
        del_dst = bool(delete_dest)
        resp = await self._client.request(
            "DELETE",
            f"{self.origin}/api/v1/history/transfer",
            params={"deletesrc": del_src, "deletedest": del_dst},
            json={"id": int(record_id)},
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return bool(data.get("success", False))
        return True

    async def find_transfer_history(
        self,
        title: str,
        season: int | None = None,
        episodes: set[int] | list[int] | None = None,
    ) -> list[dict]:
        """按剧名、季号和集数查找重叠的转移历史记录。

        Args:
            title: 剧集标题（清洗后）
            season: 目标季号（如 1）
            episodes: 目标集数集合（如 {14}）。若为 None 或空，代表整季所有集数。
        """
        all_items = await self.get_all_transfer_history_for_title(title)
        if not all_items:
            return []

        target_season = season
        target_eps = set(episodes) if episodes is not None else None
        matched: list[dict] = []

        for item in all_items:
            # 1. 季号匹配
            if target_season is not None:
                item_seasons = parse_season_str(item.get("seasons"))
                if item_seasons and target_season not in item_seasons:
                    continue

            # 2. 集数匹配
            if target_eps is not None and len(target_eps) > 0:
                item_eps = parse_episode_str(item.get("episodes"))
                # 如果历史记录有明确集数，且与目标集数无交集，则不匹配
                if item_eps and not (item_eps & target_eps):
                    continue

            matched.append(item)

        return matched

    async def clear_transfer_history_for_episodes(
        self,
        title: str,
        season: int,
        episodes: set[int] | list[int] | None = None,
    ) -> dict[str, Any]:
        """针对指定剧集与评估下载的集数，查找并【仅删除转移记录】（绝对不删实体文件）。

        Returns:
            {"cleared_count": int, "records": list[dict], "errors": list[str]}
        """
        conflicting = await self.find_transfer_history(title=title, season=season, episodes=episodes)
        cleared: list[dict] = []
        errors: list[str] = []

        for item in conflicting:
            rec_id = item.get("id")
            if not rec_id:
                continue
            try:
                ok = await self.delete_transfer_history(rec_id, delete_src=False, delete_dest=False)
                if ok:
                    cleared.append({
                        "id": rec_id,
                        "title": item.get("title"),
                        "seasons": item.get("seasons"),
                        "episodes": item.get("episodes"),
                        "dest": item.get("dest"),
                    })
                    log.info(
                        "已安全清除 MoviePilot 冲突转移记录: id=%s (%s %s %s)",
                        rec_id, item.get("title"), item.get("seasons"), item.get("episodes")
                    )
                else:
                    errors.append(f"删除记录 {rec_id} 接口返回未成功")
            except Exception as e:
                err_msg = f"删除转移记录 {rec_id} 失败: {e}"
                log.warning(err_msg)
                errors.append(err_msg)

        return {
            "cleared_count": len(cleared),
            "records": cleared,
            "errors": errors,
        }

    async def manual_transfer(self, source_path: str, target_path: str | None = None, preview: bool = False) -> dict:
        payload: dict[str, Any] = {"in_path": source_path, "preview": preview}
        if target_path:
            payload["target"] = target_path
        return await self._post("/api/v1/transfer/manual", json=payload)

    # ── 下载 ──────────────────────────────────────────────
    async def submit_download(self, media_info: dict, torrent_info: dict) -> dict:
        body = {"media_in": media_info, "torrent_in": torrent_info, "downloader": None, "save_path": None}
        res = await self._post("/api/v1/download/", json=body)
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(res.get("message") or "MoviePilot 下载提交失败")
        return res

    async def get_active_downloads(self) -> list[dict]:
        return await self._get("/api/v1/download/")

    # ── 站点 ──────────────────────────────────────────────
    async def get_sites(self) -> list[dict]:
        res = await self._get("/api/v1/site/")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data") or []
        return []

    # ── 系统 ──────────────────────────────────────────────
    async def get_system_info(self) -> dict:
        try:
            res = await self._get("/api/v1/system/env")
            env = res.get("data") if isinstance(res, dict) and "data" in res else res
            version = env.get("VERSION") or env.get("APP_VERSION") or "v2.x" if isinstance(env, dict) else "v2.x"
            return {"version": version, "online": True}
        except Exception:
            return {"version": "unknown", "online": False}

    async def close(self) -> None:
        await self._client.aclose()
