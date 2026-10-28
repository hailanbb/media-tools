/**
 * 阶段调度入口
 * 统一分发各阶段函数，并提供全自动流程编排
 */
import { runInspect } from './inspect.js';
import { runDownload } from './download.js';
import { runReview } from './review.js';
import { runArchive, postArchiveReview } from './archive.js';
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { readReport, writeReport } from '../core/report-adapter.js';
import { readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { PROJECT_ROOT } from '../config.js';

/**
 * 按名称执行指定阶段
 * @param {string} stageName - inspect | download | review | archive | auto
 * @param {object} config
 * @param {object} logger
 * @param {object} options - { reportPath?, dryRun?, yes? }
 * @returns {Promise<object>}
 */
export async function runStage(stageName, config, logger, options = {}) {
  const startTime = Date.now();
  logger.info(`\n${'═'.repeat(50)}`);
  logger.info(`🎬 开始执行: ${stageLabel(stageName)}`);
  logger.info(`${'═'.repeat(50)}\n`);

  let result;
  try {
    switch (stageName) {
      case 'inspect':
        result = await runInspect(config, logger, options);
        break;
      case 'download': {
        const rep = options.reportPath || findLatestReport('初检') || findLatestReport('');
        if (rep) logger.info(`📄 依据最新报表执行: ${rep}`);
        result = await runDownload(config, logger, rep);
        break;
      }
      case 'repair': {
        const rep = options.reportPath || findLatestReport('初检') || findLatestReport('');
        if (rep) logger.info(`📄 依据最新报表执行: ${rep}`);
        result = await runDownload(config, logger, rep, { repairOnly: true });
        break;
      }
      case 'review': {
        const rep = options.reportPath || findLatestReport('补缺') || findLatestReport('初检') || findLatestReport('');
        if (rep) logger.info(`📄 依据最新报表执行: ${rep}`);
        result = await runReview(config, logger, rep, { dryRun: !options.yes });
        break;
      }
      case 'archive': {
        const rep = options.reportPath || findLatestReport('复核清理') || findLatestReport('初检') || findLatestReport('');
        if (rep) logger.info(`📄 依据最新报表执行: ${rep}`);
        result = await runArchive(config, logger, rep, { dryRun: !options.yes });
        break;
      }
      case 'verify':
      case 'audit':
        result = await runVerify(config, logger, options);
        break;
      case 'auto':
        result = await runAuto(config, logger);
        break;
      default:
        throw new Error(`未知阶段: ${stageName}`);
    }
  } catch (e) {
    logger.error(`\n❌ ${stageLabel(stageName)} 执行失败: ${e.message}`);
    throw e;
  }

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  logger.info(`\n⏱ 耗时 ${elapsed} 秒`);

  return result;
}

/**
 * 全自动流程：依次执行 初检 → 补缺下载 → 复核清理 → 归档
 * 每个阶段的报告自动传递给下一阶段
 */
export async function runAuto(config, logger) {
  logger.info('🤖 全自动流程启动\n');
  const results = {};

  // ① 初检
  try {
    results.inspect = await runInspect(config, logger);
  } catch (e) {
    logger.error(`初检失败，终止后续流程: ${e.message}`);
    return results;
  }

  // ② 补缺下载
  if (results.inspect.outputPath) {
    try {
      results.download = await runDownload(config, logger, results.inspect.outputPath);
    } catch (e) {
      logger.error(`补缺下载失败: ${e.message}`);
    }
  }

  // ③ 复核清理（自动模式下执行实际清理）
  const reviewInput = results.download?.outputPath || results.inspect?.outputPath;
  if (reviewInput) {
    try {
      results.review = await runReview(config, logger, reviewInput, { dryRun: false });
    } catch (e) {
      logger.error(`复核清理失败: ${e.message}`);
    }
  }

  // ④ 归档（自动模式下执行实际归档）
  const archiveInput = results.review?.outputPath || reviewInput;
  if (archiveInput) {
    try {
      results.archive = await runArchive(config, logger, archiveInput, { dryRun: false });
    } catch (e) {
      logger.error(`归档失败: ${e.message}`);
    }
  }

  logger.info('\n🤖 全自动流程完成');
  return results;
}

/**
 * 独立执行归档后对账复核阶段
 * @param {object} config
 * @param {object} logger
 * @param {object} options - { reportPath? }
 * @returns {Promise<object>}
 */
export async function runVerify(config, logger, options = {}) {
  const reportPath = options.reportPath || findLatestReport('归档完成') || findLatestReport('归档') || findLatestReport('');
  if (!reportPath) throw new Error('未找到可用的归档报告，请通过 --report 指定报告路径');

  const cd2 = new CloudDriveClient({
    origin: config.clouddrive2.origin,
    token: config.clouddrive2.token,
    logger,
  });

  logger.info(`📄 读取报告进行复核: ${reportPath}`);
  const session = readReport(reportPath, {
    origin: config.clouddrive2.origin,
    source: config.paths.source,
    mode: 'archive',
  });
  session.updates = new Map();

  const reviewSummary = await postArchiveReview({ config, logger, cd2, session }, { autoCleanEmptyAliases: true });

  // 回写复核结果至报表
  const outputPath = reportPath.replace(/(?:_复核核验)*\.xlsx$/i, '_复核核验.xlsx');
  await writeReport(session, outputPath);
  logger.info(`📄 复核核验报告已更新: ${outputPath}`);

  return { ...reviewSummary, outputPath };
}

/** 阶段名称映射 */
function stageLabel(name) {
  return {
    inspect: '① 剧集初检',
    download: '② 补缺下载（含自动整理修复）',
    repair: '🔧 MoviePilot 整理历史修复',
    review: '③ 复核清理',
    archive: '④ 归档（含归档后对账复核）',
    verify: '🔍 归档后对账复核',
    audit: '🔍 归档后对账复核',
    auto: '🤖 全自动流程'
  }[name] || name;
}

/**
 * 从报表文件名精准提取时间戳（优先从 ISO 时间解析，次选文件修改时间）
 * @param {string} filePath
 * @returns {number}
 */
function extractReportTime(filePath) {
  const m = filePath.match(/(\d{4}-\d{2}-\d{2}T\d{2}[-:]\d{2}[-:]\d{2})/);
  if (m) {
    const tStr = m[1].replace(/-/g, ':');
    const norm = tStr.replace(
      /^(\d{4}):(\d{2}):(\d{2})T(\d{2}):(\d{2}):(\d{2})$/,
      '$1-$2-$3T$4:$5:$6'
    );
    const ts = Date.parse(norm);
    if (!Number.isNaN(ts)) return ts;
  }
  try {
    return statSync(filePath).mtimeMs;
  } catch {
    return 0;
  }
}

/**
 * 查找 reports/ 或项目根目录下最新的匹配报告（基于真实时间戳倒序）
 * @param {string} [stageKey=''] - 阶段类型: '初检' | '补缺' | '复核' | '归档'
 * @param {string} [keyword=''] - 可选剧名关键词
 * @returns {string|undefined}
 */
function findLatestReport(stageKey = '', keyword = '') {
  const dirs = [resolve(PROJECT_ROOT, 'reports'), PROJECT_ROOT];
  const allFiles = [];

  for (const dir of dirs) {
    try {
      const files = readdirSync(dir)
        .filter(f => f.endsWith('.xlsx') && !f.startsWith('~$'))
        .map(f => join(dir, f));
      allFiles.push(...files);
    } catch {
      // 忽略无法读取的目录
    }
  }

  // 严格按真实时间戳倒序排序（最新的在最前）
  allFiles.sort((a, b) => extractReportTime(b) - extractReportTime(a));

  if (!allFiles.length) return undefined;

  function filterByStage(files, stage) {
    if (!stage) return files[0];
    let filtered = [];
    if (stage === '初检') {
      filtered = files.filter(f => f.includes('初检报告') && !/(?:补缺|复核|归档)/.test(f));
    } else if (stage === '补缺') {
      filtered = files.filter(f => f.includes('补缺'));
    } else if (stage === '复核') {
      filtered = files.filter(f => f.includes('复核'));
    } else if (stage === '归档' || stage === '归档完成') {
      filtered = files.filter(f => f.includes('归档'));
    } else {
      filtered = files.filter(f => f.includes(stage));
    }
    return filtered[0];
  }

  // 1. 若提供了剧名关键字，优先在包含关键字的报表中按时间倒序匹配
  if (keyword) {
    const kwFiles = allFiles.filter(f => f.includes(keyword));
    const matched = filterByStage(kwFiles, stageKey);
    if (matched) return matched;
  }

  // 2. 在全局报表中按时间倒序匹配
  const matched = filterByStage(allFiles, stageKey);
  if (matched) return matched;

  // 3. 回退：若没有上一阶段报告，取未归档的最新报告
  const fallback = allFiles.find(f => !/(?:归档)/.test(f)) || allFiles[0];
  return fallback;
}
