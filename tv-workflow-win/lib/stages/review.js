/**
 * 阶段三：复核清理（含修复闭环核验 v1.4.0）
 *
 * 核心流程：
 * 1. 扫描当前黄色项季目录
 * 2. 【整理修复核验】若该季存在修复记录，核验修复涉及的集号是否已实际进入网盘
 *    - 若未闭环（修复集号仍缺），严格暂缓清理，不误删其他版本
 *    - 若已闭环，解除阻塞，转为已核验状态
 * 3. 识别单版本完整性，保留唯一完整版本，清理冗余旧版本与专属刮削文件
 * 4. 播完且齐全后标绿
 */
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { readReport, writeReport } from '../core/report-adapter.js';
import { episodeFromName, MEDIA_REGEX, SCRAPE_REGEX, SHARED_SCRAPE, range, uniq } from '../core/episode-parser.js';
import { readRepairState, isRepairPending, verifyRepairState } from '../core/repair-state.js';
import { createDataStore } from '../platform/data-store.js';

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

  for (let i = 0; i < session.tasks.length; i++) {
    const task = session.tasks[i];
    logger.progress('复核', i + 1, session.tasks.length, `${task.name} Season ${task.season}`);

    try {
      let files;
      try {
        files = await cd2.listFiles(task.path);
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
      const total = Number(task.data['来源登记集数']) || videos.length;

      // 解析视频集号与版本模板
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

      const foundEpisodes = uniq(parsed.filter(Boolean).map(p => p.episode));

      // ──【整理修复闭环核验】──
      let currentRepair = readRepairState(task.data);
      if (currentRepair) {
        currentRepair = verifyRepairState(currentRepair, foundEpisodes, nowIso);
        // 如果修复仍未闭环（修复涉及的集号并未全部在网盘出现）
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

      // 按版本分组
      const groups = new Map();
      for (const p of parsed.filter(Boolean)) {
        if (!groups.has(p.key)) groups.set(p.key, { key: p.key, files: [], episodes: [] });
        const g = groups.get(p.key);
        g.files.push(p);
        g.episodes = uniq([...g.episodes, p.episode]);
      }

      for (const g of groups.values()) {
        g.missing = range(total).filter(n => !g.episodes.includes(n));
        g.complete = g.missing.length === 0;
      }

      const complete = [...groups.values()].filter(g => g.complete);

      if (complete.length > 1) {
        session.updates.set(task, {
          state: '多个完整版本，人工判断', color: 'yellow',
          repair: currentRepair || undefined,
          note: `${complete.length} 个版本均完整，不自动淘汰`,
          fileCheckedAt: nowIso
        });
        summary.skipped++;
        continue;
      }

      if (complete.length === 0) {
        const allMissing = range(total).filter(n => !foundEpisodes.includes(n));
        session.updates.set(task, {
          state: allMissing.length ? '缺集' : '混版拼齐，人工判断',
          color: 'yellow',
          repair: currentRepair || undefined,
          note: allMissing.length ? `缺 ${allMissing.join('、')} 集` : '无单独完整版本',
          fileCheckedAt: nowIso
        });
        summary.skipped++;
        continue;
      }

      // 唯一完整版本
      const best = complete[0];
      const toKeep = [];
      const toDelete = [];

      for (const ep of range(total)) {
        const copies = best.files.filter(f => f.episode === ep).sort((a, b) =>
          (b.modified || '').localeCompare(a.modified || '')
        );
        if (copies.length > 1 && copies[0].modified === copies[1].modified) {
          session.updates.set(task, {
            state: '重复副本时间相同', color: 'yellow',
            repair: currentRepair || undefined,
            note: `第 ${ep} 集无法判断保留哪份`,
            fileCheckedAt: nowIso
          });
          summary.skipped++;
          continue;
        }
        toKeep.push(copies[0]);
        for (const dup of copies.slice(1)) {
          toDelete.push({ ...dup, reason: '同版本较旧重复副本' });
        }
      }

      for (const [key, g] of groups) {
        if (key === best.key) continue;
        for (const f of g.files) {
          toDelete.push({ ...f, reason: '其他不完整版本' });
        }
      }

      // 刮削文件级联删除判定
      const deletedPaths = new Set(toDelete.map(f => f.path));
      for (const f of files.filter(f => SCRAPE_REGEX.test(f.name))) {
        const stem = f.name.replace(/\.[^.]+$/, '');
        if (SHARED_SCRAPE.test(stem)) continue;
        const matches = videos.filter(v => {
          const vs = v.name.replace(/\.[^.]+$/, '').toLowerCase();
          return stem.toLowerCase() === vs || stem.toLowerCase() === vs + '-thumb';
        });
        if (matches.length === 1 && deletedPaths.has(matches[0].path)) {
          toDelete.push({ name: f.name, path: f.path, modified: f.modified, size: f.size, reason: '唯一对应已删视频的刮削文件' });
        }
      }

      if (toDelete.length === 0) {
        const broadcastEnded = task.data['播出状态'] === '已播完';
        session.updates.set(task, {
          state: broadcastEnded ? '齐全' : '集号齐全，播出状态待确认',
          color: broadcastEnded ? 'green' : 'yellow',
          repair: currentRepair || undefined,
          note: `保留版本: ${best.key}`,
          fileCheckedAt: nowIso,
        });
        continue;
      }

      logger.info(`  📋 保留 ${toKeep.length} 个文件, 拟删除 ${toDelete.length} 个`);

      if (dryRun) {
        session.updates.set(task, {
          state: '待确认清理（预览）', color: 'yellow',
          repair: currentRepair || undefined,
          note: `拟删除 ${toDelete.length} 个文件`,
          remove: toDelete.map(f => f.path),
          keep: toKeep.map(f => f.path),
          fileCheckedAt: nowIso,
        });
        continue;
      }

      // 执行实际删除
      let deletedCount = 0;
      for (const f of toDelete) {
        try {
          await cd2.deleteFile(f.path);
          deletedCount++;
          logger.debug(`    ✅ 已删除: ${f.name}`);
        } catch (e) {
          logger.error(`    ❌ 删除失败: ${f.name} - ${e.message}`);
        }
      }
      summary.deletedFiles += deletedCount;
      summary.cleaned++;

      // 删除后执行当前操作路径页面刷新（举一反三规约）
      if (deletedCount > 0) {
        try {
          await cd2.refreshDir(task.path);
          logger.debug(`    🔄 已刷新季目录: ${task.path}`);
        } catch (e) {
          logger.warn(`    ⚠️ 刷新季目录异常: ${e.message}`);
        }
      }

      const broadcastEnded = task.data['播出状态'] === '已播完';
      session.updates.set(task, {
        state: broadcastEnded && deletedCount === toDelete.length ? '齐全' : '已清理',
        color: broadcastEnded && deletedCount === toDelete.length ? 'green' : 'yellow',
        repair: currentRepair || undefined,
        note: `已删除 ${deletedCount}/${toDelete.length} 个文件`,
        deleted: toDelete.filter((_, idx) => idx < deletedCount).map(f => f.path),
        keep: toKeep.map(f => f.path),
        fileCheckedAt: nowIso,
      });

    } catch (e) {
      session.updates.set(task, { state: '处理失败', color: null, note: e.message, fileCheckedAt: nowIso });
      summary.errors++;
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
