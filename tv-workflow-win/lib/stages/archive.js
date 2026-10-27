/**
 * 阶段四：剧集归档
 * 将绿色完结剧集从源目录移动到归档目录，验证完整性后清理源空目录
 */
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { readReport, writeReport } from '../core/report-adapter.js';
import { seasonNumber as parseSeasonNumber } from '../core/title-matcher.js';
import { episodeFromName, MEDIA_REGEX } from '../core/episode-parser.js';
import { readRepairState, isRepairPending } from '../core/repair-state.js';

/**
 * 执行归档阶段
 * @param {object} config
 * @param {object} logger
 * @param {string} reportPath
 * @param {{ dryRun?: boolean }} options
 * @returns {Promise<object>}
 */
export async function runArchive(config, logger, reportPath, { dryRun = true } = {}) {
  if (!reportPath) throw new Error('请指定报告路径');

  const cd2 = new CloudDriveClient({
    origin: config.clouddrive2.origin,
    token: config.clouddrive2.token,
    logger,
  });
  const source = config.paths.source;
  const dest = config.paths.destination;

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
    const key = task.showPath;
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
      // 检查：是否存在未闭环的修复记录（v1.3.0 规约）
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

      // 检查：所有季必须是绿色
      const nonGreen = rows.filter(r => r.color !== 'green');
      if (nonGreen.length) {
        for (const r of rows) {
          session.updates.set(r, {
            state: '整剧含非绿色季', color: null,
            note: `${nonGreen.length} 季未完成，整剧暂缓归档`,
          });
        }
        summary.skipped++;
        continue;
      }

      // 检查重复行
      const seasons = rows.map(r => r.season);
      if (new Set(seasons).size !== seasons.length) {
        for (const r of rows) {
          session.updates.set(r, { state: '同季重复', color: null, note: '同一季在报告中有多行' });
        }
        summary.errors++;
        continue;
      }

      // 目标与源分类路径
      const targetCatPath = `${dest}/${catName}`;
      const sourceCatPath = `${source}/${catName}`;
      let targetCatExists = false;
      try {
        targetCatExists = await cd2.exists(targetCatPath);
        if (!targetCatExists) {
          // 尝试自动创建目标分类目录
          try {
            await cd2.mkdir(targetCatPath);
            targetCatExists = true;
            logger.info(`  📁 目标分类目录不存在，已自动创建: ${targetCatPath}`);
          } catch (mkdirErr) {
            logger.warn(`  ⚠️ 自动创建目标分类失败: ${mkdirErr.message}`);
          }
        }
      } catch (e) {
        for (const r of rows) session.updates.set(r, { state: '归档目标不可达', color: null, note: e.message });
        summary.errors++;
        continue;
      }
      if (!targetCatExists) {
        for (const r of rows) {
          session.updates.set(r, { state: '缺少目标分类', color: null, note: `归档目标 ${targetCatPath} 不存在且自动创建失败，请先手动建立` });
        }
        summary.skipped++;
        continue;
      }

      // 检查目标是否已有同名剧集
      const targetShowPath = `${dest}/${catName}/${showName}`;
      const catContents = await cd2.listFiles(targetCatPath);
      const existing = catContents.find(f =>
        f.directory && f.name.normalize('NFC').toLowerCase() === showName.normalize('NFC').toLowerCase()
      );

      if (existing && existing.name !== showName) {
        for (const r of rows) {
          session.updates.set(r, { state: '名称大小写冲突', color: null, note: `目标已有 "${existing.name}"，与源 "${showName}" 大小写不同` });
        }
        summary.conflicts++;
        continue;
      }

      if (existing) {
        // 同名剧集已存在 → 增量季合入模式
        const targetContents = await cd2.listFiles(targetShowPath);
        const sourceContents = await cd2.listFiles(showPath);

        // 分离源目录中的季目录与根文件
        const sourceSeasons = sourceContents.filter(f => f.directory && parseSeasonNumber(f.name) !== null);
        const sourceFiles = sourceContents.filter(f => !f.directory);
        const targetSeasons = targetContents.filter(f => f.directory && parseSeasonNumber(f.name) !== null);

        // 检查季冲突：仅当源中的季在目标中已经存在时，才构成真正的同季冲突
        const seasonConflicts = [];
        for (const s of sourceSeasons) {
          const sNum = parseSeasonNumber(s.name);
          const hit = targetSeasons.find(t =>
            parseSeasonNumber(t.name) === sNum || t.name.normalize('NFC').toLowerCase() === s.name.normalize('NFC').toLowerCase()
          );
          if (hit) {
            seasonConflicts.push({ source: s.name, target: hit.name });
            logger.warn(`  ⚠️ 同季冲突: ${s.name} 目标已存在同季目录 "${hit.name}"`);
          }
        }

        if (seasonConflicts.length > 0) {
          for (const r of rows) {
            session.updates.set(r, {
              state: '同季冲突，整剧暂缓', color: null,
              note: `目标已存在同季目录: ${seasonConflicts.map(c => c.source).join('、')}`,
            });
          }
          summary.conflicts++;
          continue;
        }

        // 识别元数据文件：目标已存在的重复文件跳过，目标缺失的文件准备一同迁移
        const filesToMove = [];
        const filesToSkip = [];
        for (const f of sourceFiles) {
          const existsInTarget = targetContents.some(t =>
            !t.directory && t.name.normalize('NFC').toLowerCase() === f.name.normalize('NFC').toLowerCase()
          );
          if (existsInTarget) {
            filesToSkip.push(f);
            logger.debug(`  ℹ️ 目标已有同名元数据，源端将随目录清理: ${f.name}`);
          } else {
            filesToMove.push(f);
          }
        }

        if (dryRun) {
          logger.info(`  📋 计划: 增量合入已有剧集目录 ${targetShowPath}`);
          for (const s of sourceSeasons) {
            logger.info(`     ↳ 移入季目录: ${s.name} -> ${targetShowPath}/${s.name}`);
          }
          if (filesToMove.length) {
            logger.info(`     ↳ 补充移入新元数据: ${filesToMove.map(f => f.name).join(', ')}`);
          }
          if (filesToSkip.length) {
            logger.info(`     ↳ 重复元数据（最后自动清理）: ${filesToSkip.map(f => f.name).join(', ')}`);
          }
          logger.info(`     ↳ 完成后删除源剧集目录并刷新操作路径`);

          for (const r of rows) {
            session.updates.set(r, {
              state: '待归档（合入预览）', color: null,
              note: `将增量合入 ${targetShowPath}，迁移 ${sourceSeasons.length} 个季目录`,
              source: r.path,
              target: `${targetShowPath}/${r.path.split('/').pop()}`,
            });
          }
          continue;
        }

        // ── 实际执行增量合入（严格遵照 5 步与操作后刷新规约）──
        let moveFailed = false;

        // 1. 移动季目录
        for (const s of sourceSeasons) {
          try {
            await cd2.moveFile(s.path, targetShowPath);
            logger.info(`  ✅ 已移动季目录: ${s.name}`);

            // 2. 刷新目标季目录（进入目标季目录刷新页面）
            const destSeasonPath = `${targetShowPath}/${s.name}`;
            await cd2.refreshDir(destSeasonPath);
            logger.debug(`  🔄 已刷新目标季目录: ${destSeasonPath}`);
          } catch (e) {
            logger.error(`  ❌ 移动季目录失败: ${s.name} - ${e.message}`);
            moveFailed = true;
            for (const r of rows) session.updates.set(r, { state: '移动季失败', color: null, note: e.message });
            summary.errors++;
            break;
          }
        }
        if (moveFailed) continue;

        // 3. 补充移动目标缺失的元数据文件（如有）
        for (const f of filesToMove) {
          try {
            await cd2.moveFile(f.path, targetShowPath);
            logger.debug(`  ✅ 已补充移动元数据: ${f.name}`);
          } catch (e) {
            logger.warn(`  ⚠️ 移动元数据失败: ${f.name} - ${e.message}`);
          }
        }

        // 4. 刷新目标剧集目录（进入目标剧集目录刷新页面）
        try {
          await cd2.refreshDir(targetShowPath);
          logger.debug(`  🔄 已刷新目标剧集目录: ${targetShowPath}`);
        } catch (e) {
          logger.warn(`  ⚠️ 刷新目标剧集目录异常: ${e.message}`);
        }

        // 5. 校验源目录并删除源剧集目录
        try {
          const remaining = await cd2.listFiles(showPath);
          const remainingSeasons = remaining.filter(f => f.directory && parseSeasonNumber(f.name) !== null);
          if (remainingSeasons.length > 0) {
            throw new Error(`源目录仍有未迁移季: ${remainingSeasons.map(s => s.name).join(', ')}，拒绝删除`);
          }

          // 删除源剧集目录（剩余的重复元数据或空目录直接连同整剧目录删除）
          await cd2.deleteFile(showPath);
          logger.info(`  🗑 已删除源剧集目录: ${showPath}`);

          // 6. 刷新源分类目录（进入源分类目录刷新页面）
          await cd2.refreshDir(sourceCatPath);
          logger.debug(`  🔄 已刷新源分类目录: ${sourceCatPath}`);
        } catch (e) {
          logger.error(`  ❌ 清理源剧集目录失败: ${e.message}`);
          for (const r of rows) session.updates.set(r, { state: '清理源目录失败', color: 'yellow', note: e.message });
          summary.errors++;
          continue;
        }

      } else {
        // 目标无同名剧集 → 整剧全新移动
        if (dryRun) {
          logger.info(`  📋 计划: 整剧移至 ${targetCatPath}`);
          for (const r of rows) {
            session.updates.set(r, {
              state: '待归档（整剧预览）', color: null,
              note: `将移至 ${targetShowPath}`,
              source: showPath, target: targetShowPath,
            });
          }
          continue;
        }

        try {
          await cd2.moveFile(showPath, targetCatPath);
          logger.info(`  ✅ 已移动整剧: ${showName}`);

          // 刷新目标剧集目录、目标分类目录、源分类目录
          await cd2.refreshDir(targetShowPath);
          await cd2.refreshDir(targetCatPath);
          await cd2.refreshDir(sourceCatPath);
          logger.debug(`  🔄 已刷新目标与源相关目录`);
        } catch (e) {
          for (const r of rows) session.updates.set(r, { state: '移动失败', color: null, note: e.message });
          summary.errors++;
          continue;
        }
      }

      // 验证最终状态
      try {
        const targetExists = await cd2.exists(targetShowPath);
        const sourceGone = !(await cd2.exists(showPath));
        if (targetExists && sourceGone) {
          for (const r of rows) {
            session.updates.set(r, {
              state: '已归档', color: 'archived',
              note: `已归档至 ${targetShowPath}`,
              source: showPath, target: targetShowPath,
              targetUrl: `${config.clouddrive2.origin}/?page=files&path=${encodeURIComponent(targetShowPath)}`,
              at: new Date().toISOString(),
            });
          }
          summary.archived++;
        } else {
          for (const r of rows) {
            session.updates.set(r, {
              state: '验证未通过', color: 'yellow',
              note: `验证失败 (目标存在:${targetExists}, 源已消失:${sourceGone})，请人工核实`,
            });
          }
          summary.errors++;
        }
      } catch (e) {
        for (const r of rows) session.updates.set(r, { state: '验证异常', color: 'yellow', note: e.message });
        summary.errors++;
      }

    } catch (e) {
      for (const r of rows) session.updates.set(r, { state: '归档失败', color: null, note: e.message });
      summary.errors++;
    }
  }

  // ── 归档后对账复核 ──
  if (!dryRun) {
    try {
      const reviewSummary = await postArchiveReview({ config, logger, cd2, session });
      summary.postReview = reviewSummary;
    } catch (e) {
      logger.error(`❌ 归档后对账复核异常: ${e.message}`);
    }
  }

  // 写入报告
  const suffix = dryRun ? '_归档预览' : '_归档完成';
  const outputPath = reportPath.replace(/(?:_补缺下载|_复核预览|_复核清理|_归档预览|_归档完成)*\.xlsx$/i, `${suffix}.xlsx`);
  await writeReport(session, outputPath);

  logger.info(`\n📊 归档${dryRun ? '预览' : ''}完成:`);
  logger.info(`   ✅ 已归档 ${summary.archived}  ⏭ 跳过 ${summary.skipped}  ⚠️ 冲突 ${summary.conflicts}  ❌ 错误 ${summary.errors}`);
  logger.info(`   📄 报告: ${outputPath}`);

  return { ...summary, outputPath };
}

/**
 * 归档后全局对账复核 (Post-Archive Verification & Reconciliation)
 *
 * 核心检查项：
 * 1. 目标目录完整性核验：核验每部已归档剧集在目标目录下是否存在、季目录及有效视频文件（非空）
 * 2. 源目录存量对账：物理扫描源分类目录，与报告对账，确保归档剧集已完全脱离，留存剧集与跳过数量匹配
 * 3. 别名与残留目录智能检测与自动清理：
 *    - 扫描源目录中与“已归档剧集”共享同一 TMDB ID 的历史残留别名目录
 *    - 若残留别名目录为空（0 视频文件），自动安全清理删除并刷新源目录
 *    - 若残留别名目录包含残余视频，且目标目录已有完整剧集，记录预警提示
 * 4. 汇总对账统计，输出清晰对账报告卡，并在 Excel 报表中回写核验结果
 *
 * @param {object} params
 * @param {object} params.config
 * @param {object} params.logger
 * @param {CloudDriveClient} params.cd2
 * @param {object} params.session
 * @param {{ autoCleanEmptyAliases?: boolean }} [options]
 * @returns {Promise<object>}
 */
export async function postArchiveReview({ config, logger, cd2, session }, { autoCleanEmptyAliases = true } = {}) {
  const source = config.paths.source;
  const dest = config.paths.destination;

  logger.info(`\n${'═'.repeat(52)}`);
  logger.info(`🔍 开始执行: 归档后对账复核 (Post-Archive Audit)`);
  logger.info(`${'═'.repeat(52)}`);

  // 按剧集分组提取已归档与跳过任务
  const showGroups = new Map();
  for (const task of session.tasks) {
    const key = task.showPath;
    if (!showGroups.has(key)) showGroups.set(key, []);
    showGroups.get(key).push(task);
  }

  const archivedList = [];
  const skippedList = [];
  const categories = new Set();
  const archivedTmdbMap = new Map(); // tmdbId -> item

  for (const [showPath, rows] of showGroups) {
    const showName = rows[0].name;
    const catName = rows[0].category;
    categories.add(catName);

    const update = session.updates.get(rows[0]);
    const isArchived = update?.state === '已归档' || rows[0].data['处理结果'] === '已归档' || rows[0].data['归档结果'] === '已归档';

    const tmdbIdRaw = rows[0].data['TMDB ID'] || showName.match(/\[tmdbid=(\d+)\]/i)?.[1];
    const tmdbId = tmdbIdRaw ? Number(tmdbIdRaw) : null;

    if (isArchived) {
      const item = { showName, catName, showPath, tmdbId, rows };
      archivedList.push(item);
      if (tmdbId) archivedTmdbMap.set(tmdbId, item);
    } else {
      skippedList.push({ showName, catName, showPath, tmdbId, rows });
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

  // 1. 目标目录完整性核验
  logger.info(`\n[复核-1] 正在核验目标目录 (${archivedList.length} 部已归档剧集)...`);
  for (const item of archivedList) {
    const targetPath = `${dest}/${item.catName}/${item.showName}`;
    try {
      const targetContents = await cd2.listFiles(targetPath);
      const seasons = targetContents.filter(f => f.directory);
      let totalVideos = 0;
      for (const s of seasons) {
        const seasonFiles = await cd2.listFiles(s.path);
        totalVideos += seasonFiles.filter(f => MEDIA_REGEX.test(f.name)).length;
      }

      if (totalVideos > 0) {
        reviewSummary.targetVerified++;
        for (const r of item.rows) {
          const prevUpdate = session.updates.get(r) || {};
          session.updates.set(r, {
            ...prevUpdate,
            fields: {
              ...(prevUpdate.fields || {}),
              '归档复核': `目标已核验 (完整 ${seasons.length} 季 ${totalVideos} 集)`,
              '复核核对时间': new Date().toISOString()
            }
          });
        }
      } else {
        reviewSummary.targetFailed++;
        logger.warn(`  ⚠️ 目标完整性异常: ${item.showName} 未发现有效视频文件`);
        for (const r of item.rows) {
          const prevUpdate = session.updates.get(r) || {};
          session.updates.set(r, {
            ...prevUpdate,
            fields: {
              ...(prevUpdate.fields || {}),
              '归档复核': '⚠️ 目标缺失视频文件',
              '复核核对时间': new Date().toISOString()
            }
          });
        }
      }
    } catch (e) {
      reviewSummary.targetFailed++;
      logger.error(`  ❌ 目标目录不可达: ${item.showName} - ${e.message}`);
      for (const r of item.rows) {
        const prevUpdate = session.updates.get(r) || {};
        session.updates.set(r, {
          ...prevUpdate,
          fields: {
            ...(prevUpdate.fields || {}),
            '归档复核': `❌ 目标不存在: ${e.message}`,
            '复核核对时间': new Date().toISOString()
          }
        });
      }
    }
  }

  // 2. 源目录存量对账与别名残留排查
  logger.info(`\n[复核-2] 正在对账源目录存量与排查别名残留...`);
  for (const catName of categories) {
    const sourceCatPath = `${source}/${catName}`;
    const destCatPath = `${dest}/${catName}`;
    let sourceDirs = [];
    try {
      const srcList = await cd2.listFiles(sourceCatPath);
      sourceDirs = srcList.filter(f => f.directory);
    } catch (e) {
      logger.error(`  ❌ 无法读取源分类目录: ${sourceCatPath} - ${e.message}`);
      continue;
    }

    // 亦查询目标分类中所有剧集的 TMDB ID 集合（包括历史归档的剧集）
    const destAllTmdbMap = new Map();
    try {
      const destList = await cd2.listFiles(destCatPath);
      for (const d of destList.filter(f => f.directory)) {
        const m = d.name.match(/\[tmdbid=(\d+)\]/i);
        if (m) destAllTmdbMap.set(Number(m[1]), d.name);
      }
    } catch (e) {
      logger.debug(`  读取目标分类列表异常: ${e.message}`);
    }

    const expectedSkipped = skippedList.filter(s => s.catName === catName);
    const catResult = {
      sourceActualCount: sourceDirs.length,
      expectedCount: expectedSkipped.length,
      unmovedShows: [],
      cleanedAliases: []
    };

    // 检查是否有本应归档但仍在源目录的剧集
    for (const a of archivedList.filter(item => item.catName === catName)) {
      if (sourceDirs.some(d => d.name === a.showName)) {
        catResult.unmovedShows.push(a.showName);
        logger.error(`  ❌ 异常残留: 剧集 "${a.showName}" 已标记归档但仍在源目录！`);
      }
    }

    // 检查同 TMDB ID 别名残留（对照本批次已归档与历史已归档）
    const effectiveArchivedTmdbMap = new Map([...destAllTmdbMap.entries(), ...archivedTmdbMap.entries()]);

    let needRefresh = false;
    for (const d of sourceDirs) {
      const m = d.name.match(/\[tmdbid=(\d+)\]/i);
      const dTmdb = m ? Number(m[1]) : null;

      if (!dTmdb) continue;
      const targetMatchName = effectiveArchivedTmdbMap.get(dTmdb)?.showName || effectiveArchivedTmdbMap.get(dTmdb);

      // 若目标中已有相同 TMDB ID 的剧集，且源端目录名与目标不同（别名残留）
      if (targetMatchName && targetMatchName !== d.name) {
        // 检查该别名目录中的文件
        let videoCount = 0;
        try {
          const subItems = await cd2.listFiles(d.path);
          for (const item of subItems) {
            if (item.directory) {
              const seasonItems = await cd2.listFiles(item.path);
              videoCount += seasonItems.filter(sf => MEDIA_REGEX.test(sf.name)).length;
            } else if (MEDIA_REGEX.test(item.name)) {
              videoCount++;
            }
          }
        } catch {
          videoCount = -1;
        }

        if (videoCount === 0 && autoCleanEmptyAliases) {
          try {
            await cd2.deleteFile(d.path);
            catResult.cleanedAliases.push(d.name);
            reviewSummary.cleanedAliases.push({ category: catName, name: d.name, targetName: targetMatchName, tmdbId: dTmdb });
            logger.info(`  🗑 已自动清理源端残留空别名目录: ${d.name} (目标已有完整归档: ${targetMatchName})`);
            needRefresh = true;
          } catch (delErr) {
            logger.warn(`  ⚠️ 清理空别名目录失败: ${d.name} - ${delErr.message}`);
          }
        } else if (videoCount > 0) {
          const warning = { category: catName, name: d.name, targetName: targetMatchName, tmdbId: dTmdb, videoCount };
          reviewSummary.residualWarnings.push(warning);
          logger.warn(`  ⚠️ 发现源端残留别名剧集: "${d.name}" (含 ${videoCount} 个视频)，目标已有归档 "${targetMatchName}" [tmdbid=${dTmdb}]，建议清理避免重复下载`);
        }
      }
    }

    if (needRefresh) {
      try {
        await cd2.refreshDir(sourceCatPath);
        const refreshedList = await cd2.listFiles(sourceCatPath);
        catResult.sourceActualCount = refreshedList.filter(f => f.directory).length;
      } catch (e) {
        logger.debug(`  刷新目录异常: ${e.message}`);
      }
    }

    reviewSummary.categories[catName] = catResult;
  }

  // 输出结构化复核报告卡
  logger.info(`\n${'═'.repeat(52)}`);
  logger.info(`📊 归档后对账复核汇总 (Post-Archive Reconciliation)`);
  logger.info(`${'═'.repeat(52)}`);
  logger.info(`  🎯 目标完整性核验: ${reviewSummary.targetVerified}/${archivedList.length} 通过` +
    (reviewSummary.targetFailed > 0 ? ` (❌ 异常 ${reviewSummary.targetFailed} 部)` : ` (100% 完整)`)
  );

  for (const [catName, catRes] of Object.entries(reviewSummary.categories)) {
    const isMatched = catRes.sourceActualCount === catRes.expectedCount && catRes.unmovedShows.length === 0;
    logger.info(`  📂 分类 [${catName}] 源目录存量: 实际 ${catRes.sourceActualCount} 部 / 预期 ${catRes.expectedCount} 部 ` +
      (isMatched ? `(✅ 对账完全一致)` : `(⚠️ 存量存在差异)`)
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
  logger.info(`${'═'.repeat(52)}\n`);

  return reviewSummary;
}
