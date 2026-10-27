"""
TMDB (The Movie Database) API 客户端

封装电视剧详情、季详情、搜索接口，内置请求缓存与 429 限流重试。
"""

import time
from typing import Any, Dict, Optional, Tuple, Union
import urllib.parse

import requests

BASE_URL = "https://api.themoviedb.org"
CACHE_TTL = 3600  # 1 小时 (秒)


class TMDBClient:
    """TMDB API 客户端"""

    def __init__(self, api_key: str, language: str = "zh-CN", logger: Any = None):
        self.api_key = api_key
        self.language = language
        self.logger = logger
        self.session = requests.Session()
        self._cache: Dict[str, Tuple[Any, float]] = {}

    def _log(self, level: str, msg: str) -> None:
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    def _request(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Any:
        url = f"{BASE_URL}{path}"
        clean_params = dict(params or {})

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.api_key.startswith("ey") or len(self.api_key) > 50:
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                clean_params["api_key"] = self.api_key

        # 缓存命中检查
        cache_key = f"{path}?{urllib.parse.urlencode(sorted(clean_params.items()))}"
        now = time.time()
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if now - ts < CACHE_TTL:
                self._log("info", f"[TMDB] 缓存命中: {path}")
                return data

        max_retries = 3
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                self._log("info", f"[TMDB] GET {path}")
                res = self.session.get(url, params=clean_params, headers=headers, timeout=timeout)

                # 429 限流处理
                if res.status_code == 429:
                    retry_after = res.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else min(1.0 * (2 ** attempt), 16.0)
                    self._log("warn", f"[TMDB] 请求限流 (429)，{delay}s 后重试 ({attempt + 1}/{max_retries})")
                    if attempt < max_retries:
                        time.sleep(delay)
                        continue

                try:
                    data = res.json()
                except Exception:
                    data = res.text

                if res.status_code >= 400:
                    msg = data.get("status_message") if isinstance(data, dict) else str(data)
                    raise RuntimeError(f"TMDB 请求失败 [{res.status_code}]: {msg}")

                # 写入缓存
                self._cache[cache_key] = (data, now)
                if len(self._cache) > 500:
                    self._prune_cache()

                self._log("info", f"[TMDB] 响应 {res.status_code}")
                return data

            except Exception as e:
                last_error = e
                if "429" not in str(e) and attempt > 0:
                    raise last_error
                time.sleep(1.0)

        if last_error:
            raise last_error

    def _prune_cache(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts >= CACHE_TTL]
        for k in expired:
            del self._cache[k]

    def get_show_details(self, tmdb_id: Union[int, str]) -> Dict[str, Any]:
        """获取电视剧详情"""
        return self._request(f"/3/tv/{tmdb_id}", params={"language": self.language})

    def get_season_details(self, tmdb_id: Union[int, str], season: Union[int, str]) -> Dict[str, Any]:
        """获取电视剧指定季详情（含各集信息）"""
        return self._request(f"/3/tv/{tmdb_id}/season/{season}", params={"language": self.language})

    def search_show(self, title: str, year: Optional[Union[int, str]] = None) -> Dict[str, Any]:
        """搜索电视剧"""
        params: Dict[str, Any] = {"query": title, "language": self.language}
        if year is not None:
            params["first_air_date_year"] = str(year)
        return self._request("/3/search/tv", params=params)
