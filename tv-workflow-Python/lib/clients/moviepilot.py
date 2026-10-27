"""
MoviePilot REST API 客户端

封装媒体搜索、剧集信息查询、下载任务管理与整理修复联动。
自动适配 X-API-KEY 与 JWT Bearer 认证。
"""

import json
from typing import Any, Dict, List, Optional, Set, Union
import urllib.parse

import requests


class MoviePilotClient:
    """MoviePilot REST API 客户端"""

    def __init__(self, origin: str, token: str, logger: Any = None):
        self.origin = origin.rstrip("/")
        self.token = token
        self.logger = logger
        self.session = requests.Session()
        self._site_map_cache: Optional[Dict[str, int]] = None

    def _log(self, level: str, msg: str) -> None:
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        timeout: int = 30
    ) -> Any:
        url = f"{self.origin}{path}"
        headers = {"Content-Type": "application/json"}

        # 认证方式：JWT（以 ey 开头）使用 Bearer，其他使用 X-API-KEY
        if self.token.startswith("ey") or len(self.token) > 100:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["X-API-KEY"] = self.token

        clean_params = {}
        if params:
            for k, v in params.items():
                if v is not None:
                    clean_params[k] = v

        self._log("info", f"[MoviePilot] {method} {path}")

        try:
            res = self.session.request(
                method=method,
                url=url,
                params=clean_params,
                json=json_body,
                headers=headers,
                timeout=timeout
            )
        except requests.Timeout:
            raise TimeoutError(f"MoviePilot 请求超时 ({timeout}s): {method} {path}")
        except Exception as e:
            raise RuntimeError(f"MoviePilot 连接异常: {e}")

        if res.status_code in (401, 403):
            raise PermissionError("MoviePilot 登录已失效或权限不足 (401/403)")

        try:
            data = res.json()
        except Exception:
            data = res.text

        if res.status_code >= 400:
            msg = data.get("message") or data.get("detail") if isinstance(data, dict) else str(data)
            raise RuntimeError(f"MoviePilot 请求失败 [{res.status_code}]: {msg}")

        self._log("info", f"[MoviePilot] 响应 {res.status_code}")
        return data

    def get_sites(self) -> List[Dict[str, Any]]:
        """获取所有站点列表"""
        res = self._request("GET", "/api/v1/site/")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data") or []
        return []

    def resolve_site_ids(self, site_names: List[Union[str, int]]) -> List[int]:
        """将站点名称列表解析为站点 ID 列表（利用本地缓存）"""
        if not site_names:
            return []
        if self._site_map_cache is None:
            try:
                sites = self.get_sites()
                self._site_map_cache = {s.get("name"): s.get("id") for s in sites if s.get("name") and s.get("id")}
            except Exception as e:
                self._log("warn", f"[MoviePilot] 获取站点列表失败: {e}")
                return []

        resolved = []
        for name in site_names:
            if isinstance(name, int):
                resolved.append(name)
            elif name in self._site_map_cache:
                resolved.append(self._site_map_cache[name])
        return resolved

    def search_media(
        self,
        tmdb_id: Optional[Union[int, str]],
        season: Optional[Union[int, str]],
        title: Optional[str],
        year: Optional[Union[int, str]] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        搜索媒体资源
        - 有 tmdb_id 走精确搜索 /search/media/tmdb:{id}
        - 无 tmdb_id 走关键词搜索 /search/title
        支持指定 sites 参数以限定特定站点搜索
        """
        opts = options or {}
        if tmdb_id:
            return self._search_by_tmdb_id(tmdb_id, season, title, year, opts.get("sites"))
        return self._search_by_title(title or "", season, opts.get("sites"))

    def _search_by_tmdb_id(
        self,
        tmdb_id: Union[int, str],
        season: Optional[Union[int, str]],
        title: Optional[str],
        year: Optional[Union[int, str]],
        sites: Optional[Any]
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"mtype": "电视剧"}
        if season is not None:
            params["season"] = season
        if title:
            params["title"] = title
        if year:
            params["year"] = year
        if sites:
            params["sites"] = ",".join(str(s) for s in sites) if isinstance(sites, (list, tuple)) else str(sites)

        res = self._request("GET", f"/api/v1/search/media/tmdb:{tmdb_id}", params=params)
        data = res.get("data") if isinstance(res, dict) and "data" in res else res
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("results") or []
        return []

    def _search_by_title(
        self,
        title: str,
        season: Optional[Union[int, str]],
        sites: Optional[Any]
    ) -> List[Dict[str, Any]]:
        season_str = f" S{str(season).zfill(2)}" if season is not None else ""
        keyword = f"{title}{season_str}"

        results: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        max_pages = 10

        for page in range(1, max_pages + 1):
            params: Dict[str, Any] = {"keyword": keyword, "page": page}
            if sites:
                params["sites"] = ",".join(str(s) for s in sites) if isinstance(sites, (list, tuple)) else str(sites)

            try:
                res = self._request("GET", "/api/v1/search/title", params=params)
            except Exception as e:
                self._log("warn", f"[MoviePilot] 搜索第 {page} 页失败: {e}")
                break

            data = res.get("data") if isinstance(res, dict) and "data" in res else res
            items = data if isinstance(data, list) else (data.get("items") or data.get("results") or []) if isinstance(data, dict) else []
            if not items:
                break

            for item in items:
                key = item.get("enclosure") or item.get("magnet") or item.get("hash") or json.dumps(item, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    results.append(item)

            if len(items) < 20:
                break

        return results

    def get_media_info(self, tmdb_id: Union[int, str]) -> Dict[str, Any]:
        """获取媒体详情"""
        return self._request("GET", f"/api/v1/media/tmdb:{tmdb_id}", params={"type_name": "电视剧"})

    def get_season_episodes(self, tmdb_id: Union[int, str], season: Union[int, str]) -> Dict[str, Any]:
        """获取指定季剧集列表"""
        return self._request("GET", f"/api/v1/tmdb/{tmdb_id}/{season}")

    def get_active_downloads(self) -> List[Dict[str, Any]]:
        """获取当前活跃下载任务"""
        return self._request("GET", "/api/v1/download/")

    def submit_download(self, media_info: Dict[str, Any], torrent_info: Dict[str, Any]) -> Dict[str, Any]:
        """提交下载任务"""
        body = {
            "media_in": media_info,
            "torrent_in": torrent_info,
            "downloader": None,
            "save_path": None,
        }
        return self._request("POST", "/api/v1/download/", json_body=body)

    def get_system_global(self) -> Dict[str, Any]:
        """获取系统全局信息"""
        try:
            res = self._request("GET", "/api/v1/system/env")
            env = res.get("data") if isinstance(res, dict) and "data" in res else res
            version = env.get("VERSION") or env.get("APP_VERSION") or "v2.15.6" if isinstance(env, dict) else "v2.15.6"
            return {"data": {"BACKEND_VERSION": version}}
        except Exception:
            return {"data": {"BACKEND_VERSION": "v2.15.6"}}

    def get_transfer_queue(self) -> List[Dict[str, Any]]:
        """获取转移队列中的任务"""
        return self._request("GET", "/api/v1/transfer/queue")

    def get_transfer_histories(self, title: str, page: int = 1, count: int = 100) -> Any:
        """查询指定标题的转移历史"""
        return self._request("GET", "/api/v1/history/transfer", params={"title": title, "page": page, "count": count})

    def delete_transfer_history(self, history_id: int) -> Dict[str, Any]:
        """删除指定的转移历史记录（固定 deletesrc=false, deletedest=false 绝不删除实际文件）"""
        return self._request(
            "DELETE",
            "/api/v1/history/transfer",
            params={"deletesrc": False, "deletedest": False},
            json_body={"id": history_id}
        )

    def preview_transfer(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """预览手动转移操作"""
        body = dict(req)
        body["preview"] = True
        return self._request("POST", "/api/v1/transfer/manual", json_body=body)

    def execute_manual_transfer(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """执行手动转移"""
        body = dict(req)
        body["preview"] = False
        return self._request("POST", "/api/v1/transfer/manual", json_body=body)

    def list_storage(self, path: str, storage: str = "local") -> List[Dict[str, Any]]:
        """查询存储路径下的文件列表"""
        body = {"storage": storage, "path": path, "type": "dir"}
        res = self._request("POST", "/api/v1/storage/list", json_body=body)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data") or []
        return []
