"""
MoviePilot v2.15.6 整理修复引擎

解决 MoviePilot 下载完成后，由于识别错误、转移失败等原因导致的整理历史阻塞问题。
原则：
1. 仅对明确 TMDB ID 与已确认缺集的项进行单集修复
2. 仅删除指定的单条失败历史记录（固定 deletesrc=false, deletedest=false 绝不删除源文件或目标文件）
3. 提交前进行 preview 预检，确保目标路径与季号精确对应
4. 再次执行 manualTransfer 重新触发整理
"""

import copy
from datetime import datetime
import posixpath
import re
from typing import Any, Callable, Dict, List, Optional

from .episode_parser import MEDIA_REGEX
from .repair_state import is_repair_pending, is_repair_uncertain


def _is_valid_path(p: str) -> bool:
    if not isinstance(p, str) or not p.startswith("/"):
        return False
    if re.search(r"[\\\x00-\x1f]", p) or "//" in p:
        return False
    parts = p.split("/")
    return not any(part in (".", "..") for part in parts)


def _is_within(p: str, root: str) -> bool:
    if not _is_valid_path(p) or not _is_valid_path(root):
        return False
    norm_root = root.rstrip("/") + "/"
    return p.startswith(norm_root)


def match_history_identity(task: Any, h: Dict[str, Any]) -> bool:
    """校验历史记录条目身份是否与当前任务匹配（兼容电视剧与动漫）"""
    if not h or not getattr(task, "tmdb", None) or not getattr(task, "needed", None):
        return False

    tmdb_match = str(h.get("tmdbid")) == str(task.tmdb)
    type_match = h.get("type") in ("电视剧", "动漫")

    seasons_str = str(h.get("seasons") or "")
    if not re.match(r"^S\d+$", seasons_str, re.IGNORECASE):
        return False
    season_match = int(seasons_str[1:]) == task.season

    episodes_str = str(h.get("episodes") or "")
    if not re.match(r"^E\d+$", episodes_str, re.IGNORECASE):
        return False
    ep_num = int(episodes_str[1:])
    ep_match = ep_num in task.needed

    return tmdb_match and type_match and season_match and ep_match


def build_transfer_request(
    task: Any,
    h: Dict[str, Any],
    src_item: Dict[str, Any],
    target_root: str
) -> Dict[str, Any]:
    """构建手动转移请求参数"""
    ep = int(str(h.get("episodes") or "")[1:])
    category = getattr(task, "category", "") or ""
    name = getattr(task, "name", "") or getattr(task, "title", "")
    target_dir = f"{target_root.rstrip('/')}/{category}/{name}/Season {task.season}"
    is_anime = "动漫" in category or "动漫" in str(getattr(task, "path", ""))
    media_type = "动漫" if is_anime else "电视剧"

    return {
        "fileitem": {
            "storage": src_item.get("storage") or "local",
            "path": src_item.get("path"),
            "type": "file",
            "name": src_item.get("name") or posixpath.basename(src_item.get("path") or ""),
            "size": src_item.get("size"),
            "modify_time": src_item.get("modify_time"),
        },
        "target_storage": "local",
        "target_path": target_dir,
        "media_info": {
            "type": media_type,
            "tmdb_id": int(task.tmdb),
            "title": task.title,
            "year": task.year or "",
            "category": category,
        },
        "meta_info": {
            "begin_season": task.season,
            "end_season": task.season,
            "season_list": [task.season],
            "begin_episode": ep,
            "end_episode": ep,
            "episode_list": [ep],
        },
        "transfer_type": "copy",
        "overwrite_mode": "never",
    }


def check_repair(
    task: Any,
    mp: Any,
    config: Dict[str, str],
    checkpoint: Callable[[], None] = lambda: None
) -> Dict[str, Any]:
    """检查并生成修复计划（只读）"""
    if not getattr(task, "tmdb", None) or not getattr(task, "needed", None):
        raise ValueError("修复需要明确的 TMDB ID 与缺失集号")

    checkpoint()
    try:
        global_info = mp.get_system_global()
        # 版本检查
    except Exception:
        pass

    checkpoint()
    history_data = mp.get_transfer_histories(task.title, 1, 100)
    if isinstance(history_data, dict):
        histories = history_data.get("data", {}).get("list") or history_data.get("list") or []
    elif isinstance(history_data, list):
        histories = history_data
    else:
        histories = []

    matches = [h for h in histories if match_history_identity(task, h)]
    items: List[Dict[str, Any]] = []
    notes: List[str] = []

    src_root = config.get("source", "")
    target_root = config.get("target", "")

    for ep in task.needed:
        checkpoint()
        ep_matches = [h for h in matches if int(str(h.get("episodes") or "")[1:]) == ep]
        if not ep_matches:
            notes.append(f"E{ep}: 未找到匹配的历史记录")
            continue
        if len(ep_matches) > 1:
            notes.append(f"E{ep}: 存在多条冲突的历史记录，需人工核实")
            continue

        h = ep_matches[0]
        src_path = h.get("src") or (h.get("src_fileitem") or {}).get("path")
        if not src_path or not _is_within(src_path, src_root) or not MEDIA_REGEX.search(src_path):
            notes.append(f"E{ep}: 历史源文件不在指定下载目录下或非视频")
            continue

        src_dir = posixpath.dirname(src_path)
        try:
            src_files = mp.list_storage(src_dir)
        except Exception as e:
            notes.append(f"E{ep}: 无法读取源目录: {e}")
            continue

        src_file = next((f for f in src_files if f.get("path") == src_path), None)
        if not src_file or (src_file.get("size") or 0) <= 0:
            notes.append(f"E{ep}: 源文件不存在或大小为零")
            continue

        req = build_transfer_request(task, h, src_file, target_root)

        try:
            preview_res = mp.preview_transfer(req)
            p_data = preview_res.get("data") if isinstance(preview_res, dict) else {}
            if not preview_res.get("success", False) or (p_data.get("summary", {}).get("failed", 0) > 0):
                notes.append(f"E{ep}: 预检提示转移不可行 ({preview_res.get('message', '未知')})")
                continue

            items.append({
                "id": h.get("id"),
                "episode": ep,
                "source": src_path,
                "target": (p_data.get("items") or [{}])[0].get("target", ""),
                "history": h,
                "sourceFile": src_file,
                "request": req,
                "state": "ready",
                "steps": [],
                "note": "已预检通过，等待确认执行",
            })
        except Exception as e:
            notes.append(f"E{ep}: 预检异常: {e}")

    now_iso = datetime.now().isoformat()
    return {
        "schema": 1,
        "taskKey": getattr(task, "key", ""),
        "state": "ready" if items else "blocked",
        "at": now_iso,
        "episodes": [i["episode"] for i in items] if items else task.needed,
        "items": items,
        "note": "；".join(notes) if notes else "已生成修复计划",
        "checkedAt": now_iso,
    }


def execute_repair(
    task: Any,
    plan: Dict[str, Any],
    mp: Any,
    config: Dict[str, str],
    save: Callable[[Dict[str, Any]], None] = lambda r: None,
    checkpoint: Callable[[], None] = lambda: None
) -> Dict[str, Any]:
    """执行修复计划（解除历史后重转）"""
    if (
        plan.get("taskKey") != getattr(task, "key", "")
        or plan.get("state") != "ready"
        or not plan.get("items")
        or is_repair_uncertain(plan)
    ):
        raise ValueError("修复计划未就绪或存在未决状态")

    record = copy.deepcopy(plan)
    record["at"] = datetime.now().isoformat()

    def persist() -> None:
        record["at"] = datetime.now().isoformat()
        save(record)

    for item in record["items"]:
        checkpoint()
        # 1. 删除指定的转移历史记录
        item["state"] = record["state"] = "history-delete-pending"
        item.setdefault("steps", []).append({
            "action": "解除历史", "state": "pending", "at": datetime.now().isoformat(), "historyId": item["id"]
        })
        persist()

        try:
            del_reply = mp.delete_transfer_history(item["id"])
            if del_reply.get("success") is False:
                raise RuntimeError(f"删除历史失败: {del_reply.get('message', '未知原因')}")

            item["steps"][-1]["state"] = "verified"
            item["steps"][-1]["finishedAt"] = datetime.now().isoformat()
            item["state"] = record["state"] = "history-deleted"
            persist()

            checkpoint()
            # 2. 重新提交手动转移
            item["state"] = record["state"] = "transfer-pending"
            item["steps"].append({"action": "重转", "state": "pending", "at": datetime.now().isoformat()})
            persist()

            transfer_reply = mp.execute_manual_transfer(item["request"])
            if transfer_reply.get("success") is False:
                raise RuntimeError(f"重转提交失败: {transfer_reply.get('message', '未知原因')}")

            item["steps"][-1]["state"] = "submitted"
            item["steps"][-1]["finishedAt"] = datetime.now().isoformat()
            item["state"] = "submitted"
            persist()

        except Exception as e:
            item["note"] = str(e)
            item["state"] = record["state"] = "uncertain"
            if item.get("steps"):
                item["steps"][-1]["error"] = str(e)
            persist()
            raise e

    record["state"] = "submitted"
    record["note"] = "重转请求已发出，请等待 MoviePilot 写入并由复核阶段核对文件"
    persist()
    return record
