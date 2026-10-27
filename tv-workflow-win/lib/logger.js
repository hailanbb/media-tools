/**
 * 日志模块
 *
 * 同时输出到控制台（带颜色 + Emoji）和每日日志文件。
 * 日志文件按 YYYY-MM-DD.log 命名，存放在配置指定的目录中。
 */

import { mkdirSync, appendFileSync } from 'node:fs';
import { resolve } from 'node:path';

// ── 日志级别优先级（数字越大越严重）──────────────────────────
const LEVEL_PRIORITY = { debug: 0, info: 1, warn: 2, error: 3 };

// ── 控制台 ANSI 颜色码 ─────────────────────────────────────
const COLORS = {
  gray:  '\x1b[90m',
  cyan:  '\x1b[36m',
  yellow:'\x1b[33m',
  red:   '\x1b[31m',
  reset: '\x1b[0m',
};

// ── 各级别的 Emoji 前缀 ────────────────────────────────────
const EMOJI = {
  debug: '🔍',
  info:  '💡',
  warn:  '⚠️',
  error: '❌',
};

// ── 各级别对应的控制台颜色 ──────────────────────────────────
const LEVEL_COLOR = {
  debug: COLORS.gray,
  info:  COLORS.cyan,
  warn:  COLORS.yellow,
  error: COLORS.red,
};

/**
 * 获取当前日期字符串 YYYY-MM-DD（本地时区）
 * @returns {string}
 */
function today() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm   = String(d.getMonth() + 1).padStart(2, '0');
  const dd   = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/**
 * 获取 ISO 时间戳（本地时间 + 偏移量格式）
 * @returns {string}
 */
function timestamp() {
  return new Date().toISOString();
}

/**
 * 创建日志记录器
 *
 * @param {object} config - 完整配置对象（需含 config.log）
 * @returns {{ debug: Function, info: Function, warn: Function, error: Function, progress: Function }}
 */
export function createLogger(config) {
  const logDir   = resolve(config.log?.directory || './logs');
  const minLevel = LEVEL_PRIORITY[config.log?.level || 'info'] ?? LEVEL_PRIORITY.info;

  // 确保日志目录存在
  mkdirSync(logDir, { recursive: true });

  /**
   * 内部写入方法：同时输出到控制台和文件
   * @param {'debug'|'info'|'warn'|'error'} level
   * @param {string} message
   */
  function write(level, message) {
    // 级别过滤
    if (LEVEL_PRIORITY[level] < minLevel) return;

    const ts   = timestamp();
    const tag  = level.toUpperCase().padEnd(5);

    // 控制台输出（带颜色和 Emoji）
    const color = LEVEL_COLOR[level];
    const emoji = EMOJI[level];
    console.log(`${color}${emoji} [${ts}] [${tag}] ${message}${COLORS.reset}`);

    // 文件输出（纯文本，无 ANSI 转义）
    const logFile = resolve(logDir, `${today()}.log`);
    const line    = `[${ts}] [${tag}] ${message}\n`;
    try {
      appendFileSync(logFile, line, 'utf-8');
    } catch {
      // 日志写入失败时静默降级，避免递归错误
      console.error(`${COLORS.red}❌ 日志文件写入失败: ${logFile}${COLORS.reset}`);
    }
  }

  return {
    /** 调试信息（灰色） */
    debug: (msg) => write('debug', msg),

    /** 常规信息（青色） */
    info:  (msg) => write('info', msg),

    /** 警告信息（黄色） */
    warn:  (msg) => write('warn', msg),

    /** 错误信息（红色） */
    error: (msg) => write('error', msg),

    /**
     * 进度日志 —— 以 info 级别输出阶段进度
     *
     * 输出格式: [阶段] (current/total) message
     *
     * @param {string} stage   - 当前阶段名称（如 inspect / download）
     * @param {number} current - 当前进度序号
     * @param {number} total   - 总数
     * @param {string} message - 描述文本
     */
    progress: (stage, current, total, message) => {
      write('info', `[${stage}] (${current}/${total}) ${message}`);
    },
  };
}
