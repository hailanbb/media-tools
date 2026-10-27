"""
整理修复状态机与跨阶段校验

管理 MoviePilot 整理历史修复状态，并在复核阶段（三）与归档阶段（四）提供闭环验证。
"""

from datetime import datetime
import json
import re
from typing import Any, Dict, List, Optional

REPAIR_STATES = [
    "ready",                  # 待确认修复
    "blocked",                # 修复阻挡待人工核实
    "checked",                # 已检查
    "history-delete-pending", # 历史删除待核实
    "history-deleted",        # 历史已解除
    "transfer-pending",       # 转移待核实
    "submitted",              # 已提交重转
    "uncertain",              # 修复结果待核实
    "files-verified",         # 文件已复核
]

REPAIR_LABELS = {
    "ready": "待确认修复",
    "blocked": "修复阻挡待人工核实",
    "checked": "已检查/待重转",
    "submitted": "已提交重转",
    "uncertain": "修复结果待核实",
    "history-delete-pending": "历史删除待核实",
    "history-deleted": "历史已解除",
    "transfer-pending": "转移待核实",
    "files-verified": "文件已复核",
}


def read_repair_state(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从报告数据行读取修复记录（支持分片拼接）"""
    if not data:
        return None

    cont_keys = [k for k in data.keys() if re.match(r"^修复记录续\d+$", k)]
    cont_keys.sort(key=lambda k: int(re.search(r"\d+$", k).group(0)))
    continuations = "".join(str(data.get(k) or "") for k in cont_keys)

    text = str(data.get("修复记录") or "") + continuations
    if not text:
        return None
    if len(text) > 2000000:
        raise ValueError("修复记录超出大小限制")

    try:
        r = json.loads(text)
        if (
            isinstance(r, dict)
            and r.get("schema") == 1
            and r.get("state") in REPAIR_STATES
            and isinstance(r.get("items"), list)
            and isinstance(r.get("episodes"), list)
        ):
            return r
        return None
    except Exception:
        return None


def is_repair_pending(r: Optional[Dict[str, Any]]) -> bool:
    """只要存在且状态不是 'files-verified'，即属于未闭环"""
    return bool(r and r.get("state") != "files-verified")


def is_repair_uncertain(r: Optional[Dict[str, Any]]) -> bool:
    """判断修复记录是否存在待核实的不确定状态"""
    if not r:
        return False
    uncertain_states = {"history-delete-pending", "history-deleted", "transfer-pending", "uncertain"}
    if r.get("state") in uncertain_states:
        return True
    items = r.get("items")
    if isinstance(items, list):
        return any(i.get("state") in uncertain_states for i in items if isinstance(i, dict))
    return False


def get_repair_label(r: Optional[Dict[str, Any]]) -> str:
    """获取友好的中文状态标签"""
    if not r:
        return "未记录"
    return REPAIR_LABELS.get(r.get("state"), "未记录")


def verify_repair_state(
    r: Optional[Dict[str, Any]],
    episodes: List[int],
    at: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    闭环验证：核实网盘中实际存在的集号是否已包含修复涉及的集号
    供阶段三（复核清理）使用
    """
    if not r or is_repair_uncertain(r) or not r.get("episodes"):
        return r

    now_iso = at or datetime.now().isoformat()
    repair_eps = r.get("episodes", [])
    all_present = all(ep in episodes for ep in repair_eps)
    if not all_present:
        return r

    res = dict(r)
    res["state"] = "files-verified"
    res["at"] = now_iso
    res["verifiedAt"] = now_iso
    res["note"] = "复核阶段确认网盘修复集号已实际出现，允许与同版本及其他集一同复核"
    return res
