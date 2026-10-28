/**
 * 阶段四：剧集归档与归档后对账复核（含穿透刷新、多格式别名识别、集数守恒核验与解耦增量归档）
 *
 * 核心流程：
 * 1. 检查各剧集待归档季的完结齐全状态（支持整剧归档与已有剧集增量季安全解耦归档）
 * 2. 目标分类多级递归探测与创建，关键决策点采用穿透刷新（forceRefresh: true）消除网盘缓存时滞
 * 3. 迁移处理：
 *    - 增量季合入模式：将源端完结季目录安全迁入已有目标剧集目录，并在源端仍有留存季时安全保留源根目录
 *    - 全新整剧移动模式：整剧迁移至目标分类，并穿透刷新两端目录树
 * 4. 归档后对账复核 (Post-Archive Audit)：
 *    - 目标目录精准集数守恒核验（实到视频数与原报告登记集数精确比对，杜绝漏移）
 *    - 源目录存量物理对账与多格式 TMDB/标题别名残留排查
 *    - 原样保留空别名安全清理
 * 5. 输出归档审计快照日志与双格式报表（.xlsx + .csv）
 */
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { readReport, writeReport } from '../core/report-adapter.js';
import { seasonNumber as parseSeasonNumber } from '../core/title-matcher.js';
import { episodeFromName, MEDIA_REGEX } from '../core/episode-parser.js';
import { readRepairState, isRepairPending } from '../core/repair-state.js';
import { promises as fs } from 'node:fs';
import { join } from 'node:path';

/**
 * 多格式提取 TMDB ID：兼容 [tmdbid=123], {tmdb-123}, [tmdb-123], tmdb:123 等主流格式
 */
export function extractTmdbId(text) {
  if (!text) return null;
  const s = String(text);
  const m1 = /(?:\[|\{)tmdb(?:id)?[-_:=]?(\d+)(?:\]|\})/i.exec(s);
  if (m1) {
    const id = parseInt(m1[1], 10);
    if (!Number.isNaN(id)) return id;
  }
  const m2 = /(?:^|[^a-z0-9])tmdb(?:id)?[-_:=](\d+)(?![a-z0-9])/i.exec(s);
  if (m2) {
    const id = parseInt(m2[1], 10);
    if (!Number.isNaN(id)) return id;
  }
  return null;
}

/**
 * 提取干净剧集标题用于别名归一化比对
 */
export function extractCleanTitle(text) {
  let t = String(text || '');
  t = t.replace(/(?:\[|\{)tmdb(?:id)?[-_:=]?\d+(?:\]|\})/gi, '');
  t = t.replace(/[（(](?:19|20)\d{2}[)）]/g, '');
  return t.replace(/[\s._\-]+/g, '').toLowerCase().trim();
}

/**
 * 递归确保目录及其所有父目录存在，并刷新目录树
 */
export async function ensureDirRecursive(cd2, dirPath) {
  const normPath = dirPath.replace(/\/+$/, '') || '/';
  if (normPath === '/') return true;
  try {
    if (await cd2.exists(normPath)) return true;
  } catch {}

  const parts = normPath.replace(/^\/+/, '').split('/');
  let cur = '';
  for (const p of parts) {
    cur += '/' + p;
    try {
      if (!(await cd2.exists(cur))) {
        await cd2.mkdir(cur);
        await cd2.refreshDir(cur);
      }
    } catch {}
  }
  return cd2.exists(normPath);
}

/**
 * 执行归档阶段
 * @param {object} config
 * @param {object} logger
 * @param {string} reportPath
 * @param {{ dryRun?: boolean, allowSingleSeasonArchive?: boolean }} options
 * @returns {Promise<object>}
 */
export async function runArchive(config, logger, reportPath, options = {}) {
  if (!reportPath) throw new Error('请指定报告路径');

  const dryRun = options.dryRun !== undefined ? options.dryRun : !options.yes;
  const allowSingleSeason = Boolean(options.allowSingleSeasonArchive || config.paths?.allowSingleSeasonArchive);

  const cd2 = new CloudDriveClient({
    origin: config.clouddrive2.origin,
    token: config.clouddrive2.token,
    logger,
  });
  const source = (config.paths.source || '').replace(/\/+$/, '');
  const dest = (config.paths.destination || '').replace(/\/+$/, '');

  logger.info(`📄 读取报告... (模式: ${dryRun ? '预览' : '执行归档'})`);
  const session = readReport(reportPath, {
    origin: config.clouddrive2.origin,
    source,
    mode: 'archive',
  });
  session.updates = new Map();

  // 按剧集分组
  const showGroups = new Map();
  for (const task of session.tasks) {
    const key = task.showPath || `${task.category}/${task.name}`;
    if (!showGroups.has(key)) showGroups.set(key, []);
    showGroups.get(key).push(task);
  }

  const summary = { total: showGroups.size, archived: 0, skipped: 0, conflicts: 0, errors: 0 };
  let showIndex = 0;

  for (const [showPath, rows] of showGroups) {
    showIndex++;
    const showName = rows[0].name;
    const catName = rows[0].category;
    logger.progress('归档', showIndex, showGroups.size, `${catName} / ${showName}`);

    try {
      // 1. 检查是否存在未闭环的修复记录
      const hasUnfinishedRepair = rows.some(r => isRepairPending(readRepairState(r.data)));
      if (hasUnfinishedRepair) {
        for (const r of rows) {
          session.updates.set(r, {
            state: '含未闭环修复记录', color: null,
            note: '本剧尚有未闭环的 MoviePilot 整理修复任务，需先复核确认文件，整剧暂缓归档',
          });
        }
        summary.skipped++;
        continue;
      }

      // 2. 检查多级目标分类目录是否存在（递归建立并穿透刷新）
      const targetCatPath = `${dest}/${catName}`;
      const sourceCatPath = `${source}/${catName}`;
      let targetCatExists = false;
      try {
        targetCatExists = await ensureDirRecursive(cd2, targetCatPath);
        if (!targetCatExists) {
          for (const r of rows) {
            session.updates.set(r, {
              state: '缺少目标分类', color: null,
              note: `归档目标分类 ${targetCatPath} 不存在且自动创建失败，请先手动建立`,
            });
          }
          summary.skipped++;
          continue;
        }
      } catch (e) {
        for (const r of rows) session.updates.set(r, { state: '归档目标不可达', color: null, note: e.message });
        summary.errors++;
        continue;
      }

      // 3. 检查目标是否已有同名剧集（穿透刷新获取最新目录，杜绝生成 (1) 冲突文件夹）
      const targetShowPath = `${dest}/${catName}/${showName}`;
      let catContents = [];
      try {
        catContents = await cd2.listFiles(targetCatPath, { forceRefresh: true });
      } catch (catErr) {
        for (const r of rows) session.updates.set(r, { state: '读取目标分类失败', color: null, note: catErr.message });
        summary.errors++;
        continue;
      }

      const existing = catContents.find(f => f.directory && f.name?.toLowerCase() === showName.toLowerCase());

      if (existing && existing.name !== showName) {
        for (const r of rows) {
          session.updates.set(r, {
            state: '名称大小写冲突', color: null,
            note: `目标已有 "${existing.name}"，与源 "${showName}" 大小写不同`,
          });
        }
        summary.conflicts++;
        continue;
      }

      // 4. 完结齐全与增量季解耦判定
      const nonGreen = rows.filter(r => r.color !== 'green');
      const isIncrementalAllowed = Boolean(existing || allowSingleSeason);
      if (nonGreen.length > 0 && !isIncrementalAllowed) {
        for (const r of rows) {
          session.updates.set(r, {
            state: '整剧含非绿色季', color: null,
            note: `${nonGreen.length} 季未完成，整剧暂缓归档`,
          });
        }
        summary.skipped++;
        continue;
      }

      // 选定本次实际待归档的行任务
      const targetRows = rows.filter(r => r.color === 'green' && !isRepairPending(readRepairState(r.data)));
      if (!targetRows.length) {
        for (const r of rows) {
          session.updates.set(r, { state: '无完结齐全季', color: null, note: '本剧暂无可归档的完结齐全季' });
        }
        summary.skipped++;
        continue;
      }

      // 检查待归档行任务是否存在同季重复行
      const targetSeasonsNum = targetRows.map(r => r.season);
      if (new Set(targetSeasonsNum).size !== targetSeasonsNum.length) {
        for (const r of targetRows) {
          session.updates.set(r, { state: '同季重复', color: null, note: '同一季在报告中有多行' });
        }
        summary.errors++;
        continue;
      }

      // 5. 执行迁移
      if (existing) {
        // ── 增量季合入模式 ──
        const targetContents = await cd2.listFiles(targetShowPath, { forceRefresh: true });
        const sourceContents = await cd2.listFiles(showPath, { forceRefresh: true });

        const sourceSeasons = sourceContents.filter(f => f.directory && parseSeasonNumber(f.name) != null);
        const sourceFiles = sourceContents.filter(f => !f.directory);
        const targetSeasons = targetContents.filter(f => f.directory && parseSeasonNumber(f.name) != null);

        const targetSeasonsSet = new Set(targetSeasonsNum);
        const seasonsToMove = sourceSeasons.filter(s => targetSeasonsSet.has(parseSeasonNumber(s.name)));

        // 检查同季冲突
        const seasonConflicts = [];
        for (const s of seasonsToMove) {
          const sNum = parseSeasonNumber(s.name);
          const hit = targetSeasons.find(t => parseSeasonNumber(t.name) === sNum || t.name?.toLowerCase() === s.name?.toLowerCase());
          if (hit) {
            seasonConflicts.push(s.name);
            logger.warn(`  ⚠️ 同季冲突: ${s.name} 目标已存在同季目录 "${hit.name}"`);
          }
        }

        if (seasonConflicts.length > 0) {
          for (const r of targetRows) {
            session.updates.set(r, {
              state: '同季冲突，整剧暂缓', color: null,
              note: `目标已存在同季目录: ${seasonConflicts.join('、')}`,
            });
          }
          summary.conflicts++;
          continue;
        }

        const filesToMove = [];
        for (const f of sourceFiles) {
          const fName = f.name || '';
          const existsInTarget = targetContents.some(t => !t.directory && t.name?.toLowerCase() === fName.toLowerCase());
          if (!existsInTarget) {
            filesToMove.push(f);
          }
        }

        if (dryRun) {
          logger.info(`  📋 计划: 增量合入已有剧集目录 ${targetShowPath}`);
          for (const s of seasonsToMove) {
            logger.info(`     ↳ 移入季目录: ${s.name} -> ${targetShowPath}/${s.name}`);
          }
          for (const r of targetRows) {
            session.updates.set(r, {
              state: '待归档（合入预览）', color: null,
              note: `将增量合入 ${targetShowPath}，迁移 ${seasonsToMove.length} 个季目录`,
              source: r.path,
              target: `${targetShowPath}/${r.path.split('/').pop()}`,
            });
          }
          continue;
        }

        // 执行实际增量合入
        let moveFailed = false;
        for (const s of seasonsToMove) {
          try {
            await cd2.moveFile(s.path, targetShowPath);
            logger.info(`  ✅ 已移动季目录: ${s.name}`);
            const destSeasonPath = `${targetShowPath}/${s.name}`;
            await cd2.refreshDir(destSeasonPath);
          } catch (sErr) {
            logger.error(`  ❌ 移动季目录失败: ${s.name} - ${sErr.message}`);
            moveFailed = true;
            for (const r of targetRows) {
              session.updates.set(r, { state: '移动季失败', color: null, note: sErr.message });
            }
            summary.errors++;
            break;
          }
        }

        if (moveFailed) continue;

        for (const f of filesToMove) {
          try {
            await cd2.moveFile(f.path, targetShowPath);
          } catch (fErr) {
            logger.warn(`  ⚠️ 移动元数据失败: ${f.name} - ${fErr.message}`);
          }
        }

        try {
          await cd2.refreshDir(targetShowPath);
        } catch {}

        // 源目录清理逻辑：穿透确认源目录是否仍有其他未迁移季
        try {
          const remaining = await cd2.listFiles(showPath, { forceRefresh: true });
          const remSeasons = remaining.filter(f => f.directory && parseSeasonNumber(f.name) != null);
          if (remSeasons.length > 0) {
            const remNames = remSeasons.map(s => s.name);
            logger.info(`  📁 源目录仍有未迁移/连载季 (${remNames.join('、')})，安全保留源剧集目录`);
          } else {
            await cd2.deleteFile(showPath);
            logger.info(`  🗑 源端已无任何季目录，已安全清理源剧集空壳: ${showPath}`);
            await cd2.refreshDir(sourceCatPath);
          }
        } catch (delErr) {
          logger.warn(`  ⚠️ 检查/清理源剧集目录异常: ${delErr.message}`);
        }

      } else {
        // ── 全新整剧移动模式 ──
        if (targetRows.length === rows.length) {
          if (dryRun) {
            logger.info(`  📋 计划: 整剧移至 ${targetCatPath}`);
            for (const r of rows) {
              session.updates.set(r, {
                state: '待归档（整剧预览）', color: null,
                note: `将移至 ${targetShowPath}`,
                source: showPath,
                target: targetShowPath,
              });
            }
            continue;
          }

          try {
            await cd2.moveFile(showPath, targetCatPath);
            logger.info(`  ✅ 已移动整剧: ${showName}`);
            await cd2.refreshDir(targetShowPath);
            await cd2.refreshDir(targetCatPath);
            await cd2.refreshDir(sourceCatPath);
          } catch (mErr) {
            for (const r of rows) session.updates.set(r, { state: '移动失败', color: null, note: mErr.message });
            summary.errors++;
            continue;
          }
        } else {
          // 单季归档模式（源端尚有连载季）：先在目标端建立剧集目录，仅迁入完结季
          await ensureDirRecursive(cd2, targetShowPath);
          await cd2.refreshDir(targetShowPath);
          const sourceContents = await cd2.listFiles(showPath, { forceRefresh: true });
          const sourceSeasons = sourceContents.filter(f => f.directory && parseSeasonNumber(f.name) != null);
          const targetSeasonsSet = new Set(targetSeasonsNum);
          const seasonsToMove = sourceSeasons.filter(s => targetSeasonsSet.has(parseSeasonNumber(s.name)));

          if (dryRun) {
            logger.info(`  📋 计划: 单季先行归档至 ${targetShowPath}`);
            for (const s of seasonsToMove) {
              logger.info(`     ↳ 移入季目录: ${s.name}`);
            }
            for (const r of targetRows) {
              session.updates.set(r, {
                state: '待归档（单季预览）', color: null,
                note: `将先行归档至 ${targetShowPath}/${r.path.split('/').pop()}`,
                source: r.path,
                target: `${targetShowPath}/${r.path.split('/').pop()}`,
              });
            }
            continue;
          }

          let moveFailed = false;
          for (const s of seasonsToMove) {
            try {
              await cd2.moveFile(s.path, targetShowPath);
              logger.info(`  ✅ 已移动季目录: ${s.name}`);
              await cd2.refreshDir(`${targetShowPath}/${s.name}`);
            } catch (sErr) {
              logger.error(`  ❌ 移动季目录失败: ${s.name} - ${sErr.message}`);
              moveFailed = true;
              for (const r of targetRows) {
                session.updates.set(r, { state: '移动季失败', color: null, note: sErr.message });
              }
              summary.errors++;
              break;
            }
          }

          if (moveFailed) continue;
          await cd2.refreshDir(targetShowPath);
        }
      }

      // 验证最终移动状态
      try {
        const targetExists = await cd2.exists(targetShowPath);
        if (targetExists) {
          for (const r of targetRows) {
            session.updates.set(r, {
              state: '已归档',
              color: 'archived',
              note: `已归档至 ${targetShowPath}`,
              source: showPath,
              target: targetShowPath,
              at: new Date().toISOString(),
            });
          }
          summary.archived++;
        } else {
          for (const r of targetRows) {
            session.updates.set(r, {
              state: '验证未通过', color: 'yellow',
              note: '目标剧集目录不存在，请人工核实',
            });
          }
          summary.errors++;
        }
      } catch (vErr) {
        for (const r of targetRows) session.updates.set(r, { state: '验证异常', color: 'yellow', note: vErr.message });
        summary.errors++;
      }

    } catch (e) {
      for (const r of rows) session.updates.set(r, { state: '归档失败', color: null, note: e.message });
      summary.errors++;
    }
  }

  // ── 归档后对账复核 (Post-Archive Audit) ──
  let reviewSummary = null;
  if (!dryRun) {
    try {
      reviewSummary = await postArchiveReview({ config, logger, cd2, session });
      summary.postReview = reviewSummary;

      // 写入归档操作与对账快照审计日志
      if (summary.archived > 0) {
        try {
          const logDir = 'logs';
          await fs.mkdir(logDir, { recursive: true });
          const nowStr = new Date().toISOString().replace(/[:.]/g, '-');
          const auditFile = join(logDir, `archive_audit_${nowStr}.json`);
          const auditData = {
            timestamp: new Date().toISOString(),
            archived_shows_count: summary.archived,
            skipped_shows_count: summary.skipped,
            conflicts_count: summary.conflicts,
            errors_count: summary.errors,
            post_review_summary: reviewSummary
          };
          await fs.writeFile(auditFile, JSON.stringify(auditData, null, 2), 'utf-8');
          logger.info(`   📋 归档操作与对账审计快照已生成: ${auditFile}`);
        } catch (afErr) {
          logger.warn(`   ⚠️ 写入归档审计日志跳过: ${afErr.message}`);
        }
      }

    } catch (e) {
      logger.error(`❌ 归档后对账复核异常: ${e.message}`);
    }
  }

  // 写入报告（自动生成 .xlsx 与 .csv）
  const suffix = dryRun ? '_归档预览' : '_归档完成';
  const outputPath = reportPath.replace(/(?:_补缺下载|_复核预览|_复核清理|_归档预览|_归档完成)*\.xlsx$/i, `${suffix}.xlsx`);
  await writeReport(session, outputPath);

  logger.info(`\n📊 归档${dryRun ? '预览' : ''}完成:`);
  logger.info(`   ✅ 已归档 ${summary.archived}  ⏭ 跳过 ${summary.skipped}  ⚠️ 冲突 ${summary.conflicts}  ❌ 错误 ${summary.errors}`);
  logger.info(`   📄 报告: ${outputPath}`);

  return { ...summary, outputPath };
}

/**
 * 归档后全局对账复核 (Post-Archive Audit)
 *
 * 1. 目标目录精准“集数守恒”对账（实到正片视频数与报告登记集数精确比对）
 * 2. 源目录存量物理对账
 * 3. 残留别名目录排查（兼容多格式 TMDB ID 与标题回退比对）与空别名原样清理
 */
export async function postArchiveReview({ config, logger, cd2, session, autoCleanEmptyAliases = true }) {
  const source = (config.paths.source || '').replace(/\/+$/, '');
  const dest = (config.paths.destination || '').replace(/\/+$/, '');

  const lineSep = '═'.repeat(52);
  logger.info(`\n${lineSep}`);
  logger.info('🔍 开始执行: 归档后对账复核 (Post-Archive Audit)');
  logger.info(lineSep);

  const showGroups = new Map();
  for (const task of session.tasks) {
    const key = task.showPath || `${task.category}/${task.name}`;
    if (!showGroups.has(key)) showGroups.set(key, []);
    showGroups.get(key).push(task);
  }

  const archivedList = [];
  const skippedList = [];
  const categories = new Set();
  const archivedTmdbMap = new Map();
  const archivedTitleMap = new Map();

  for (const [showPath, rows] of showGroups) {
    const showName = rows[0].name;
    const catName = rows[0].category;
    categories.add(catName);

    const update = session.updates.get(rows[0]) || {};
    const isArchived = (
      update.state === '已归档' ||
      rows[0].data['处理结果'] === '已归档' ||
      rows[0].data['归档结果'] === '已归档'
    );

    const tmdbId = extractTmdbId(rows[0].data['TMDB ID'] || showName);
    const cleanTitle = extractCleanTitle(showName);

    const item = { showName, catName, showPath, tmdbId, cleanTitle, rows };
    if (isArchived) {
      archivedList.push(item);
      if (tmdbId) archivedTmdbMap.set(tmdbId, item);
      if (cleanTitle) archivedTitleMap.set(cleanTitle, item);
    } else {
      skippedList.push(item);
    }
  }

  const reviewSummary = {
    targetVerified: 0,
    targetFailed: 0,
    sourceMatches: true,
    cleanedAliases: [],
    residualWarnings: [],
    categories: {}
  };

  // 1. 目标目录精准“集数守恒”对账
  logger.info(`\n[复核-1] 正在核验目标目录 (${archivedList.length} 部已归档剧集)...`);
  for (const item of archivedList) {
    const targetPath = `${dest}/${item.catName}/${item.showName}`;
    try {
      const targetContents = await cd2.listFiles(targetPath, { forceRefresh: true });
      const seasons = targetContents.filter(f => f.directory);
      let totalVideos = 0;
      const seasonVideoMap = new Map();

      for (const s of seasons) {
        const seasonFiles = await cd2.listFiles(s.path, { forceRefresh: true });
        const vCount = seasonFiles.filter(sf => MEDIA_REGEX.test(sf.name)).length;
        totalVideos += vCount;
        const sNum = parseSeasonNumber(s.name);
        if (sNum != null) seasonVideoMap.set(sNum, vCount);
      }

      // 校验各季集数是否守恒
      const seasonMismatches = [];
      for (const r of item.rows) {
        let expEp = 0;
        for (const k of ['已识别集数', '来源登记集数', '视频文件数']) {
          const val = r.data[k];
          if (val != null && !Number.isNaN(Number(val)) && Number(val) > 0) {
            expEp = Number(val);
            break;
          }
        }
        const sNum = r.season;
        const actualEp = seasonVideoMap.get(sNum) || 0;
        if (expEp > 0 && actualEp < expEp) {
          seasonMismatches.push(`S${sNum}(实到${actualEp}/应到${expEp})`);
        }
      }

      if (totalVideos > 0 && seasonMismatches.length === 0) {
        reviewSummary.targetVerified++;
        for (const r of item.rows) {
          const prevUp = session.updates.get(r) || {};
          const prevFields = prevUp.fields || {};
          prevFields['归档复核'] = `目标已核验 (完整 ${seasons.length} 季 ${totalVideos} 集，账实相符)`;
          prevFields['复核核对时间'] = new Date().toISOString();
          prevUp.fields = prevFields;
          session.updates.set(r, prevUp);
        }
      } else {
        reviewSummary.targetFailed++;
        const errDesc = seasonMismatches.length > 0 ? `集数不守恒: ${seasonMismatches.join('、')}` : '未发现有效视频文件';
        logger.warn(`  ⚠️ 目标完整性异常: ${item.showName} - ${errDesc}`);
        for (const r of item.rows) {
          const prevUp = session.updates.get(r) || {};
          const prevFields = prevUp.fields || {};
          prevFields['归档复核'] = `⚠️ 完整性异常: ${errDesc}`;
          prevFields['复核核对时间'] = new Date().toISOString();
          prevUp.fields = prevFields;
          session.updates.set(r, prevUp);
        }
      }
    } catch (e) {
      reviewSummary.targetFailed++;
      logger.error(`  ❌ 目标目录不可达: ${item.showName} - ${e.message}`);
      for (const r of item.rows) {
        const prevUp = session.updates.get(r) || {};
        const prevFields = prevUp.fields || {};
        prevFields['归档复核'] = `❌ 目标不存在: ${e.message}`;
        prevFields['复核核对时间'] = new Date().toISOString();
        prevUp.fields = prevFields;
        session.updates.set(r, prevUp);
      }
    }
  }

  // 2. 源目录存量对账与别名残留排查
  logger.info('\n[复核-2] 正在对账源目录存量与排查别名残留...');
  for (const catName of categories) {
    const sourceCatPath = `${source}/${catName}`;
    const destCatPath = `${dest}/${catName}`;

    let sourceDirs = [];
    try {
      const srcList = await cd2.listFiles(sourceCatPath, { forceRefresh: true });
      sourceDirs = srcList.filter(f => f.directory);
    } catch (e) {
      logger.error(`  ❌ 无法读取源分类目录: ${sourceCatPath} - ${e.message}`);
      continue;
    }

    const destAllTmdbMap = new Map();
    const destAllTitleMap = new Map();
    try {
      const destList = await cd2.listFiles(destCatPath, { forceRefresh: true });
      for (const d of destList) {
        if (d.directory) {
          const tId = extractTmdbId(d.name);
          if (tId) destAllTmdbMap.set(tId, d.name);
          const cTitle = extractCleanTitle(d.name);
          if (cTitle) destAllTitleMap.set(cTitle, d.name);
        }
      }
    } catch {}

    const expectedSkipped = skippedList.filter(s => s.catName === catName);
    const catResult = {
      sourceActualCount: sourceDirs.length,
      expectedCount: expectedSkipped.length,
      unmovedShows: [],
      cleanedAliases: []
    };

    // 检查是否有本应归档但仍留在源目录的剧集
    for (const a of archivedList.filter(it => it.catName === catName)) {
      if (sourceDirs.some(d => d.name === a.showName)) {
        catResult.unmovedShows.push(a.showName);
        logger.error(`  ❌ 异常残留: 剧集 "${a.showName}" 已标记归档但仍在源目录！`);
      }
    }

    const effectiveTmdbMap = new Map(destAllTmdbMap);
    for (const [tId, item] of archivedTmdbMap) {
      effectiveTmdbMap.set(tId, item.showName);
    }

    const effectiveTitleMap = new Map(destAllTitleMap);
    for (const [cTitle, item] of archivedTitleMap) {
      effectiveTitleMap.set(cTitle, item.showName);
    }

    let needRefresh = false;
    for (const d of sourceDirs) {
      const dName = d.name || '';
      const dTmdb = extractTmdbId(dName);
      const dTitle = extractCleanTitle(dName);

      let targetMatchName = null;
      if (dTmdb && effectiveTmdbMap.has(dTmdb)) {
        targetMatchName = effectiveTmdbMap.get(dTmdb);
      } else if (dTitle && effectiveTitleMap.has(dTitle)) {
        targetMatchName = effectiveTitleMap.get(dTitle);
      }

      if (targetMatchName && targetMatchName !== dName) {
        let videoCount = 0;
        try {
          const subItems = await cd2.listFiles(d.path, { forceRefresh: true });
          for (const sub of subItems) {
            if (sub.directory) {
              const seasonItems = await cd2.listFiles(sub.path, { forceRefresh: true });
              videoCount += seasonItems.filter(sf => MEDIA_REGEX.test(sf.name)).length;
            } else if (MEDIA_REGEX.test(sub.name)) {
              videoCount++;
            }
          }
        } catch {
          videoCount = -1;
        }

        // 原样保留既有空别名清理判定
        if (videoCount === 0 && autoCleanEmptyAliases) {
          try {
            await cd2.deleteFile(d.path);
            catResult.cleanedAliases.push(dName);
            reviewSummary.cleanedAliases.push({
              category: catName, name: dName, targetName: targetMatchName, tmdbId: dTmdb
            });
            logger.info(`  🗑 已自动清理源端残留空别名目录: ${dName} (目标已有完整归档: ${targetMatchName})`);
            needRefresh = true;
          } catch (delErr) {
            logger.warn(`  ⚠️ 清理空别名目录失败: ${dName} - ${delErr.message}`);
          }
        } else if (videoCount > 0) {
          const warning = {
            category: catName, name: dName, targetName: targetMatchName,
            tmdbId: dTmdb, videoCount
          };
          reviewSummary.residualWarnings.push(warning);
          logger.warn(
            `  ⚠️ 发现源端残留别名剧集: "${dName}" (含 ${videoCount} 集)，` +
            `目标已有归档 "${targetMatchName}"，建议人工确认`
          );
        }
      }
    }

    if (needRefresh) {
      try {
        await cd2.refreshDir(sourceCatPath);
        const refList = await cd2.listFiles(sourceCatPath, { forceRefresh: true });
        catResult.sourceActualCount = refList.filter(f => f.directory).length;
      } catch {}
    }

    reviewSummary.categories[catName] = catResult;
  }

  // 输出复核对账汇总
  logger.info(`\n${lineSep}`);
  logger.info('📊 归档后对账复核汇总 (Post-Archive Reconciliation)');
  logger.info(lineSep);
  const verStatus = reviewSummary.targetFailed > 0 ? ` (❌ 异常 ${reviewSummary.targetFailed} 部)` : ' (100% 完整)';
  logger.info(`  🎯 目标完整性核验: ${reviewSummary.targetVerified}/${archivedList.length} 通过${verStatus}`);

  for (const [catName, catRes] of Object.entries(reviewSummary.categories)) {
    const isMatched = (catRes.sourceActualCount === catRes.expectedCount && catRes.unmovedShows.length === 0);
    const matchDesc = isMatched ? '(✅ 对账完全一致)' : '(⚠️ 存量存在差异)';
    logger.info(
      `  📂 分类 [${catName}] 源目录存量: 实际 ${catRes.sourceActualCount} 部 / ` +
      `预期 ${catRes.expectedCount} 部 ${matchDesc}`
    );
    if (catRes.unmovedShows.length > 0) {
      logger.error(`     ↳ ❌ 未成功移走剧集: ${catRes.unmovedShows.join(', ')}`);
    }
  }

  if (reviewSummary.cleanedAliases.length > 0) {
    logger.info(`  🗑 自动清理空别名残留: ${reviewSummary.cleanedAliases.length} 部:`);
    for (const c of reviewSummary.cleanedAliases) {
      logger.info(`     ↳ 已删除: ${c.name}`);
    }
  }

  if (reviewSummary.residualWarnings.length > 0) {
    logger.info(`  ⚠️ 别名残留需人工确认: ${reviewSummary.residualWarnings.length} 部:`);
    for (const w of reviewSummary.residualWarnings) {
      logger.info(`     ↳ 源端: ${w.name} (含 ${w.videoCount} 集) | 目标端: ${w.targetName}`);
    }
  }

  logger.info(`${lineSep}\n`);
  return reviewSummary;
}
