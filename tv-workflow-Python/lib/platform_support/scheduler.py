"""
定时调度器

支持标准 5 段 cron 表达式（如 "0 2 * * *"），每日定时在后台执行全流程。
优先使用 croniter 精确计算下一次触发时间，无依赖时自动降级到内置简易解析器。
"""

from datetime import datetime, timedelta
import threading
import time
from typing import Any, Callable, Optional, Tuple


def _parse_simple_cron(expr: str) -> Tuple[int, int]:
    """解析简易 cron 表达式 '分 时 * * *'"""
    parts = str(expr).strip().split()
    if len(parts) < 2:
        return 0, 2
    try:
        m = int(parts[0])
        h = int(parts[1])
        return m, h
    except ValueError:
        return 0, 2


class Scheduler:
    """定时调度器"""

    def __init__(self, config: Any, logger: Any, run_auto: Callable[[], Any]):
        self.config = config
        self.logger = logger
        self.run_auto = run_auto
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._stopped = False

    def _next_run(self) -> Tuple[datetime, float]:
        cron_expr = "0 2 * * *"
        sched_cfg = self.config.get("schedule") if isinstance(self.config, dict) else getattr(self.config, "schedule", {})
        if isinstance(sched_cfg, dict):
            cron_expr = sched_cfg.get("cron", "0 2 * * *")

        now = datetime.now()
        try:
            from croniter import croniter
            cron = croniter(cron_expr, now)
            next_dt = cron.get_next(datetime)
            diff_sec = max(1.0, (next_dt - now).total_seconds())
            return next_dt, diff_sec
        except Exception:
            minute, hour = _parse_simple_cron(cron_expr)
            next_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_dt <= now:
                next_dt += timedelta(days=1)
            diff_sec = max(1.0, (next_dt - now).total_seconds())
            return next_dt, diff_sec

    def _tick(self) -> None:
        if self._stopped or self._running:
            return

        self._running = True
        try:
            self.logger.info("⏰ 定时任务触发，开始执行全自动流程")
            self.run_auto()
            self.logger.info("✅ 定时任务执行完成")
        except Exception as e:
            self.logger.error(f"❌ 定时任务执行失败: {e}")
        finally:
            self._running = False
            if not self._stopped:
                self._schedule_next()

    def _schedule_next(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

        if self._stopped:
            return

        next_dt, diff_sec = self._next_run()
        minutes = round(diff_sec / 60)
        self.logger.info(f"📅 下次定时执行: {next_dt.strftime('%Y-%m-%d %H:%M:%S')}（约 {minutes} 分钟后）")

        self._timer = threading.Timer(diff_sec, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def start(self) -> None:
        sched_cfg = self.config.get("schedule") if isinstance(self.config, dict) else getattr(self.config, "schedule", {})
        enabled = sched_cfg.get("enabled", False) if isinstance(sched_cfg, dict) else False

        if not enabled:
            self.logger.info("📅 定时任务未启用 (config.schedule.enabled = false)")
            return

        self._stopped = False
        self.logger.info("📅 定时调度器已启动")
        self._schedule_next()

    def stop(self) -> None:
        self._stopped = True
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.logger.info("📅 定时调度器已停止")


def create_scheduler(config: Any, logger: Any, run_auto: Callable[[], Any]) -> Scheduler:
    return Scheduler(config, logger, run_auto)
