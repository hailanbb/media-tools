"""
配置加载与校验模块

从项目根目录或指定路径读取 config.json，校验必填字段，
填充默认参数，并返回只读配置字典或配置对象。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# 项目根目录（lib/ 的上级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 必填字段定义
REQUIRED_FIELDS = [
    ("clouddrive2.origin", "CloudDrive2 服务地址 (clouddrive2.origin)"),
    ("clouddrive2.token",  "CloudDrive2 API 令牌 (clouddrive2.token)"),
    ("moviepilot.origin",  "MoviePilot 服务地址 (moviepilot.origin)"),
    ("moviepilot.token",   "MoviePilot API 令牌 (moviepilot.token)"),
]


def get_nested_value(obj: Any, path: str) -> Any:
    """按点分路径从字典中取值"""
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def validate_required(config: Dict[str, Any]) -> List[str]:
    """校验必填字段，返回缺失项中文描述列表"""
    missing = []
    for path, label in REQUIRED_FIELDS:
        val = get_nested_value(config, path)
        if val is None or val == "":
            missing.append(label)
    return missing


class ConfigDict(dict):
    """支持属性访问和字典访问的配置类"""
    def __getattr__(self, item: str) -> Any:
        try:
            val = self[item]
            if isinstance(val, dict) and not isinstance(val, ConfigDict):
                val = ConfigDict(val)
                self[item] = val
            return val
        except KeyError:
            return None

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


def load_config(config_path: Optional[str] = None) -> ConfigDict:
    """
    加载并校验配置文件

    :param config_path: 配置文件的绝对或相对路径，默认为项目根目录下的 config.json
    :return: 填充默认值后的配置字典
    :raises FileNotFoundError, ValueError
    """
    if config_path:
        file_path = Path(config_path).resolve()
    else:
        file_path = (PROJECT_ROOT / "config.json").resolve()

    if not file_path.exists():
        raise FileNotFoundError(
            f"❌ 配置文件不存在: {file_path}\n"
            f"   请复制 config.example.json 为 config.json 并填写实际参数。"
        )

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"❌ 配置文件 JSON 格式错误: {file_path}\n"
            f"   请检查 JSON 语法（多余逗号、缺少引号等）: {e}"
        )
    except Exception as e:
        raise RuntimeError(f"❌ 读取配置文件失败: {e}")

    # 校验必填字段
    missing = validate_required(config)
    if missing:
        msg = "\n".join(f"   • {m}" for m in missing)
        raise ValueError(
            f"❌ 配置文件缺少以下必填项:\n{msg}\n   请在 {file_path} 中补全上述字段。"
        )

    # 填充默认值
    tmdb_defaults = {"language": "zh-CN"}
    tmdb_defaults.update(config.get("tmdb") or {})
    config["tmdb"] = tmdb_defaults

    paths_defaults = {
        "source": "/115/Library_incs/nas",
        "destination": "/115/Library_incs/nas.finish",
        "downloadPath": "/video/downloads",
    }
    paths_defaults.update(config.get("paths") or {})
    config["paths"] = paths_defaults

    transfer_repair_defaults = {
        "enabled": True,
        "source": config["paths"]["downloadPath"],
        "target": config["paths"]["source"],
    }
    transfer_repair_defaults.update(config.get("transferRepair") or {})
    config["transferRepair"] = transfer_repair_defaults

    notifications_defaults = {
        "enabled": True,
        "sound": False,
    }
    notifications_defaults.update(config.get("notifications") or {})
    config["notifications"] = notifications_defaults

    log_defaults = {
        "directory": "./logs",
        "level": "info",
    }
    log_defaults.update(config.get("log") or {})
    config["log"] = log_defaults

    return ConfigDict(config)
