/**
 * 简易定时调度器
 * 支持 "分 时 * * *" 格式的 cron 表达式，用于每日定时执行全流程
 */

/**
 * 创建调度器
 * @param {object} config - 配置对象
 * @param {object} logger
 * @param {() => Promise<void>} runAuto - 全自动流程函数
 * @returns {{ start: () => void, stop: () => void }}
 */
export function createScheduler(config, logger, runAuto) {
  let timer = null;
  let running = false;

  /** 解析简单 cron 表达式 "分 时 * * *" */
  function parseCron(expr) {
    const parts = String(expr).trim().split(/\s+/);
    if (parts.length < 2) return { minute: 0, hour: 2 };
    return {
      minute: parseInt(parts[0], 10) || 0,
      hour: parseInt(parts[1], 10) || 2,
    };
  }

  /** 计算距下次执行的毫秒数 */
  function msUntilNext() {
    const { minute, hour } = parseCron(config.schedule?.cron || '0 2 * * *');
    const now = new Date();
    const next = new Date(now);
    next.setHours(hour, minute, 0, 0);
    if (next <= now) next.setDate(next.getDate() + 1);
    return next.getTime() - now.getTime();
  }

  /** 获取下次执行时间的可读字符串 */
  function nextRunTime() {
    const { minute, hour } = parseCron(config.schedule?.cron || '0 2 * * *');
    const now = new Date();
    const next = new Date(now);
    next.setHours(hour, minute, 0, 0);
    if (next <= now) next.setDate(next.getDate() + 1);
    return next.toLocaleString('zh-CN', { hour12: false });
  }

  async function tick() {
    if (running) return;
    running = true;
    try {
      logger.info(`⏰ 定时任务触发，开始执行全自动流程`);
      await runAuto();
      logger.info(`✅ 定时任务执行完成`);
    } catch (e) {
      logger.error(`❌ 定时任务执行失败: ${e.message}`);
    } finally {
      running = false;
      schedule(); // 安排下一次
    }
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    const ms = msUntilNext();
    timer = setTimeout(tick, ms);
    logger.info(`📅 下次定时执行: ${nextRunTime()}（${Math.round(ms / 60000)} 分钟后）`);
  }

  return {
    start() {
      if (!config.schedule?.enabled) {
        logger.info('📅 定时任务未启用');
        return;
      }
      logger.info('📅 定时调度器已启动');
      schedule();
    },
    stop() {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      logger.info('📅 定时调度器已停止');
    }
  };
}
