#!/usr/bin/env python3
"""
剧集整理工作流 — CLI 命令行主入口

用法:
  python3 bin/tv_workflow.py <命令> [选项]
  或
  ./bin/tv_workflow.py <命令> [选项]
"""

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import time

# 确保 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 将项目根目录加入模块搜索路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.config import PROJECT_ROOT as ROOT_DIR, load_config
from lib.logger import create_logger
from lib.platform_support.interactive_inspect import handle_interactive_inspect
from lib.platform_support.notify import notify
from lib.stages.dispatcher import run_stage

# 切换工作目录到项目根目录
os.chdir(ROOT_DIR)

USAGE_TEXT = """
🎬 剧集整理工作流 (Python 版)

用法:
  python3 bin/tv_workflow.py <命令> [选项]

命令:
  inspect              ① 剧集初检（支持交互式 -i 或指定 --category/--show/--path）
  download [--report]  ② 补缺下载（含自动整理修复与资源搜索）
  repair [--report]    🔧 整理历史修复（仅检查并解除 MoviePilot 阻塞历史重转）
  review [--report]    ③ 复核清理（核验修复闭环，清理冗余版本）
  archive [--report]   ④ 归档（移动完结剧集到目标目录，含自动对账复核）
  verify [--report]    🔍 归档后对账复核（验证目标完整性、源存量并清理残留空别名）
  update               🔄 一键在线更新程序（同步最新代码并保留配置）
  auto                 🤖 全自动流程（依次执行全流程）

选项:
  --config <path>      指定配置文件 (默认: config.json)
  --report <path>      指定输入报告路径 (.xlsx)
  --path <dirPath>     指定网盘扫描目录（可为单部剧、单分类或根目录路径）
  --category <name>    仅初检指定分类（如: 日韩动漫、国产剧、欧美剧）
  --show <keyword>     仅初检匹配剧名关键字（如: 凡人修仙传）
  -i, --interactive    交互式浏览网盘分类与剧集文件夹后再初检
  -y, --yes            跳过预览，直接执行清理/归档
  -h, --help           显示此帮助信息

示例 (已禁用全库初检，需定向检测):
  python3 bin/tv_workflow.py inspect -i                          # 交互式选剧初检 (支持动漫/电视剧分级导航)
  python3 bin/tv_workflow.py inspect --category 日韩动漫          # 仅初检指定分类
  python3 bin/tv_workflow.py inspect --show 凡人修仙传            # 仅初检包含关键字的剧集
  python3 bin/tv_workflow.py inspect --path "/115/Library_incs/nas/动漫/日韩动漫/葬送的芙莉莲" # 精确扫描指定目录
  python3 bin/tv_workflow.py archive --yes                       # 执行归档并自动对账复核
  python3 bin/tv_workflow.py verify                              # 独立归档核验
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="剧集整理工作流 CLI",
        usage="python3 bin/tv_workflow.py <命令> [选项]",
        add_help=False
    )
    parser.add_argument("command", nargs="?", default=None, help="执行命令")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--report", default=None, help="输入报告路径")
    parser.add_argument("--path", default=None, help="指定网盘扫描目录")
    parser.add_argument("--category", default=None, help="指定分类")
    parser.add_argument("--show", default=None, help="指定剧名关键字")
    parser.add_argument("-i", "--interactive", action="store_true", help="交互式浏览选剧")
    parser.add_argument("-y", "--yes", action="store_true", help="直接执行清理/归档")
    parser.add_argument("-h", "--help", action="store_true", help="帮助信息")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.help or not args.command:
        print(USAGE_TEXT.strip())
        sys.exit(0 if args.help else 1)

    valid_commands = {"inspect", "download", "repair", "review", "archive", "verify", "audit", "auto", "update", "upgrade"}
    if args.command not in valid_commands:
        sys.stderr.write(f"❌ 未知命令: {args.command}\n\n")
        print(USAGE_TEXT.strip())
        sys.exit(1)

    if args.command in ("update", "upgrade"):
        import subprocess
        update_script = ROOT_DIR / "update.sh"
        if update_script.exists():
            subprocess.run(["bash", str(update_script)], check=False)
        else:
            subprocess.run([
                "bash", "-c",
                "curl -sSL https://ghfast.top/https://raw.githubusercontent.com/hailanbb/media-tools/main/tv-workflow-Python/update.sh -o update.sh && chmod +x update.sh && bash update.sh"
            ], check=False)
        sys.exit(0)

    # 加载配置
    try:
        config = load_config(args.config)
    except Exception as e:
        sys.stderr.write(f"❌ 配置加载失败: {e}\n")
        sys.exit(1)

    logger = create_logger(config)
    start_time = datetime.now()
    logger.info(f"🕐 启动时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    flags = []
    if args.interactive:
        flags.append("--interactive")
    if args.report:
        flags.append(f"--report {args.report}")
    if args.yes:
        flags.append("--yes")
    flag_str = (" " + " ".join(flags)) if flags else ""
    logger.info(f"📌 命令: {args.command}{flag_str}")

    # 交互式选剧初检
    if args.command == "inspect" and (args.interactive or (not args.path and not args.category and not args.show and sys.stdin.isatty())):
        try:
            handle_interactive_inspect(config, logger)
        except Exception as e:
            logger.error(f"交互式初检异常: {e}")
            sys.exit(1)
        sys.exit(0)

    try:
        options = {
            "reportPath": args.report,
            "path": args.path,
            "category": args.category,
            "show": args.show,
            "yes": args.yes,
        }
        result = run_stage(args.command, config, logger, options)

        elapsed = f"{(datetime.now() - start_time).total_seconds():.1f}"
        summary_msg = f"⏱ 耗时 {elapsed} 秒"
        if isinstance(result, dict) and result.get("outputPath"):
            summary_msg += f"\n📄 报告: {result['outputPath']}"
        logger.info(summary_msg)

        # 发送通知
        notif_cfg = config.get("notifications", {}) if isinstance(config, dict) else getattr(config, "notifications", {})
        if notif_cfg.get("enabled", True):
            notify("✅ 剧集整理完成", summary_msg.replace("\n", " ")[:150], config)

        sys.exit(0)

    except Exception as e:
        logger.error(f"\n💥 执行失败: {e}")
        notif_cfg = config.get("notifications", {}) if isinstance(config, dict) else getattr(config, "notifications", {})
        if notif_cfg.get("enabled", True):
            notify("❌ 剧集整理失败", f"{args.command}: {e}"[:150], config)
        sys.exit(1)


if __name__ == "__main__":
    main()
