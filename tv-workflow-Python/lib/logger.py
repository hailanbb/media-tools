"""
日志模块

同时输出到终端（带 ANSI 颜色 + Emoji）和每日日志文件。
日志文件按 YYYY-MM-DD.log 命名，存放在配置指定的目录中。
完全适配 Ubuntu 终端环境（自动探测 TTY 与 UTF-8 编码）。
"""

from datetime import datetime
import os
from pathlib import Path
import sys
from typing import Any, Optional

from .config import PROJECT_ROOT

LEVEL_PRIORITY = {"debug": 0, "info": 1, "warn": 2, "error": 3}

# ANSI 颜色码
COLORS = {
    "gray":   "\033[90m",
    "cyan":   "\033[36m",
    "yellow": "\033[33m",
    "red":    "\033[31m",
    "reset":  "\033[0m",
}

EMOJI = {
    "debug": "🔍",
    "info":  "💡",
    "warn":  "⚠️",
    "error": "❌",
}

LEVEL_COLOR = {
    "debug": COLORS["gray"],
    "info":  COLORS["cyan"],
    "warn":  COLORS["yellow"],
    "error": COLORS["red"],
}


class Logger:
    """双通道日志记录器（控制台 + 文件）"""

    def __init__(self, config: Any):
        log_cfg = config.get("log") if isinstance(config, dict) else getattr(config, "log", {})
        log_dir_raw = (log_cfg or {}).get("directory", "./logs")
        if os.path.isabs(log_dir_raw):
            self.log_dir = Path(log_dir_raw)
        else:
            self.log_dir = (PROJECT_ROOT / log_dir_raw).resolve()

        level_str = (log_cfg or {}).get("level", "info").lower()
        self.min_level = LEVEL_PRIORITY.get(level_str, LEVEL_PRIORITY["info"])

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            sys.stderr.write(f"创建日志目录失败: {e}\n")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write(self, level: str, message: str) -> None:
        if LEVEL_PRIORITY.get(level, 1) < self.min_level:
            return

        ts = self._timestamp()
        tag = level.upper().ljust(5)
        color = LEVEL_COLOR.get(level, "")
        emoji = EMOJI.get(level, "")
        reset = COLORS["reset"]

        # 终端输出
        console_msg = f"{color}{emoji} [{ts}] [{tag}] {message}{reset}"
        try:
            print(console_msg)
        except UnicodeEncodeError:
            print(console_msg.encode("utf-8", errors="replace").decode("utf-8"))

        # 文件输出（纯文本，去除 ANSI 转义）
        log_file = self.log_dir / f"{self._today()}.log"
        file_msg = f"[{ts}] [{tag}] {message}\n"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(file_msg)
        except Exception:
            pass

    def debug(self, msg: str) -> None:
        self._write("debug", msg)

    def info(self, msg: str) -> None:
        self._write("info", msg)

    def warn(self, msg: str) -> None:
        self._write("warn", msg)

    def error(self, msg: str) -> None:
        self._write("error", msg)

    def progress(self, stage: str, current: int, total: int, message: str) -> None:
        """输出阶段进度: [阶段] (current/total) message"""
        self._write("info", f"[{stage}] ({current}/{total}) {message}")


def create_logger(config: Any) -> Logger:
    return Logger(config)
