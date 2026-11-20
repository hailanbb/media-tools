"""推荐处理的本地持久化回执；中断中的写操作必须先重新盘点。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

from app.config import get_settings


def _path(key: str) -> Path:
    if not re.fullmatch(r"(?:plan_rec|job_rec)_[a-f0-9]{12}", key):
        raise ValueError("无效的推荐处理标识")
    return Path(get_settings().data_dir) / "recommended_batches" / (key + ".json")


def save(key: str, value: dict) -> None:
    target = _path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load(key: str) -> dict | None:
    try:
        value = json.loads(_path(key).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return value if isinstance(value, dict) else None
