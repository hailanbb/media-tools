"""
阶段调度入口

统一分发各阶段函数，并提供全自动流程编排与独立对账复核调用。
"""

from datetime import datetime
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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


def extract_report_time(file_path: Path) -> float:
    """从报表文件名中精准提取时间戳（优先从 ISO 时间解析，次选文件修改时间）"""
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}[-:]\d{2}[-:]\d{2})", file_path.name)
    if m:
        t_str = m.group(1).replace("-", ":")
        try:
            norm = re.sub(
                r"^(\d{4}):(\d{2}):(\d{2})T(\d{2}):(\d{2}):(\d{2})$",
                r"\1-\2-\3T\4:\5:\6",
                t_str
            )
            return datetime.fromisoformat(norm).timestamp()
        except Exception:
            pass
    try:
        return file_path.stat().st_mtime
    except Exception:
        return 0.0


def _filter_by_stage(files: List[Path], stage_key: str) -> Optional[Path]:
    if not stage_key:
        return files[0] if files else None

    filtered: List[Path] = []
    if stage_key == "初检":
        filtered = [f for f in files if f.name.startswith("初检报告") and not re.search(r"补缺|复核|归档", f.name)]
    elif stage_key == "补缺":
        filtered = [f for f in files if "补缺" in f.name]
    elif stage_key == "复核":
        filtered = [f for f in files if "复核" in f.name]
    elif stage_key in ("归档", "归档完成"):
        filtered = [f for f in files if "归档" in f.name]
    else:
        filtered = [f for f in files if stage_key in f.name]

    if filtered:
        return filtered[0]

    # 回退策略：取最新的一份未归档报表
    fallback = next((f for f in files if "归档" not in f.name), files[0] if files else None)
    return fallback


def find_latest_report(stage_key: str = "", keyword: Optional[str] = None) -> Optional[str]:
    """
    智能搜寻 reports/ 及项目根目录下时间戳最新的匹配报表
    - stage_key: 阶段标识 ('初检' | '补缺' | '复核' | '归档' 等)
    - keyword: 剧名/分类关键字过滤（若提供，则优先挑选该剧最新的报表）
    """
    dirs = [(PROJECT_ROOT / "reports").resolve(), PROJECT_ROOT.resolve()]
    candidate_files: List[Path] = []
    seen: Set[str] = set()

    for d in dirs:
        if not d.exists():
            continue
        try:
            for f in d.iterdir():
                if f.is_file() and f.name.endswith(".xlsx") and not f.name.startswith("~$"):
                    res_p = str(f.resolve())
                    if res_p not in seen:
                        seen.add(res_p)
                        candidate_files.append(f)
        except Exception:
            continue

    if not candidate_files:
        return None

    # 按时间戳从最新到最旧绝对排序
    candidate_files.sort(key=extract_report_time, reverse=True)

    # 1. 优先按剧名/关键字过滤
    if keyword:
        kw = str(keyword).strip().lower()
        kw_files = [f for f in candidate_files if kw in f.name.lower()]
        if kw_files:
            match = _filter_by_stage(kw_files, stage_key)
            if match:
                return str(match)

    # 2. 全局匹配对应阶段最新的报表
    match = _filter_by_stage(candidate_files, stage_key)
    return str(match) if match else None


def run_stage(stage_name: str, config: Any, logger: Any, options: Optional[Dict[str, Any]] = None) -> Any:
    """按名称执行指定阶段"""
    opts = options or {}
    start_time = time.time()

    line_sep = "═" * 50
    logger.info(f"\n{line_sep}")
    logger.info(f"🎬 开始执行: {stage_label(stage_name)}")
    logger.info(f"{line_sep}\n")

    # 提取剧名/分类关键字用于智能匹配最新报表
    kw = opts.get("show") or opts.get("category")
    if not kw and opts.get("path"):
        p_clean = str(opts["path"]).rstrip("/").split("/")[-1]
        kw = re.sub(r"\[tmdbid=\d+\]", "", p_clean, flags=re.I).strip()
        kw = re.sub(r"[（(](?:19|20)\d{2}[)）]", "", kw).strip()

    result = None
    try:
        if stage_name == "inspect":
            result = run_inspect(config, logger, opts)
        elif stage_name == "download":
            rep = (
                opts.get("reportPath")
                or find_latest_report("初检", keyword=kw)
                or find_latest_report("", keyword=kw)
                or find_latest_report("初检")
                or find_latest_report("")
            )
            logger.info(f"📄 依据最新报表执行: {rep}")
            result = run_download(config, logger, rep)
        elif stage_name == "repair":
            rep = (
                opts.get("reportPath")
                or find_latest_report("初检", keyword=kw)
                or find_latest_report("", keyword=kw)
                or find_latest_report("初检")
                or find_latest_report("")
            )
            logger.info(f"📄 依据最新报表执行: {rep}")
            result = run_download(config, logger, rep, {"repairOnly": True})
        elif stage_name == "review":
            rep = (
                opts.get("reportPath")
                or find_latest_report("补缺", keyword=kw)
                or find_latest_report("初检", keyword=kw)
                or find_latest_report("", keyword=kw)
                or find_latest_report("补缺")
                or find_latest_report("初检")
                or find_latest_report("")
            )
            logger.info(f"📄 依据最新报表执行: {rep}")
            result = run_review(config, logger, rep, {"dryRun": not opts.get("yes", False)})
        elif stage_name == "archive":
            rep = (
                opts.get("reportPath")
                or find_latest_report("复核清理", keyword=kw)
                or find_latest_report("补缺", keyword=kw)
                or find_latest_report("初检", keyword=kw)
                or find_latest_report("", keyword=kw)
                or find_latest_report("复核清理")
                or find_latest_report("补缺")
                or find_latest_report("初检")
                or find_latest_report("")
            )
            logger.info(f"📄 依据最新报表执行: {rep}")
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
