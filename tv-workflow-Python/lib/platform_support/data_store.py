"""
JSON 文件持久化存储

用于保存下载记录、清理日志和整理修复等需要跨会话保留的数据。
使用临时文件 + 原子重命名保证读写一致性。
"""

import json
import os
from pathlib import Path
import re
from typing import Any, List, Optional

from ..config import PROJECT_ROOT


class DataStore:
    """轻量级 JSON 原子持久化存储"""

    def __init__(self, data_dir: str = "data"):
        if os.path.isabs(data_dir):
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = (PROJECT_ROOT / data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _key_to_file(self, key: str) -> Path:
        safe_key = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(key))
        safe_key = re.sub(r"\s+", "_", safe_key)[:200]
        return self.data_dir / f"{safe_key}.json"

    def get(self, key: str, fallback: Any = None) -> Any:
        file_path = self._key_to_file(key)
        if not file_path.exists():
            return fallback
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return fallback

    def put(self, key: str, value: Any) -> None:
        file_path = self._key_to_file(key)
        tmp_path = file_path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
            tmp_path.replace(file_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise

    def del_item(self, key: str) -> None:
        file_path = self._key_to_file(key)
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception:
                pass

    def list_keys(self) -> List[str]:
        try:
            return [
                p.stem for p in self.data_dir.iterdir()
                if p.is_file() and p.name.endswith(".json") and not p.name.endswith(".tmp")
            ]
        except Exception:
            return []


def create_data_store(data_dir: str = "data") -> DataStore:
    return DataStore(data_dir)
