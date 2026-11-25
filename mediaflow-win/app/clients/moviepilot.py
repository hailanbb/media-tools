"""MoviePilot v2 异步 API 客户端。

基于 tv-workflow-Python 已验证的 REST 调用模式，
改用 httpx.AsyncClient 实现全异步。
自动适配 X-API-KEY 与 JWT Bearer 认证。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

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

    @staticmethod
    def _raise_for_status_with_detail(resp: httpx.Response) -> None:
        if not resp.is_error:
            return
        detail = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail = str(data.get("detail") or data.get("message") or "").strip()
        except Exception:
            pass
        if not detail:
            try:
                raw_txt = str(resp.text or "").strip()
                if raw_txt and not raw_txt.lstrip().startswith("<"):
                    detail = raw_txt[:220]
            except Exception:
                pass
        req_path = resp.url.path if getattr(resp, "url", None) is not None else ""
        if detail:
            msg = f"MoviePilot HTTP {resp.status_code} ({req_path}): {detail}"
        else:
            msg = f"MoviePilot HTTP {resp.status_code} {resp.reason_phrase or ''} ({req_path})".strip()
        raise httpx.HTTPStatusError(msg, request=resp.request, response=resp)

    async def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.get(
            f"{self.origin}{path}", params=clean, headers=self._headers()
        )
        self._raise_for_status_with_detail(resp)
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> Any:
        resp = await self._client.post(
            f"{self.origin}{path}", json=json, headers=self._headers()
        )
        self._raise_for_status_with_detail(resp)
        return resp.json()

    async def _delete(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.delete(
            f"{self.origin}{path}", params=clean, headers=self._headers()
        )
        self._raise_for_status_with_detail(resp)
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

    async def get_media_info(self, tmdb_id: int, type_name: str = "电视剧") -> dict:
        return await self._get(f"/api/v1/media/tmdb:{tmdb_id}", type_name=type_name)

    # ── 资源搜索 ──────────────────────────────────────────
    @staticmethod
    def _format_sites_param(sites: list[int | str] | tuple[int | str, ...] | set[int | str] | str | None) -> str | None:
        if sites is None:
            return None
        if isinstance(sites, str):
            parts = [p.strip() for p in sites.split(",") if p.strip()]
            return ",".join(parts) if parts else None
        # 保持传入的原始顺序，不按 ID 升序重排，严格保留优先级顺序
        ids: list[str] = []
        seen: set[int] = set()
        for s in sites:
            s_str = str(s).strip()
            if s_str.lstrip("-").isdigit():
                val = int(s_str)
                if val not in seen:
                    seen.add(val)
                    ids.append(str(val))
        return ",".join(ids) if ids else None

    async def search_resources(
        self,
        tmdb_id: int | None = None,
        season: int | None = None,
        title: str | None = None,
        sites: list[int | str] | tuple[int | str, ...] | set[int | str] | str | None = None,
        media_type: str = "tv",
    ) -> list[dict]:
        # 显式空集合拦截：若调用方明确传入了空的有效站点集合（例如 [] 或 set()），严禁省略参数退化为全站搜索
        if sites is not None and not isinstance(sites, str) and len(sites) == 0:
            return []
        if isinstance(sites, str) and not sites.strip():
            return []

        sites_param = self._format_sites_param(sites)
        if sites is not None and not sites_param:
            return []

        is_movie = media_type in ("movie", "电影")
        mtype_str = "电影" if is_movie else "电视剧"

        if tmdb_id and tmdb_id > 0:
            params: dict[str, Any] = {"mtype": mtype_str}
            if not is_movie and season is not None:
                params["season"] = season
            if title:
                params["title"] = title
            if sites_param:
                params["sites"] = sites_param
            res = await self._get(f"/api/v1/search/media/tmdb:{tmdb_id}", **params)
        else:
            keyword = title or ""
            if not is_movie and season is not None:
                keyword = f"{keyword} S{str(season).zfill(2)}"
            params = {"keyword": keyword}
            if sites_param:
                params["sites"] = sites_param
            res = await self._get("/api/v1/search/title", **params)

        if isinstance(res, dict) and res.get("success") is False:
            message = str(res.get("message") or "")
            if "未搜索到任何资源" in message:
                return []
            raise RuntimeError(message or "MoviePilot 资源检索失败")
        data = res.get("data") if isinstance(res, dict) and "data" in res else res
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
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

    async def update_subscription_status(self, sub_id: int, state: str) -> dict:
        """更新订阅状态（R 运行中, S 暂停, P 待定）。"""
        state_code = str(state).upper().strip()
        if state_code not in ("R", "S", "P"):
            raise ValueError(f"未知的订阅状态代码: {state}")
        try:
            # 优先调用状态专有端点 PUT /api/v1/subscribe/status/{subid}?state={state}
            res = await self._client.put(
                f"{self.origin}/api/v1/subscribe/status/{sub_id}",
                params={"state": state_code},
                headers=self._headers(),
            )
            res.raise_for_status()
            return res.json()
        except Exception:
            # 降级端点：PUT /api/v1/subscribe/ 提交字典更新
            res = await self._client.put(
                f"{self.origin}/api/v1/subscribe/",
                json={"id": sub_id, "state": state_code},
                headers=self._headers(),
            )
            res.raise_for_status()
            return res.json()

    # ── 转移队列 ──────────────────────────────────────────
    async def get_transfer_queue(self) -> list[dict]:
        res = await self._get("/api/v1/transfer/queue")
        # 兼容 MP 返回嵌套队列结构 {media: ..., season: ..., tasks: [...]}
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and isinstance(res.get("data"), list):
            return res["data"]
        return []

    @staticmethod
    def flatten_transfer_queue(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 MoviePilot v2 嵌套的 media + season + tasks[] 整理队列展开为扁平任务列表。"""
        flat: list[dict[str, Any]] = []
        if not isinstance(queue, list):
            return flat
        for group in queue:
            if not isinstance(group, dict):
                continue
            tasks = group.get("tasks")
            if isinstance(tasks, list) and tasks:
                media = group.get("media") or {}
                season = group.get("season")
                for t in tasks:
                    if not isinstance(t, dict):
                        continue
                    fi = t.get("fileitem") or {}
                    flat.append({
                        "title": media.get("title") or t.get("title") or fi.get("name") or "",
                        "season": season,
                        "state": t.get("state") or group.get("state") or "running",
                        "source_path": fi.get("path") or t.get("source_path") or t.get("src") or "",
                        "storage": fi.get("storage") or "local",
                        "size": fi.get("size") or 0,
                        "downloader": t.get("downloader") or "",
                        "download_hash": t.get("download_hash") or "",
                    })
            else:
                flat.append(group)
        return flat

    async def get_system_version(self) -> str:
        """读取 MoviePilot 运行版本号（只读，用于能力与版本展示）。"""
        for ep in ("/api/v1/system/version", "/api/v1/system/env"):
            try:
                res = await self._get(ep)
                if isinstance(res, dict):
                    data = res.get("data")
                    if isinstance(data, str) and data.strip():
                        return data.strip()
                    if isinstance(data, dict):
                        for key in ("VERSION", "version", "APP_VERSION", "app_version"):
                            if data.get(key):
                                return str(data[key]).strip()
                    if res.get("version"):
                        return str(res["version"]).strip()
            except Exception:
                continue
        return ""

    async def list_storage_dir(
        self,
        storage: str,
        dir_path: str,
    ) -> tuple[bool, list[dict[str, Any]], str]:
        """调用 MoviePilot 原生 POST /api/v1/storage/list 列举远程存储目录（支持 local/u115/smb 等）。

        返回 (ok, entries, error_message)：
        - ok=False 代表网络/超时/鉴权/5xx 等存储异常，绝不能解释为“文件不存在”；
        - ok=True 代表存储健康且正常返回目录清单（空目录或不存在的目录返回 []）。
        """
        import posixpath

        norm_dir = str(dir_path or "").replace("\\", "/").strip()
        if not norm_dir:
            return False, [], "空目录路径"
        if not norm_dir.endswith("/"):
            norm_dir += "/"
        name = posixpath.basename(norm_dir.rstrip("/")) or "/"
        payload = {
            "storage": storage or "local",
            "type": "dir",
            "path": norm_dir,
            "name": name,
        }
        try:
            res = await self._post("/api/v1/storage/list", json=payload)
            if isinstance(res, list):
                return True, [x for x in res if isinstance(x, dict)], ""
            if isinstance(res, dict):
                if res.get("success") is False:
                    return False, [], str(res.get("message") or "storage/list 返回失败")
                data = res.get("data")
                if isinstance(data, list):
                    return True, [x for x in data if isinstance(x, dict)], ""
                return True, [], ""
            return False, [], f"storage/list 未知返回结构: {type(res).__name__}"
        except Exception as exc:
            return False, [], f"{type(exc).__name__}: {exc}"

    # ── 转移历史管理 ──────────────────────────────────────
    async def get_transfer_history(
        self,
        title: str | None = None,
        status: bool | None = None,
        page: int = 1,
        count: int = 100,
    ) -> list[dict]:
        """获取 MoviePilot 媒体整理（转移历史）记录列表。"""
        res = await self.get_transfer_history_paginated(status=status, title=title, page=page, count=count)
        return res.get("list", [])

    async def get_transfer_history_paginated(
        self,
        status: bool | None = None,
        title: str | None = None,
        page: int = 1,
        count: int = 50,
    ) -> dict[str, Any]:
        """获取 MoviePilot 媒体整理（转移历史）记录分页字典，保留 total 与 list。"""
        params: dict[str, Any] = {"page": page, "count": count}
        if status is not None:
            params["status"] = "true" if status else "false"
        if title:
            params["title"] = title
        res = await self._get("/api/v1/history/transfer", **params)
        total = 0
        records: list[dict[str, Any]] = []
        if isinstance(res, dict):
            data = res.get("data")
            if isinstance(data, dict):
                total = int(data.get("total") or len(data.get("list") or []))
                records = data.get("list") or data.get("items") or []
            elif isinstance(data, list):
                total = len(data)
                records = data
            elif isinstance(res.get("list"), list):
                total = int(res.get("total") or len(res["list"]))
                records = res["list"]
        elif isinstance(res, list):
            total = len(res)
            records = res
        return {"total": total, "list": records}

    async def get_manual_target_path(
        self,
        fileitem: dict[str, Any] | str,
        storage: str = "local",
    ) -> dict[str, Any]:
        """根据源 FileItem 调用 MoviePilot v2.15.6 POST /api/v1/transfer/manual/target-path 匹配目标目录配置。"""
        import posixpath

        if isinstance(fileitem, str):
            norm = str(fileitem).replace("\\", "/").strip()
            name = posixpath.basename(norm)
            stem, ext = posixpath.splitext(name)
            fi_obj: dict[str, Any] = {
                "storage": storage or "local",
                "type": "file",
                "path": norm,
                "name": name,
                "basename": stem,
                "extension": ext.lstrip("."),
            }
        else:
            fi_obj = dict(fileitem)
        try:
            return await self._post("/api/v1/transfer/manual/target-path", json={"fileitem": fi_obj})
        except Exception as e:
            log.warning("MoviePilot target-path 匹配失败: %s", e)
            return {"success": False, "message": str(e)}

    async def get_manual_transfer_history(self, logid: int) -> dict[str, Any]:
        """查询手动整理命中的已有成功历史（评估重整与冲突风险）。"""
        try:
            return await self._post("/api/v1/transfer/manual/history", json={"logid": int(logid)})
        except Exception as e:
            log.warning("MoviePilot manual/history 查询失败: %s", e)
            return {"success": False, "message": str(e)}

    async def recommend_episode_format(
        self,
        fileitem_or_path: dict[str, Any] | str,
        storage: str = "local",
    ) -> dict[str, Any]:
        """请求 MoviePilot 推荐集号格式。"""
        import posixpath

        if isinstance(fileitem_or_path, str):
            norm = str(fileitem_or_path).replace("\\", "/").strip()
            name = posixpath.basename(norm)
            payload: dict[str, Any] = {
                "fileitem": {
                    "storage": storage or "local",
                    "type": "file",
                    "path": norm,
                    "name": name,
                }
            }
        else:
            payload = {"fileitem": dict(fileitem_or_path)}
        try:
            return await self._post("/api/v1/transfer/episode-format/recommend", json=payload)
        except Exception as e:
            log.warning("MoviePilot episode-format/recommend 失败: %s", e)
            return {"success": False, "message": str(e)}

    async def manual_transfer_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按照 MoviePilot v2.15.6 ManualTransferItem 契约执行手动整理或预览。"""
        return await self._post("/api/v1/transfer/manual", json=payload)

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
        explicit_only: bool = False,
        tmdb_id: int | str | None = None,
        title_aliases: list[str] | None = None,
    ) -> list[dict]:
        """按剧名、季号和集数查找重叠的转移历史记录。

        Args:
            title: 剧集标题（清洗后）
            season: 目标季号（如 1）
            episodes: 目标集数集合（如 {14}）。若为 None 或空，代表整季所有集数。
            explicit_only: 为 True 且提供目标集数时，仅匹配有明确集号且完全落入 target_eps 内的冲突记录，保护已有集或整季历史不被误删。
            tmdb_id: 可选 TMDB ID，若历史记录自带 tmdbid 且不一致则跳过以防误删同名异剧。
            title_aliases: 可选附加剧名别名列表，用于覆盖中英文/原名差异。
        """
        queries: list[str] = []
        for t in [title, *(title_aliases or [])]:
            t_clean = str(t or "").strip()
            if t_clean and t_clean not in queries:
                queries.append(t_clean)

        all_items: list[dict] = []
        seen_ids: set[Any] = set()
        for q_title in queries:
            items = await self.get_all_transfer_history_for_title(q_title)
            for it in items or []:
                it_id = it.get("id")
                if it_id is not None:
                    if it_id in seen_ids:
                        continue
                    seen_ids.add(it_id)
                all_items.append(it)
        if not all_items:
            return []

        target_season = season
        target_eps = set(episodes) if episodes is not None else None
        target_tmdb = str(tmdb_id).strip() if tmdb_id is not None and str(tmdb_id).strip() else ""
        matched: list[dict] = []

        for item in all_items:
            # 0. TMDB ID 防错配校验
            if target_tmdb:
                item_tmdb = item.get("tmdbid") or item.get("tmdb_id")
                if item_tmdb is not None and str(item_tmdb).strip() and str(item_tmdb).strip() != target_tmdb:
                    continue

            # 1. 季号匹配
            if target_season is not None:
                item_seasons = parse_season_str(item.get("seasons"))
                if item_seasons and target_season not in item_seasons:
                    continue

            # 2. 集数匹配
            if target_eps is not None and len(target_eps) > 0:
                item_eps = parse_episode_str(item.get("episodes"))
                if explicit_only:
                    if not item_eps or not (item_eps & target_eps) or not item_eps.issubset(target_eps):
                        continue
                else:
                    # 如果历史记录有明确集数，且与目标集数无交集，则不匹配
                    if item_eps and not (item_eps & target_eps):
                        continue

            matched.append(item)

        return matched

    async def clear_transfer_history_for_episodes(
        self,
        title: str,
        season: int | None = None,
        episodes: set[int] | list[int] | None = None,
        explicit_only: bool = False,
        tmdb_id: int | str | None = None,
        title_aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """针对指定剧集与评估下载的集数，查找并【仅删除转移记录】（绝对不删实体文件）。

        Returns:
            {"cleared_count": int, "records": list[dict], "errors": list[str], "protected_count": int}
        """
        target_eps = set(episodes) if episodes is not None else None
        conflicting = await self.find_transfer_history(
            title=title,
            season=season,
            episodes=target_eps,
            explicit_only=explicit_only,
            tmdb_id=tmdb_id,
            title_aliases=title_aliases,
        )
        protected_count = 0
        if explicit_only and target_eps:
            broad = await self.find_transfer_history(
                title=title,
                season=season,
                episodes=target_eps,
                explicit_only=False,
                tmdb_id=tmdb_id,
                title_aliases=title_aliases,
            )
            matched_ids = {item.get("id") for item in conflicting if item.get("id") is not None}
            protected_count = sum(1 for item in broad if item.get("id") not in matched_ids)

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
            "protected_count": protected_count,
        }

    async def manual_transfer(self, source_path: str, target_path: str | None = None, preview: bool = False) -> dict:
        payload: dict[str, Any] = {"in_path": source_path, "preview": preview}
        if target_path:
            payload["target"] = target_path
        return await self._post("/api/v1/transfer/manual", json=payload)

    # ── 下载 ──────────────────────────────────────────────
    async def submit_download(self, media_info: dict, torrent_info: dict) -> dict:
        if not isinstance(media_info, dict) or not (
            str(media_info.get("title") or "").strip()
            or str(media_info.get("tmdb_id") or "").strip()
            or str(media_info.get("media_id") or "").strip()
        ):
            raise ValueError("候选资源缺少有效的影视元数据 (media_info 为空)，已拦截提交以防 MoviePilot 返回 500")
        if not isinstance(torrent_info, dict) or not (
            str(torrent_info.get("enclosure") or "").strip()
            or str(torrent_info.get("page_url") or "").strip()
            or str(torrent_info.get("title") or "").strip()
        ):
            raise ValueError("候选资源缺少有效的种子信息 (torrent_info 为空)")
        body = {"media_in": media_info, "torrent_in": torrent_info, "downloader": None, "save_path": None}
        res = await self._post("/api/v1/download/", json=body)
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(res.get("message") or "MoviePilot 下载提交失败")
        return res

    async def get_active_downloads(self) -> list[dict]:
        return await self._get("/api/v1/download/")

    async def get_download_history(self, page: int = 1, count: int = 100) -> list[dict]:
        """获取 MoviePilot 下载历史记录（兼容多种响应格式，不支持或失败时安全返回空列表）。"""
        try:
            res = await self._get("/api/v1/history/download", page=page, count=count)
            if isinstance(res, dict):
                data = res.get("data")
                if isinstance(data, dict):
                    return list(data.get("list") or data.get("items") or [])
                if isinstance(data, list):
                    return list(data)
            if isinstance(res, list):
                return list(res)
        except Exception:
            pass
        return []

    # ── 站点 ──────────────────────────────────────────────
    @staticmethod
    def sanitize_site_data(site: dict[str, Any]) -> dict[str, Any]:
        """清洗站点数据，剥离 Cookie/Passkey/Token 等敏感凭据。"""
        raw_id = site.get("id")
        site_id = int(raw_id) if raw_id is not None and str(raw_id).lstrip("-").isdigit() else raw_id
        raw_name = str(site.get("name") or "").strip()
        raw_domain = str(site.get("domain") or "").strip()
        raw_url = str(site.get("url") or "").strip()

        # URL 仅保留 scheme 与 host，杜绝带 token 的鉴权链接
        safe_url = ""
        if raw_url:
            try:
                parts = urlsplit(raw_url)
                safe_url = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else parts.netloc or raw_domain
            except Exception:
                safe_url = raw_domain

        return {
            "id": site_id,
            "name": raw_name or f"站点#{site_id}",
            "domain": raw_domain,
            "url": safe_url,
            "is_active": bool(site.get("is_active", True)),
            "public": bool(site.get("public", False)),
            "pri": site.get("pri"),
            "comment": str(site.get("comment") or site.get("note") or "").strip(),
        }

    async def get_sites(self, sanitize: bool = False) -> list[dict]:
        """从 MoviePilot 读取站点目录，严格校验返回格式。"""
        res = await self._get("/api/v1/site/")
        raw_list: list[dict] = []
        if isinstance(res, list):
            raw_list = res
        elif isinstance(res, dict):
            if res.get("success") is False:
                raise RuntimeError(res.get("message") or "获取站点列表失败")
            data = res.get("data")
            if isinstance(data, list):
                raw_list = data
            elif isinstance(data, dict) and isinstance(data.get("sites"), list):
                raw_list = data["sites"]
            else:
                raise RuntimeError("站点列表返回未知数据格式")
        else:
            raise RuntimeError("站点列表返回未知响应类型")

        if sanitize:
            return [self.sanitize_site_data(s) for s in raw_list if isinstance(s, dict)]
        return raw_list

    async def get_indexer_sites(self) -> dict[str, Any]:
        """读取 MoviePilot 默认搜索站点配置 (IndexerSites)。"""
        try:
            res = await self._get("/api/v1/system/setting/IndexerSites")
            if isinstance(res, dict):
                if res.get("success") is False:
                    return {"ok": False, "value": None, "error": str(res.get("message") or "接口返回失败")}
                data = res.get("data")
                if isinstance(data, dict) and "value" in data:
                    val = data.get("value")
                    if val is None or val == []:
                        return {"ok": True, "value": [], "error": None}
                    if isinstance(val, list):
                        return {"ok": True, "value": [int(x) for x in val if str(x).lstrip("-").isdigit()], "error": None}
                if "value" in res:
                    val = res.get("value")
                    if val is None or val == []:
                        return {"ok": True, "value": [], "error": None}
                    if isinstance(val, list):
                        return {"ok": True, "value": [int(x) for x in val if str(x).lstrip("-").isdigit()], "error": None}
            return {"ok": False, "value": None, "error": "返回格式未识别"}
        except Exception as exc:
            return {"ok": False, "value": None, "error": f"{type(exc).__name__}: {exc}"}

    async def get_transfer_overwrite_policy(self) -> dict[str, Any]:
        """读取 MoviePilot 目录整理覆盖策略（仅读取配置，不做任何写入）。"""
        try:
            res = await self._get("/api/v1/system/setting/Directories")
            dirs = (res.get("data") or {}).get("value") if isinstance(res, dict) and isinstance(res.get("data"), dict) else None
            if not isinstance(dirs, list):
                return {"ok": False, "tv_modes": [], "protects_existing": None, "summary": "未读到目录覆盖策略"}
            tv_modes: list[str] = []
            details: list[dict[str, Any]] = []
            for d in dirs:
                if not isinstance(d, dict):
                    continue
                mtype = str(d.get("media_type") or "").strip()
                if mtype in ("", "电视剧", "TV"):
                    mode = str(d.get("overwrite_mode") or "").strip() or "unknown"
                    tv_modes.append(mode)
                    details.append({"name": d.get("name"), "download_path": d.get("download_path"), "overwrite_mode": mode})
            unique_modes = sorted(set(tv_modes))
            protects_existing = bool(tv_modes) and all(m == "never" for m in tv_modes)
            return {
                "ok": True,
                "tv_modes": unique_modes,
                "protects_existing": protects_existing,
                "details": details,
                "summary": f"TV目录覆盖策略={','.join(unique_modes) or '未知'}",
            }
        except Exception as exc:
            return {"ok": False, "tv_modes": [], "protects_existing": None, "summary": f"读取覆盖策略失败({type(exc).__name__})"}

    # ── 媒体服务器 (Emby/Jellyfin/Plex) ──────────────────
    async def check_mediaserver_exists_remote(
        self,
        tmdb_id: int,
        title: str = "",
        year: str = "",
        media_type: str = "电视剧",
    ) -> dict[str, Any]:
        """通过 MoviePilot /api/v1/mediaserver/exists_remote 实时核查媒体服务器内已入库集号。"""
        if not self.origin:
            return {"checked": False, "season_episodes": {}, "error": "MoviePilot 未配置"}
        tid = int(tmdb_id)
        if not title:
            try:
                info = await self.get_media_info(tid)
                if isinstance(info, dict):
                    title = str(info.get("title") or info.get("en_title") or "")
                    if not year:
                        year = str(info.get("year") or "")[:4]
            except Exception:
                pass
        payload: dict[str, Any] = {
            "tmdb_id": tid,
            "title": str(title or ""),
            "type": media_type or "电视剧",
        }
        if year and str(year)[:4].isdigit():
            payload["year"] = str(year)[:4]
        try:
            res = await self._post("/api/v1/mediaserver/exists_remote", json=payload)
            if not isinstance(res, dict):
                return {"checked": False, "season_episodes": {}, "error": "响应格式异常"}
            if res.get("success") is False:
                return {"checked": False, "season_episodes": {}, "error": str(res.get("message") or "MoviePilot 查询失败")}
            raw_dict = res.get("data") if (isinstance(res.get("data"), dict)) else res
            if not isinstance(raw_dict, dict):
                return {"checked": False, "season_episodes": {}, "error": "数据体格式异常"}
            season_episodes: dict[int, list[int]] = {}
            for k, vals in raw_dict.items():
                if not str(k).lstrip("-").isdigit() or not isinstance(vals, (list, tuple, set)):
                    continue
                sn = int(k)
                eps = sorted({int(x) for x in vals if str(x).isdigit()})
                if eps:
                    season_episodes[sn] = eps
            return {"checked": True, "season_episodes": season_episodes, "error": ""}
        except Exception as exc:
            return {"checked": False, "season_episodes": {}, "error": f"{type(exc).__name__}: {exc}"}

    async def check_mediaserver_movie_exists(
        self,
        tmdb_id: int,
        title: str = "",
        year: str = "",
    ) -> dict[str, Any]:
        """通过 MoviePilot 实时核查媒体服务器内电影是否存在（适配 notexists 与 exists 语义）。"""
        if not self.origin:
            return {"checked": False, "exists": False, "error": "MoviePilot 未配置"}
        tid = int(tmdb_id)
        if not title:
            try:
                info = await self.get_media_info(tid, type_name="电影")
                if isinstance(info, dict):
                    title = str(info.get("title") or info.get("en_title") or "")
                    if not year:
                        year = str(info.get("year") or "")[:4]
            except Exception:
                pass

        payload: dict[str, Any] = {
            "tmdb_id": tid,
            "title": str(title or ""),
            "type": "电影",
        }
        if year and str(year)[:4].isdigit():
            payload["year"] = str(year)[:4]

        # 1. 优先尝试 POST /api/v1/mediaserver/notexists
        # MoviePilot v2.15.6 契约：对于电影，若已在媒体库存在，则返回空列表 []；若缺失则返回含未入库项的列表
        try:
            res = await self._post("/api/v1/mediaserver/notexists", json=payload)
            if isinstance(res, dict) and res.get("success") is not False:
                data = res.get("data") if "data" in res else res
                if isinstance(data, list):
                    exists = (len(data) == 0)
                    return {"checked": True, "exists": exists, "method": "notexists", "error": ""}
        except Exception:
            pass

        # 2. 备选尝试 GET /api/v1/mediaserver/exists (本地索引查询)
        try:
            params = {"tmdbid": tid, "type": "电影"}
            res = await self._get("/api/v1/mediaserver/exists", **params)
            if isinstance(res, dict):
                data = res.get("data")
                if isinstance(data, dict) and "item" in data:
                    exists = bool(data.get("item"))
                    return {"checked": True, "exists": exists, "method": "exists_index", "error": ""}
                if isinstance(data, bool):
                    return {"checked": True, "exists": data, "method": "exists_index", "error": ""}
        except Exception as exc:
            return {"checked": False, "exists": False, "error": f"{type(exc).__name__}: {exc}"}

        return {"checked": False, "exists": False, "error": "未取得有效电影入库检验数据"}

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


sanitize_site_data = MoviePilotClient.sanitize_site_data
