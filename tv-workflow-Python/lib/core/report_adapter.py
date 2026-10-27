"""
Excel 报表适配器（无头模式，基于 openpyxl）

提供剧集整理工作流所需的 .xlsx 报表读取与回写能力。
支持：
- 初检全新报表创建
- 补缺/复核/归档三阶段结果回写
- 单元格 RGB/HSV 颜色识别与高保真填色
- MoviePilot 整理修复列与下载记录分片存取
"""

import copy
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .repair_state import get_repair_label

COLORS = {
    "blue":     {"bg": "BDD7EE", "fg": "1F4E78"},  # 连载中
    "yellow":   {"bg": "FFFF00", "fg": "000000"},  # 待复核/待下载/待清理
    "red":      {"bg": "FFC7CE", "fg": "9C0006"},  # 无合格资源/空目录
    "green":    {"bg": "C6EFCE", "fg": "006100"},  # 已播完且齐全
    "archived": {"bg": "E4DFEC", "fg": "7030A0"},  # 已归档（紫色）
}


def hex_to_hsv(hex_str: str) -> Tuple[float, float, float]:
    """将十六进制 RGB 转换为 HSV"""
    clean_hex = str(hex_str).upper()
    if len(clean_hex) == 8 and clean_hex.startswith("FF"):
        clean_hex = clean_hex[2:]
    if len(clean_hex) != 6:
        return 0.0, 0.0, 0.0

    try:
        r = int(clean_hex[0:2], 16) / 255.0
        g = int(clean_hex[2:4], 16) / 255.0
        b = int(clean_hex[4:6], 16) / 255.0
    except ValueError:
        return 0.0, 0.0, 0.0

    max_c = max(r, g, b)
    min_c = min(r, g, b)
    d = max_c - min_c

    h = 0.0
    if d != 0:
        if max_c == r:
            h = 60.0 * (((g - b) / d) % 6)
        elif max_c == g:
            h = 60.0 * ((b - r) / d + 2)
        else:
            h = 60.0 * ((r - g) / d + 4)
    if h < 0:
        h += 360.0

    s = 0.0 if max_c == 0 else d / max_c
    v = max_c
    return h, s, v


def detect_color(cell: Any) -> str:
    """从单元格填充样式中识别语义颜色类别"""
    if not cell or not hasattr(cell, "fill") or not cell.fill:
        return "other"

    fill = cell.fill
    rgb = None
    if hasattr(fill, "start_color") and fill.start_color and hasattr(fill.start_color, "rgb"):
        rgb = str(fill.start_color.rgb)
    elif hasattr(fill, "fgColor") and fill.fgColor and hasattr(fill.fgColor, "rgb"):
        rgb = str(fill.fgColor.rgb)

    if not rgb or rgb == "00000000" or rgb == "None":
        return "other"

    rgb_upper = rgb.upper()
    if len(rgb_upper) == 8 and rgb_upper.startswith("FF"):
        rgb_upper = rgb_upper[2:]

    if rgb_upper == "E4DFEC":
        return "archived"

    h, s, v = hex_to_hsv(rgb_upper)
    if 75 <= h <= 170 and s >= 0.08 and v >= 0.4:
        return "green"
    if 40 <= h <= 70 and s >= 0.15 and v >= 0.7:
        return "yellow"
    if 190 <= h <= 250 and s >= 0.1:
        return "blue"
    if (h >= 340 or h <= 20) and s >= 0.15:
        return "red"

    return "other"


def build_fill(color_name: str) -> Optional[PatternFill]:
    c = COLORS.get(color_name)
    if not c:
        return None
    hex_color = f"FF{c['bg']}"
    return PatternFill(fill_type="solid", start_color=hex_color, end_color=hex_color)


def build_font(color_name: str) -> Font:
    c = COLORS.get(color_name)
    if not c:
        return Font(name="Microsoft YaHei", size=10)
    return Font(
        name="Microsoft YaHei",
        size=10,
        color=f"FF{c['fg']}",
        bold=(color_name in ("yellow", "archived"))
    )


class TaskItem:
    """行任务对象"""
    def __init__(self, **kwargs: Any):
        for k, v in kwargs.items():
            setattr(self, k, v)


class ReportSession:
    """报表会话上下文"""
    def __init__(
        self,
        tasks: List[TaskItem],
        errors: List[Dict[str, Any]],
        skipped: int,
        workbook: Optional[openpyxl.Workbook] = None,
        file_path: Optional[str] = None,
        mode: str = "download"
    ):
        self.tasks = tasks
        self.errors = errors
        self.skipped = skipped
        self.workbook = workbook
        self.file_path = file_path
        self.mode = mode
        self.updates: Dict[TaskItem, Dict[str, Any]] = {}


def read_report(
    file_path: str,
    origin: Optional[str] = None,
    source: Optional[str] = None,
    mode: str = "download"
) -> ReportSession:
    """读取报表并构建任务列表"""
    abs_path = Path(file_path).resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"报表文件不存在: {abs_path}")

    wb = openpyxl.load_workbook(str(abs_path), data_only=True)
    tasks: List[TaskItem] = []
    errors: List[Dict[str, Any]] = []
    skipped = 0

    for sheet_name in wb.sheetnames:
        if re.search(r"明细|日志|Audit", sheet_name, re.IGNORECASE):
            continue

        sheet = wb[sheet_name]
        if sheet.max_row < 2:
            continue

        # 解析表头
        headers = []
        for col_idx in range(1, sheet.max_column + 1):
            val = sheet.cell(row=1, column=col_idx).value
            headers.append(str(val).strip() if val is not None else "")

        if "剧集目录" not in headers:
            continue
        dir_col_idx = headers.index("剧集目录") + 1

        for r in range(2, sheet.max_row + 1):
            name_cell = sheet.cell(row=r, column=dir_col_idx)
            name = str(name_cell.value or "").strip()
            if not name:
                skipped += 1
                continue

            color = detect_color(name_cell)

            # 模式过滤
            if mode == "download" and color in ("green", "archived"):
                skipped += 1
                continue
            if mode == "review" and color != "yellow":
                skipped += 1
                continue

            # 构建行数据映射
            row_data: Dict[str, Any] = {}
            for col_idx, h in enumerate(headers, start=1):
                if h:
                    val = sheet.cell(row=r, column=col_idx).value
                    row_data[h] = val if val is not None else ""

            category = str(row_data.get("分类") or "").strip()
            season_raw = row_data.get("季数") if "季数" in row_data else row_data.get("季")
            season = None
            if season_raw is not None and str(season_raw).strip() != "":
                try:
                    season = int(season_raw)
                except ValueError:
                    pass

            raw_path = str(row_data.get("季目录路径") or row_data.get("源云存储链接") or "")
            path_match = re.search(r"[?&]path=([^&]+)", raw_path)
            extracted_path = urllib.parse.unquote(path_match.group(1)) if path_match else raw_path
            show_path = f"{source.rstrip('/')}/{category}/{name}" if (source and category and name) else ""

            task = TaskItem(
                name=name,
                category=category,
                season=season,
                path=extracted_path,
                showPath=show_path,
                key=f"{sheet_name}:{r}",
                data=row_data,
                color=color,
                sheetName=sheet_name,
                row=r,
                cellAddress=f"{get_column_letter(dir_col_idx)}{r}",
            )
            tasks.append(task)

    return ReportSession(
        tasks=tasks,
        errors=errors,
        skipped=skipped,
        workbook=wb,
        file_path=str(abs_path),
        mode=mode
    )


def write_report(session: ReportSession, output_path: str) -> str:
    """回写报表或创建新报表"""
    abs_output = Path(output_path).resolve()
    abs_output.parent.mkdir(parents=True, exist_ok=True)

    # 初检模式或无现有工作簿
    if session.mode == "inspect" or session.workbook is None:
        return create_new_report(session.tasks, str(abs_output))

    wb = session.workbook

    for task in session.tasks:
        result = session.updates.get(task)
        if not result:
            continue

        sheet_name = getattr(task, "sheetName", None)
        if not sheet_name or sheet_name not in wb.sheetnames:
            continue

        sheet = wb[sheet_name]
        row_idx = task.row

        # 解析现有表头映射
        header_map: Dict[str, int] = {}
        for col_idx in range(1, sheet.max_column + 1):
            val = sheet.cell(row=1, column=col_idx).value
            if val is not None:
                header_map[str(val).strip()] = col_idx

        def ensure_column(col_name: str) -> int:
            if col_name in header_map:
                return header_map[col_name]
            new_col = sheet.max_column + 1
            header_cell = sheet.cell(row=1, column=new_col)
            header_cell.value = col_name
            header_cell.font = Font(name="Microsoft YaHei", size=10, bold=True)
            header_cell.fill = PatternFill(fill_type="solid", start_color="FFE8EEF3", end_color="FFE8EEF3")
            header_cell.alignment = Alignment(horizontal="center", vertical="center")
            header_map[col_name] = new_col
            return new_col

        # 映射回写字段
        field_map = dict(result.get("fields") or {})
        state = result.get("state")
        if state:
            if "处理结果" in header_map:
                field_map["处理结果"] = state
            if session.mode == "download":
                field_map["下载处理"] = state
            elif session.mode == "review":
                field_map["复核结果"] = state
            elif session.mode == "archive":
                field_map["归档结果"] = state

        note = result.get("note")
        if note:
            if session.mode == "download":
                field_map["下载说明"] = note
            elif session.mode == "review":
                field_map["复核说明"] = note
            elif session.mode == "archive":
                field_map["归档说明"] = note

        if result.get("fileCheckedAt"):
            field_map["文件核对时间"] = result["fileCheckedAt"]

        # 整理修复字段处理
        if result.get("repair"):
            r = result["repair"]
            field_map["修复状态"] = get_repair_label(r)
            eps = r.get("episodes") or []
            field_map["修复集号"] = "、".join(str(e) for e in eps)
            field_map["修复时间"] = r.get("at") or ""
            field_map["修复说明"] = r.get("note") or ""

            repair_json = json.dumps(r, ensure_ascii=False)
            field_map["修复记录"] = repair_json[:30000]
            if len(repair_json) > 30000:
                field_map["修复记录续1"] = repair_json[30000:60000]

        # 下载记录处理
        if result.get("records"):
            rec_json = json.dumps(result["records"], ensure_ascii=False)
            field_map["下载记录"] = rec_json[:30000]

        # 回写字段值
        for col_name, val in field_map.items():
            if val is None:
                continue
            col_num = ensure_column(col_name)
            target_cell = sheet.cell(row=row_idx, column=col_num)
            target_cell.value = val
            target_cell.font = Font(name="Microsoft YaHei", size=9)

        # 更新填色
        target_color = result.get("color")
        if target_color and target_color in COLORS:
            name_col = header_map.get("剧集目录", 2)
            name_cell = sheet.cell(row=row_idx, column=name_col)
            name_cell.fill = build_fill(target_color)
            name_cell.font = build_font(target_color)

    wb.save(str(abs_output))
    return str(abs_output)


def create_new_report(tasks: List[Any], output_path: str) -> str:
    """初检阶段从无到有创建全新报表"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "剧集清单"

    headers = [
        "分类", "剧集目录", "季数", "源云存储链接",
        "视频文件数", "已识别集数", "来源登记集数",
        "已识别集号", "重复集号", "缺失集号", "无法识别集号的文件",
        "TMDB ID", "数据来源", "播出状态", "处理结果",
        "待播集号", "播期未知集号", "报告版本"
    ]

    # 写入表头
    ws.append(headers)
    for c_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c_idx)
        cell.font = Font(name="Microsoft YaHei", size=10, bold=True, color="FF1F3864")
        cell.fill = PatternFill(fill_type="solid", start_color="FFD9E1F2", end_color="FFD9E1F2")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 写入数据行
    dir_col_idx = headers.index("剧集目录") + 1
    for r_idx, t in enumerate(tasks, start=2):
        row_dict = getattr(t, "data", {}) or {}
        row_vals = [row_dict.get(h, "") for h in headers]
        ws.append(row_vals)

        # 单元格字体与填色
        c_name = getattr(t, "color", "other")
        fill = build_fill(c_name)
        font = build_font(c_name)
        if fill:
            cell = ws.cell(row=r_idx, column=dir_col_idx)
            cell.fill = fill
            cell.font = font

    # 设置列宽
    for col_idx, h in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        if re.search(r"链接|目录|文件", h):
            ws.column_dimensions[col_letter].width = 35
        elif re.search(r"集号|说明", h):
            ws.column_dimensions[col_letter].width = 22
        else:
            ws.column_dimensions[col_letter].width = 14

    wb.save(output_path)
    return output_path
