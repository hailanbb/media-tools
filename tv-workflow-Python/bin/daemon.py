#!/usr/bin/env python3
"""
剧集整理工作流 — 交互式控制台菜单

提供直观的终端编号选择菜单，支持手动触发各阶段任务与在线更新。
已精简为纯按需运行工具，无常驻后台守护进程与定时轮询开销。
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Optional

# 确保 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.config import PROJECT_ROOT as ROOT_DIR, load_config
from lib.logger import create_logger
from lib.platform_support.interactive_inspect import handle_interactive_inspect
from lib.platform_support.notify import notify
from lib.stages.dispatcher import run_auto, run_stage

os.chdir(ROOT_DIR)

BANNER = f"""
{'═' * 50}
  🎬 剧集整理工作流 (Python 版) — 控制台交互菜单
{'═' * 50}"""

MENU = f"""
  [1] 选剧/分类初检（浏览选择媒体库、分类与剧集，支持多选）
  [2] 立即补缺下载（含自动整理修复）
  [r] MoviePilot 整理历史专项修复
  [3] 立即复核清理（预览模式）
  [4] 立即执行归档（预览模式）
  [5] 复核清理（执行删除）
  [6] 执行归档（实际移动并自动对账复核）
  [v] 归档后对账复核（验证目标完整性与清理残留空别名）
  [7] 全自动流程 (初检→下载→复核→归档)
  [8] 查看最新报表路径
  [9] 查看最新运行日志
  [u] 一键在线更新程序（同步最新代码并保留配置）
  [0] 退出
{'═' * 50}"""


def show_latest_report(logger: Any) -> None:
    reports_dir = ROOT_DIR / "reports"
    if not reports_dir.exists():
        print("  📭 暂无报表目录")
        return
    files = [f for f in reports_dir.iterdir() if f.is_file() and f.name.endswith(".xlsx") and not f.name.startswith("~$")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        print("  📭 暂无报表文件")
        return
    latest = files[0]
    print(f"  📄 最新报表: {latest}")
    if shutil_which := getattr(__import__("shutil"), "which"):
        if shutil_which("xdg-open") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            try:
                subprocess.Popen(["xdg-open", str(latest)])
            except Exception:
                pass


def show_latest_log(logger: Any) -> None:
    logs_dir = ROOT_DIR / "logs"
    if not logs_dir.exists():
        print("  📭 暂无日志目录")
        return
    files = [f for f in logs_dir.iterdir() if f.is_file() and f.name.endswith(".log")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        print("  📭 暂无日志文件")
        return
    latest = files[0]
    print(f"  📄 最新日志文件: {latest} (末尾 20 行):")
    print("-" * 50)
    try:
        with open(latest, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in lines[-20:]:
                sys.stdout.write("  " + line)
    except Exception as e:
        print(f"  读取日志失败: {e}")
    print("-" * 50)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    try:
        config = load_config(config_path)
    except Exception as e:
        sys.stderr.write(f"❌ 配置加载失败: {e}\n")
        sys.stderr.write("请复制 config.example.json 为 config.json 并填写参数\n")
        sys.exit(1)

    logger = create_logger(config)

    def auto_with_notify() -> Any:
        try:
            res = run_auto(config, logger)
            notif_cfg = config.get("notifications", {}) if isinstance(config, dict) else getattr(config, "notifications", {})
            if notif_cfg.get("enabled", True):
                notify("✅ 全自动流程完成", "初检→下载→清理→归档 已执行", config)
            return res
        except Exception as e:
            notif_cfg = config.get("notifications", {}) if isinstance(config, dict) else getattr(config, "notifications", {})
            if notif_cfg.get("enabled", True):
                notify("❌ 全自动流程失败", str(e)[:150], config)
            raise

    # 退出信号处理
    def handle_exit(signum: int, frame: Any) -> None:
        logger.info("\n👋 控制台退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # 非 TTY 终端环境阻断
    if not sys.stdin.isatty():
        sys.stderr.write("请在交互式终端中执行，或使用 CLI 命令行参数，例如: tv inspect -i 或 tv download\n")
        sys.exit(0)

    print(BANNER)
    print(MENU)

    busy = False

    while True:
        try:
            sys.stdout.write("  请选择 > ")
            sys.stdout.flush()
            line = sys.stdin.readline()
            if not line:
                break
            choice = line.strip().lower()

            if busy:
                print("  ⏳ 上一个任务仍在执行中，请稍候...")
                continue

            if choice == "0":
                logger.info("👋 再见")
                break

            busy = True
            try:
                if choice in ("1", "s"):
                    handle_interactive_inspect(config, logger)
                elif choice == "2":
                    run_stage("download", config, logger)
                elif choice == "r":
                    run_stage("repair", config, logger)
                elif choice == "3":
                    run_stage("review", config, logger, {"yes": False})
                elif choice == "4":
                    run_stage("archive", config, logger, {"yes": False})
                elif choice == "5":
                    run_stage("review", config, logger, {"yes": True})
                elif choice == "6":
                    run_stage("archive", config, logger, {"yes": True})
                elif choice == "v":
                    run_stage("verify", config, logger)
                elif choice == "7":
                    auto_with_notify()
                elif choice == "8":
                    show_latest_report(logger)
                elif choice == "9":
                    show_latest_log(logger)
                elif choice in ("u", "update"):
                    update_script = ROOT_DIR / "update.sh"
                    if update_script.exists():
                        subprocess.run(["bash", str(update_script)], check=False)
                    else:
                        subprocess.run([
                            "bash", "-c",
                            "curl -sSL https://ghfast.top/https://raw.githubusercontent.com/hailanbb/media-tools/main/tv-workflow-Python/update.sh -o update.sh && chmod +x update.sh && bash update.sh"
                        ], check=False)
                else:
                    print("  ❓ 无效选择，请输入菜单中的序号或指令")
            except Exception as e:
                logger.error(f"执行失败: {e}")
            finally:
                busy = False
                print(MENU)

        except (KeyboardInterrupt, EOFError):
            logger.info("\n👋 控制台退出")
            break


if __name__ == "__main__":
    main()
