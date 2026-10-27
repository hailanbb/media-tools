/**
 * 配置加载与校验模块
 *
 * 从项目根目录读取 config.json，校验必填字段，
 * 并以冻结对象返回配置。
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// 项目根目录（lib/ 的上级）
const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, '..');

/**
 * 必填字段定义
 * 每项包含 路径（点分隔）和 缺失时的中文提示
 */
const REQUIRED_FIELDS = [
  { path: 'clouddrive2.origin', label: 'CloudDrive2 服务地址 (clouddrive2.origin)' },
  { path: 'clouddrive2.token',  label: 'CloudDrive2 API 令牌 (clouddrive2.token)' },
  { path: 'moviepilot.origin',  label: 'MoviePilot 服务地址 (moviepilot.origin)' },
  { path: 'moviepilot.token',   label: 'MoviePilot API 令牌 (moviepilot.token)' },
];

/**
 * 按点分路径从对象中取值
 * @param {object} obj  - 目标对象
 * @param {string} path - 点分隔路径，如 "a.b.c"
 * @returns {*} 对应值或 undefined
 */
function getNestedValue(obj, path) {
  return path.split('.').reduce((current, key) => current?.[key], obj);
}

/**
 * 校验必填字段，返回所有缺失项的描述列表
 * @param {object} config - 待校验的配置对象
 * @returns {string[]} 缺失字段的中文描述
 */
function validateRequired(config) {
  const missing = [];
  for (const { path, label } of REQUIRED_FIELDS) {
    const value = getNestedValue(config, path);
    if (value === undefined || value === null || value === '') {
      missing.push(label);
    }
  }
  return missing;
}

/**
 * 加载并校验配置文件
 *
 * @param {string} [configPath] - 配置文件的绝对或相对路径，
 *                                 默认为项目根目录下的 config.json
 * @returns {object} 冻结后的配置对象
 * @throws {Error} 文件不存在、格式错误或必填字段缺失时抛出中文错误
 */
export function loadConfig(configPath) {
  const filePath = configPath
    ? resolve(configPath)
    : resolve(PROJECT_ROOT, 'config.json');

  // 1. 读取文件
  let raw;
  try {
    raw = readFileSync(filePath, 'utf-8');
  } catch (err) {
    if (err.code === 'ENOENT') {
      throw new Error(
        `❌ 配置文件不存在: ${filePath}\n` +
        `   请复制 config.example.json 为 config.json 并填写实际参数。`
      );
    }
    throw new Error(`❌ 读取配置文件失败: ${err.message}`);
  }

  // 2. 解析 JSON
  let config;
  try {
    config = JSON.parse(raw);
  } catch {
    throw new Error(
      `❌ 配置文件 JSON 格式错误: ${filePath}\n` +
      `   请检查 JSON 语法（多余逗号、缺少引号等）。`
    );
  }

  // 3. 校验必填字段
  const missing = validateRequired(config);
  if (missing.length > 0) {
    throw new Error(
      `❌ 配置文件缺少以下必填项:\n` +
      missing.map((m) => `   • ${m}`).join('\n') +
      `\n   请在 ${filePath} 中补全上述字段。`
    );
  }

  // 4. 填充默认值
  config.tmdb = { language: 'zh-CN', ...config.tmdb };
  config.paths = {
    source: '/115/Library_incs/nas',
    destination: '/115/Library_incs/nas.finish',
    ...config.paths,
  };
  config.schedule = {
    enabled: false,
    cron: '0 2 * * *',
    stages: ['inspect', 'download', 'review', 'archive'],
    ...config.schedule,
  };
  config.notifications = {
    enabled: true,
    sound: true,
    ...config.notifications,
  };
  config.log = {
    directory: './logs',
    level: 'info',
    ...config.log,
  };

  return Object.freeze(config);
}

export { PROJECT_ROOT };
