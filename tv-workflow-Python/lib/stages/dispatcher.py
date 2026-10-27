"""
阶段调度入口

统一分发各阶段函数，并提供全自动流程编排与独立对账复核调用。
"""

from datetime import datetime
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, Optional

from ..clients.clouddrive2 import CloudDriveClient
from ..config import PROJECT_ROOT
from ..core.report_adapter import read_report, write_report
from .archive import post_archive_review, run_archive
from .download import run_download
from .inspect import run_inspect
from .review import run_review


def stage_label(name: str) -> str:
    """阶段名称中文映射"""
    labels = {
        "inspect":  "① 剧集初检",
        "download": "② 补缺下载（含自动整理修复）",
        "repair":   "🔧 MoviePilot 整理历史修复",
        "review":   "③ 复核清理",
        "archive":  "④ 归档（含归档后对账复核）",
        "verify":   "🔍 归档后对账复核",
        "audit":    "🔍 归档后对账复核",
        "auto":     "🤖 全自动流程"
    }
    return labels.get(name, name)


def find_latest_report(stage_key: str = "") -> Optional[str]:
    """查找 reports/ 或项目根目录下最新的匹配 Excel 报告"""
    dirs = [(PROJECT_ROOT / "reports").resolve(), PROJECT_ROOT.resolve()]
    for d in dirs:
        if not d.exists():
            continue
        try:
            files = [
                f.name for f in d.iterdir()
                if f.is_file() and f.name.endswith(".xlsx") and not f.name.startswith("~$")
            ]
            files.sort(reverse=True)
            if not files:
                continue

            if not stage_key:
                return str(d / files[0])

            filtered = []
            if stage_key == "初检":
                filtered = [f for f in files if f.startswith("初检报告") and not re.search(r"补缺|复核|归档", f)]
            elif stage_key == "补缺":
                filtered = [f for f in files if "补缺" in f]
            elif stage_key == "复核":
                filtered = [f for f in files if "复核" in f]
            elif stage_key in ("归档", "归档完成"):
                filtered = [f for f in files if "归档" in f]
            else:
                filtered = [f for f in files if stage_key in f]

            if filtered:
                return str(d / filtered[0])

            fallback = next((f for f in files if "归档" not in f), files[0])
            if fallback:
                return str(d / fallback)
        except Exception:
            continue

    return None


def run_stage(stage_name: str, config: Any, logger: Any, options: Optional[Dict[str, Any]] = None) -> Any:
    """按名称执行指定阶段"""
    opts = options or {}
    start_time = time.time()

    line_sep = "═" * 50
    logger.info(f"\n{line_sep}")
    logger.info(f"🎬 开始执行: {stage_label(stage_name)}")
    logger.info(f"{line_sep}\n")

    result = None
    try:
        if stage_name == "inspect":
            result = run_inspect(config, logger, opts)
        elif stage_name == "download":
            rep = opts.get("reportPath") or find_latest_report("初检") or find_latest_report("")
            result = run_download(config, logger, rep)
        elif stage_name == "repair":
            rep = opts.get("reportPath") or find_latest_report("初检") or find_latest_report("")
            result = run_download(config, logger, rep, {"repairOnly": True})
        elif stage_name == "review":
            rep = (
                opts.get("reportPath")
                or find_latest_report("补缺")
                or find_latest_report("初检")
                or find_latest_report("")
            )
            result = run_review(config, logger, rep, {"dryRun": not opts.get("yes", False)})
        elif stage_name == "archive":
            rep = (
                opts.get("reportPath")
                or find_latest_report("复核清理")
                or find_latest_report("初检")
                or find_latest_report("")
            )
            result = run_archive(config, logger, rep, {"dryRun": not opts.get("yes", False)})
        elif stage_name in ("verify", "audit"):
            result = run_verify(config, logger, opts)
        elif stage_name == "auto":
            result = run_auto(config, logger)
        else:
            raise ValueError(f"未知阶段命令: {stage_name}")

    except Exception as e:
        logger.error(f"\n❌ {stage_label(stage_name)} 执行失败: {e}")
        raise

    elapsed = f"{time.time() - start_time:.1f}"
    logger.info(f"\n⏱ 耗时 {elapsed} 秒")
    return result


def run_auto(config: Any, logger: Any) -> Dict[str, Any]:
    """全自动流程：依次执行 初检 → 补缺下载 → 复核清理 → 归档"""
    logger.info("🤖 全自动流程启动\n")
    results: Dict[str, Any] = {}

    # ① 初检
    try:
        results["inspect"] = run_inspect(config, logger)
    except Exception as e:
        logger.error(f"初检失败，终止后续流程: {e}")
        return results

    # ② 补缺下载
    inspect_out = results.get("inspect", {}).get("outputPath")
    if inspect_out:
        try:
            results["download"] = run_download(config, logger, inspect_out)
        except Exception as e:
            logger.error(f"补缺下载失败: {e}")

    # ③ 复核清理
    review_in = (results.get("download") or {}).get("outputPath") or inspect_out
    if review_in:
        try:
            results["review"] = run_review(config, logger, review_in, {"dryRun": False})
        except Exception as e:
            logger.error(f"复核清理失败: {e}")

    # ④ 归档
    archive_in = (results.get("review") or {}).get("outputPath") or review_in
    if archive_in:
        try:
            results["archive"] = run_archive(config, logger, archive_in, {"dryRun": False})
        except Exception as e:
            logger.error(f"归档失败: {e}")

    logger.info("\n🤖 全自动流程完成")
    return results


def run_verify(config: Any, logger: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """独立执行归档后对账复核阶段"""
    opts = options or {}
    report_path = (
        opts.get("reportPath")
        or find_latest_report("归档完成")
        or find_latest_report("归档")
        or find_latest_report("")
    )
    if not report_path:
        raise ValueError("未找到可用的归档报告，请通过 --report 指定报告路径")

    cd2_cfg = config.get("clouddrive2", {}) if isinstance(config, dict) else getattr(config, "clouddrive2", {})
    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})

    cd2 = CloudDriveClient(
        origin=cd2_cfg.get("origin", ""),
        token=cd2_cfg.get("token", ""),
        logger=logger,
    )

    logger.info(f"📄 读取报告进行复核: {report_path}")
    session = read_report(
        file_path=report_path,
        origin=cd2_cfg.get("origin", ""),
        source=paths_cfg.get("source", ""),
        mode="archive"
    )
    session.updates = {}

    review_summary = post_archive_review(
        config=config, logger=logger, cd2=cd2, session=session, auto_clean_empty_aliases=True
    )

    output_path = re.sub(r"(?:_复核核验)*\.xlsx$", "_复核核验.xlsx", report_path, flags=re.I)
    write_report(session, output_path)
    logger.info(f"📄 复核核验报告已更新: {output_path}")

    return {**review_summary, "outputPath": output_path}
