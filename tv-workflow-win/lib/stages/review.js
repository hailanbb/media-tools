/**
 * 阶段三：复核清理（含修复闭环核验、穿透刷新、零字节安全门禁与字幕伴生级联清理）
 *
 * 核心流程：
 * 1. 穿透刷新扫描当前黄色项季目录（强制同步网盘最新文件元数据）
 * 2. 【安全熔断】检查是否存在 0 字节损坏或传输中的视频文件，若有坚决熔断，禁止误删已有可用版本
 * 3. 【整理修复核验】若该季存在修复记录，核验修复涉及的集号是否已实际进入网盘
 *    - 若未闭环（修复集号仍缺），严格暂缓清理，不误删其他版本
 *    - 若已闭环，解除阻塞，转为已核验状态
 * 4. 识别版本完整性与重复副本：
 *    - 严格保持原生分集模板正则分组逻辑
 *    - 多级精准仲裁同集重复副本（分辨率 > 标准命名 > 文件体积 > 时间戳），消除相同时间戳跳过死锁
 *    - 存在多个完整版本时，自动优选最高画质版本（如 4K/1080p），淘汰低画质版本（如 720p/SD）
 *    - 混版拼齐完结且无重集时放行标绿，允许流转至归档阶段
 * 5. 伴生文件级联清理：淘汰旧版视频时，同名专属外挂字幕（.srt/.ass/.vtt等）与专属刮削图片同步清理
 * 6. 执行删除后自动触发底层网盘目录页面刷新（refreshDir），并生成删除审计快照日志
 * 7. 完结齐全标绿，连载全集齐全标蓝
 */
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { readReport, writeReport } from '../core/report-adapter.js';
import { episodeFromName, MEDIA_REGEX, SCRAPE_REGEX, SHARED_SCRAPE, range, uniq } from '../core/episode-parser.js';
import { readRepairState, isRepairPending, verifyRepairState } from '../core/repair-state.js';
import { createDataStore } from '../platform/data-store.js';
import { promises as fs } from 'node:fs';
import { join } from 'node:path';

// 外挂字幕扩展名正则
const SUBTITLE_REGEX = /\.(?:srt|ass|ssa|vtt|sub|idx|sup)$/i;

/**
 * 同集重复副本梯级打分（分辨率规格 > 标准命名规整度 > 文件体积 > 修改时间戳）
 */
function rankCopy(f) {
  const fName = f.name || '';
  const size = Number(f.size) || 0;
  const hasDupMarker = /[\s._\-(](?:1|2|copy|副本)[\s._\-)]/i.test(fName) ? 1 : 0;
  const cleanScore = hasDupMarker ? 0 : 1;

  let resScore = 0;
  if (/2160p|4k|uhd/i.test(fName)) {
    resScore = 3;
  } else if (/1080p|1080i|fhd/i.test(fName)) {
    resScore = 2;
  } else if (/720p|hd(?![a-z0-9])/i.test(fName)) {
    resScore = 1;
  }

  const modStr = String(f.modified || '');
  return { resScore, cleanScore, size, modStr };
}

/**
 * 比较两个副本谁更优先
 */
function compareCopies(a, b) {
  const ra = rankCopy(a);
  const rb = rankCopy(b);
  return (rb.resScore - ra.resScore) ||
         (rb.cleanScore - ra.cleanScore) ||
         (rb.size - ra.size) ||
         rb.modStr.localeCompare(ra.modStr);
}

/**
 * 计算完整版本的综合画质得分，用于多完整版本优选
 */
function scoreGroup(g) {
  const files = g.files || [];
  if (!files.length) return 0;
  const totalSize = files.reduce((acc, cur) => acc + (Number(cur.size) || 0), 0);
  const avgSizeMb = (totalSize / files.length) / (1024 * 1024);
  let resScore = 0;
  const k = String(g.key || '').toLowerCase();
  if (/2160p|4k|uhd/i.test(k)) {
    resScore = 3000;
  } else if (/1080p|1080i|fhd/i.test(k)) {
    resScore = 2000;
  } else if (/720p|hd(?![a-z0-9])/i.test(k)) {
    resScore = 500;
  }
  return resScore + avgSizeMb;
}

/**
 * 执行复核清理阶段
 * @param {object} config
 * @param {object} logger
 * @param {string} reportPath
 * @param {{ dryRun?: boolean }} options
 * @returns {Promise<object>}
 */
export async function runReview(config, logger, reportPath, { dryRun = true } = {}) {
  if (!reportPath) throw new Error('请指定报告路径');

  const cd2 = new CloudDriveClient({
    origin: config.clouddrive2.origin,
    token: config.clouddrive2.token,
    logger,
  });
  const store = createDataStore('data');

  logger.info(`📄 读取报告... (模式: ${dryRun ? '预览' : '执行清理'})`);
  const session = readReport(reportPath, {
    origin: config.clouddrive2.origin,
    source: config.paths.source,
    mode: 'review',
  });
  logger.info(`📄 待复核: ${session.tasks.length} 项黄色`);
  session.updates = new Map();

  const summary = {
    total: session.tasks.length,
    cleaned: 0,
    skipped: 0,
    repairPending: 0,
    errors: 0,
    deletedFiles: 0
  };

  const nowIso = new Date().toISOString();
  const allAuditDeleted = [];

  for (let i = 0; i < session.tasks.length; i++) {
    const task = session.tasks[i];
    logger.progress('复核', i + 1, session.tasks.length, `${task.name} Season ${task.season}`);

    try {
      // 1. 穿透刷新拉取云盘最新文件列表
      let files;
      try {
        files = await cd2.listFiles(task.path, { forceRefresh: true });
      } catch (e) {
        session.updates.set(task, { state: '目录读取失败', color: null, note: e.message, fileCheckedAt: nowIso });
        summary.errors++;
        continue;
      }

      if (files.some(f => f.directory)) {
        session.updates.set(task, { state: '存在子目录', color: 'yellow', note: '季内存在子目录，需人工确认', fileCheckedAt: nowIso });
        summary.skipped++;
        continue;
      }
      if (files.some(f => /\.(?:part|tmp|download|crdownload|!qb)$/i.test(f.name))) {
        session.updates.set(task, { state: '下载中', color: 'yellow', note: '发现临时下载文件，等待完成', fileCheckedAt: nowIso });
        summary.skipped++;
        continue;
      }

      const videos = files.filter(f => MEDIA_REGEX.test(f.name));

      // 2. 0 字节破损或正在写入文件安全熔断门禁（绝对防止误删现有好文件）
      const zeroByteVideos = videos.filter(f => (Number(f.size) || 0) <= 0);
      if (zeroByteVideos.length > 0) {
        const zeroNames = zeroByteVideos.slice(0, 2).map(f => f.name).join('、');
        logger.warn(`  ⚠️ 发现 ${zeroByteVideos.length} 个 0 字节视频文件 (${zeroNames})，触发防误删熔断保护，暂缓清理`);
        session.updates.set(task, {
          state: '存在0字节破损/传输中文件',
          color: 'yellow',
          note: `发现 ${zeroByteVideos.length} 个 0 字节视频 (${zeroNames})，暂缓清理以防误删`,
          fileCheckedAt: nowIso,
        });
        summary.skipped++;
        continue;
      }

      const total = Number(task.data['来源登记集数']) || videos.length;

      // 3. 解析视频集号与版本模板（严格保持原生简单分集正则逻辑）
      const parsed = videos.map(f => {
        const ep = episodeFromName(f.name, task.season);
        if (!ep) return null;
        let template = f.name.replace(/\.[^.]+$/, '');
        const seRe = new RegExp(`S0*${task.season}\\s*E\\d+`, 'ig');
        template = template.replace(seRe, `S${task.season}E{集}`);
        if (template === f.name.replace(/\.[^.]+$/, '')) {
          template = template.replace(/第\s*\d+\s*集/g, '第{集}集');
        }
        const ext = f.name.match(/\.[^.]+$/)?.[0] || '';
        const key = template.normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase() + ext;
        return { episode: ep, key, name: f.name, path: f.path, modified: f.modified, size: f.size };
      });

      const validParsed = parsed.filter(Boolean);
      const foundEpisodes = uniq(validParsed.map(p => p.episode));

      // 4. 【整理修复闭环核验】
      let currentRepair = readRepairState(task.data);
      if (currentRepair) {
        currentRepair = verifyRepairState(currentRepair, foundEpisodes, nowIso);
        if (isRepairPending(currentRepair)) {
          logger.warn(`  ⚠️ 整理修复尚未闭环（部分修复集号尚未在网盘呈现），暂缓清理`);
          session.updates.set(task, {
            state: '修复未闭环，暂缓清理',
            color: 'yellow',
            repair: currentRepair,
            note: '已触发 MoviePilot 修复，但对应集号文件尚未在网盘同步到位，避免误删旧版本',
            fileCheckedAt: nowIso,
          });
          summary.repairPending++;
          continue;
        }
      }

      // 检查未知集号视频
      const unknownVideos = videos.filter((_, idx) => !parsed[idx]).map(f => f.name);
      if (unknownVideos.length) {
        session.updates.set(task, {
          state: '待核实', color: 'yellow',
          repair: currentRepair || undefined,
          note: `${unknownVideos.length} 个视频无法识别集号`,
          fileCheckedAt: nowIso
        });
        summary.skipped++;
        continue;
      }

      // 按原生版本模板分组
      const groups = new Map();
      for (const p of validParsed) {
        if (!groups.has(p.key)) groups.set(p.key, { key: p.key, files: [], episodes: [] });
        const g = groups.get(p.key);
        g.files.push(p);
        g.episodes = uniq([...g.episodes, p.episode]);
      }

      for (const g of groups.values()) {
        g.missing = range(total).filter(n => !g.episodes.includes(n));
        g.complete = g.missing.length === 0;
      }

      const completeGroups = [...groups.values()].filter(g => g.complete);
      const toKeep = [];
      const toDelete = [];
      let versionNote = '';

      // 5. 完整版本判定与多完整版本画质优选淘汰
      if (completeGroups.length > 1) {
        completeGroups.sort((a, b) => scoreGroup(b) - scoreGroup(a));
        const best = completeGroups[0];
        versionNote = `存在 ${completeGroups.length} 个完整版本，优选保留最佳画质 [${best.key}]，淘汰劣质版本`;
        logger.info(`  💡 ${versionNote}`);

        for (const ep of range(total)) {
          const copies = best.files.filter(f => f.episode === ep).sort(compareCopies);
          toKeep.push(copies[0]);
          for (const dup of copies.slice(1)) {
            toDelete.push({ ...dup, reason: '同版本较旧或冗余副本' });
          }
        }

        for (let idx = 1; idx < completeGroups.length; idx++) {
          const inferiorG = completeGroups[idx];
          for (const f of inferiorG.files) {
            toDelete.push({ ...f, reason: `淘汰低画质完整版本 (${inferiorG.key})` });
          }
        }

        for (const [k, g] of groups) {
          if (completeGroups.includes(g)) continue;
          for (const f of g.files) {
            toDelete.push({ ...f, reason: '其他不完整版本' });
          }
        }

      } else if (completeGroups.length === 1) {
        const best = completeGroups[0];
        versionNote = `保留版本: ${best.key}`;

        for (const ep of range(total)) {
          const copies = best.files.filter(f => f.episode === ep).sort(compareCopies);
          toKeep.push(copies[0]);
          for (const dup of copies.slice(1)) {
            toDelete.push({ ...dup, reason: '同版本较旧或冗余副本' });
          }
        }

        for (const [key, g] of groups) {
          if (key === best.key) continue;
          for (const f of g.files) {
            toDelete.push({ ...f, reason: '其他不完整版本' });
          }
        }

      } else {
        // 无单一完整版本：检查所有集号是否已全齐（混版拼齐）
        const allMissing = range(total).filter(n => !foundEpisodes.includes(n));
        if (allMissing.length > 0) {
          session.updates.set(task, {
            state: '缺集',
            color: 'yellow',
            repair: currentRepair || undefined,
            note: `缺 ${allMissing.join('、')} 集`,
            fileCheckedAt: nowIso
          });
          summary.skipped++;
          continue;
        } else {
          // 混版齐全（1..total 各集均有）：梯级仲裁优选各集最佳副本
          versionNote = `各集版本不同但已拼齐 (共 ${total} 集)`;
          logger.info(`  💡 ${versionNote}，按画质梯队自动优选保留单集最佳副本`);

          for (const ep of range(total)) {
            const copies = validParsed.filter(p => p.episode === ep).sort(compareCopies);
            toKeep.push(copies[0]);
            for (const dup of copies.slice(1)) {
              toDelete.push({ ...dup, reason: '混版中较低质量或冗余副本' });
            }
          }
        }
      }

      // 6. 外挂字幕与刮削文件级联判定（彻底清理孤立幽灵字幕）
      const deletedPaths = new Set(toDelete.map(f => f.path));
      const keptPaths = new Set(toKeep.map(f => f.path));

      for (const f of files) {
        const fName = f.name || '';
        const isSub = SUBTITLE_REGEX.test(fName);
        const isScrape = SCRAPE_REGEX.test(fName);
        if (!isSub && !isScrape) continue;

        const stem = fName.replace(/\.[^.]+$/, '');
        if (isScrape && SHARED_SCRAPE.test(stem)) continue;

        const cleanStem = stem.replace(/\.(?:zh|chs|cht|chi|eng|sc|tc|jpn|default|forced)$/i, '');

        const matchedDeleted = toDelete.filter(v => {
          const vs = (v.name || '').replace(/\.[^.]+$/, '').toLowerCase();
          const sl = stem.toLowerCase();
          const csl = cleanStem.toLowerCase();
          return sl === vs || sl === vs + '-thumb' || csl === vs;
        });

        const matchedKept = toKeep.filter(v => {
          const vs = (v.name || '').replace(/\.[^.]+$/, '').toLowerCase();
          const sl = stem.toLowerCase();
          const csl = cleanStem.toLowerCase();
          return sl === vs || csl === vs;
        });

        if (matchedDeleted.length > 0 && matchedKept.length === 0) {
          toDelete.push({
            name: f.name,
            path: f.path,
            modified: f.modified,
            size: f.size,
            reason: isSub ? '对应已删视频的外挂字幕' : '唯一对应已删视频的刮削文件',
          });
        }
      }

      const broadcastEnded = task.data['播出状态'] === '已播完';

      // 7. 无需删除文件时的状态流转（完结齐全标绿，连载当前集齐标蓝）
      if (toDelete.length === 0) {
        session.updates.set(task, {
          state: broadcastEnded ? '齐全' : '连载中，当前集齐全',
          color: broadcastEnded ? 'green' : 'blue',
          repair: currentRepair || undefined,
          note: versionNote,
          fileCheckedAt: nowIso,
        });
        continue;
      }

      logger.info(`  📋 保留 ${toKeep.length} 个文件, 拟删除 ${toDelete.length} 个`);

      if (dryRun) {
        session.updates.set(task, {
          state: '待确认清理（预览）',
          color: 'yellow',
          repair: currentRepair || undefined,
          note: `拟删除 ${toDelete.length} 个文件 (${versionNote})`,
          remove: toDelete.map(f => f.path),
          keep: toKeep.map(f => f.path),
          fileCheckedAt: nowIso,
        });
        continue;
      }

      // 8. 执行实际物理删除
      let deletedCount = 0;
      for (const f of toDelete) {
        try {
          await cd2.deleteFile(f.path);
          deletedCount++;
          logger.debug(`    ✅ 已删除: ${f.name}`);
          allAuditDeleted.push({
            task: `${task.name} Season ${task.season}`,
            deleted_file: f.path,
            name: f.name,
            size: f.size || 0,
            modified: f.modified || '',
            reason: f.reason || '',
            deleted_at: new Date().toISOString(),
          });
        } catch (e) {
          logger.error(`    ❌ 删除失败: ${f.name} - ${e.message}`);
        }
      }
      summary.deletedFiles += deletedCount;
      summary.cleaned++;

      // 规约：删除后立即刷新季目录缓存
      if (deletedCount > 0) {
        try {
          await cd2.refreshDir(task.path);
          logger.debug(`    🔄 已刷新季目录: ${task.path}`);
        } catch (e) {
          logger.warn(`    ⚠️ 刷新季目录异常: ${e.message}`);
        }
      }

      const isAllClean = (deletedCount === toDelete.length);
      let finalState = '已清理';
      let finalColor = 'yellow';
      if (isAllClean && broadcastEnded) {
        finalState = '齐全';
        finalColor = 'green';
      } else if (isAllClean && !broadcastEnded) {
        finalState = '连载中，当前集齐全';
        finalColor = 'blue';
      }

      session.updates.set(task, {
        state: finalState,
        color: finalColor,
        repair: currentRepair || undefined,
        note: `已删除 ${deletedCount}/${toDelete.length} 个文件 (${versionNote})`,
        deleted: toDelete.filter((_, idx) => idx < deletedCount).map(f => f.path),
        keep: toKeep.map(f => f.path),
        fileCheckedAt: nowIso,
      });

    } catch (e) {
      session.updates.set(task, { state: '处理失败', color: null, note: e.message, fileCheckedAt: nowIso });
      summary.errors++;
    }
  }

  // 9. 物理删除审计快照存盘
  if (!dryRun && allAuditDeleted.length > 0) {
    try {
      const logDir = 'logs';
      await fs.mkdir(logDir, { recursive: true });
      const nowStr = new Date().toISOString().replace(/[:.]/g, '-');
      const auditFile = join(logDir, `review_deleted_${nowStr}.json`);
      await fs.writeFile(auditFile, JSON.stringify(allAuditDeleted, null, 2), 'utf-8');
      logger.info(`   📋 物理删除审计快照已生成: ${auditFile}`);
    } catch (aErr) {
      logger.warn(`   ⚠️ 写入删除审计日志跳过: ${aErr.message}`);
    }
  }

  const suffix = dryRun ? '_复核预览' : '_复核清理';
  const outputPath = reportPath.replace(/(?:_补缺下载|_复核预览|_复核清理|_归档预览|_归档完成)*\.xlsx$/i, `${suffix}.xlsx`);
  await writeReport(session, outputPath);

  logger.info(`\n📊 复核${dryRun ? '预览' : '清理'}完成:`);
  logger.info(`   ✅ 已清理 ${summary.cleaned}  ⏳ 修复待闭环 ${summary.repairPending}  ⏭ 跳过 ${summary.skipped}  🗑 删除文件 ${summary.deletedFiles}  ❌ 错误 ${summary.errors}`);
  logger.info(`   📄 报告: ${outputPath}`);

  return { ...summary, outputPath };
}
