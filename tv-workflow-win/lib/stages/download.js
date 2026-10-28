/**
 * 阶段二：补缺下载（含 MoviePilot 整理修复联动 v1.5.0）
 *
 * 核心流程：
 * 1. 读取初检报告中的缺失项
 * 2. 【新增整理修复】优先检查 MoviePilot 整理历史：若已下载但转移失败/受阻，先执行单集历史解除与重转，避免重复下载
 * 3. 对无历史或未修复项，搜索资源站资源，按规则筛选（整季优先、中文优先、做种≥5优先）并提交下载
 * 4. 写回更新报告并保存防重复记录
 */
import { MoviePilotClient } from '../clients/moviepilot.js';
import { readReport, writeReport } from '../core/report-adapter.js';
import { parseEpisodeField, uniq, positive } from '../core/episode-parser.js';
import { cleanTitleText, createNormalizer } from '../core/title-matcher.js';
import { createDataStore } from '../platform/data-store.js';
import { checkRepair, executeRepair } from '../core/transfer-repair.js';
import { readRepairState } from '../core/repair-state.js';

const norm = createNormalizer();

/**
 * 精准判断是否为整季资源包
 */
function isSeasonPack(meta, t, taskSeason, total) {
  const eps = meta?.episode_list || [];
  if (total > 0 && eps.length === total) return true;
  const totalEp = meta?.total_episode;
  if (total > 0 && totalEp === total && eps.length === 0) return true;

  const beginSeason = meta?.begin_season;
  const seasonList = meta?.season_list || (beginSeason ? [beginSeason] : []);
  if (seasonList.length === 1 && seasonList[0] === taskSeason) {
    const beginEp = meta?.begin_episode;
    const endEp = meta?.end_episode;
    const seasonEp = String(meta?.season_episode || '').toUpperCase().trim();
    const isSeasonOnly = /^S\d{1,2}$/.test(seasonEp);

    if ((beginEp == null || beginEp === 0) && (endEp == null || endEp === 0) && eps.length === 0) {
      const title = String(t?.title || '');
      const sub = String(meta?.subtitle || t?.description || '');
      const combined = `${title} ${sub}`;
      if (!/(?:[Ee][Pp]?\s*\d+|第\s*\d+\s*集|\b\d+\s*集\b)/.test(combined)) {
        return true;
      }
      if (isSeasonOnly && !/[Ee][Pp]?\s*\d+/.test(title)) {
        return true;
      }
    }
  }
  return false;
}

/**
 * 执行补缺下载与整理修复
 * @param {object} config
 * @param {object} logger
 * @param {string} reportPath - 初检报告路径
 * @param {object} [options] - { repairOnly?: boolean, skipRepair?: boolean }
 * @returns {Promise<object>} 执行摘要
 */
export async function runDownload(config, logger, reportPath, options = {}) {
  if (!reportPath) throw new Error('请指定初检报告路径');

  const mp = new MoviePilotClient({
    origin: config.moviepilot.origin,
    token: config.moviepilot.token,
    logger,
  });
  const store = createDataStore('data');

  // 读取报告
  logger.info('📄 读取报告...');
  const session = readReport(reportPath, {
    origin: config.clouddrive2.origin,
    source: config.paths.source,
    mode: 'download',
  });
  logger.info(`📄 待处理: ${session.tasks.length} 项, 已跳过: ${session.skipped} 项绿色/已归档`);
  if (session.errors.length) {
    logger.warn(`⚠️ 读取错误 ${session.errors.length} 项`);
  }

  const summary = {
    total: session.tasks.length,
    repaired: 0,
    submitted: 0,
    existing: 0,
    noResource: 0,
    errors: 0
  };
  session.updates = new Map();

  const repairConfig = config.transferRepair || {
    source: config.paths.downloadPath || '/video/downloads/电视剧',
    target: config.paths.source || '/115/Library_incs/nas/电视剧'
  };

  // 逐项处理
  for (let i = 0; i < session.tasks.length; i++) {
    const task = session.tasks[i];
    const d = task.data;
    logger.progress('补缺', i + 1, session.tasks.length, `${task.name} Season ${task.season}`);

    try {
      const total = Number(d['来源登记集数']);
      if (!positive(total)) {
        session.updates.set(task, { state: '来源集数未确认', color: null, note: '请先补充初检结果' });
        summary.errors++;
        continue;
      }

      const title = cleanTitleText(task.name.replace(/\[tmdbid=\d+\]/ig, '').replace(/[（(](?:19|20)\d{2}[)）]/g, ''));
      const year = /[（(]((?:19|20)\d{2})[)）]/.exec(task.name)?.[1] || '';
      const tmdb = /\[tmdbid=(\d+)\]/i.exec(task.name)?.[1] || String(d['TMDB ID'] || '').trim();
      const missing = parseEpisodeField(d['缺失集号'], { blankUnknown: true });
      const future = parseEpisodeField(d['待播集号'] || '');
      const undated = parseEpisodeField(d['播期未知集号'] || '');
      const excluded = uniq([...(future || []), ...(undated || [])]);
      const needed = missing?.filter(n => !excluded.includes(n)) ?? null;

      task.title = title;
      task.year = year;
      task.tmdb = tmdb;
      task.needed = needed;

      // 无缺集时跳过
      if (needed !== null && needed.length === 0) {
        session.updates.set(task, { state: '无需下载', color: null, note: '报告没有已确认的缺失集号' });
        continue;
      }

      // ──【整理修复联动】优先检查 MoviePilot 整理历史中是否有现成受阻的文件 ──
      let repairedThisTurn = false;
      if (!options.skipRepair && tmdb && needed?.length) {
        try {
          const repairPlan = await checkRepair(task, mp, repairConfig);
          if (repairPlan.state === 'ready' && repairPlan.items.length > 0) {
            logger.info(`  🔧 发现 ${repairPlan.items.length} 个转移受阻集目，正在自动修复...`);
            const repairResult = await executeRepair(task, repairPlan, mp, repairConfig, {
              save: (rec) => store.put(`repair:${norm(task.name)}:${task.season}`, rec)
            });

            session.updates.set(task, {
              state: '已提交重转，待复核',
              color: 'yellow',
              repair: repairResult,
              note: `已重新触发 ${repairPlan.items.map(it => 'E' + it.episode).join('、')} 整理转移`,
              at: new Date().toISOString(),
            });
            summary.repaired++;
            repairedThisTurn = true;
          }
        } catch (re) {
          logger.debug(`  整理修复检查跳过: ${re.message}`);
        }
      }

      if (repairedThisTurn || options.repairOnly) {
        continue;
      }

      // ── 正常搜索与下载流程 ──
      const storeKey = `dl:${norm(task.name)}:${task.season}`;
      const records = store.get(storeKey, []);
      const accepted = records.filter(r => ['accepted', 'existing'].includes(r.state));

      // 若历史下载记录已覆盖全部缺集
      if (needed !== null && needed.every(n => accepted.some(r => r.episodes?.includes(n)))) {
        session.updates.set(task, {
          state: '已提交下载，待复核',
          color: 'yellow',
          note: '防重复记录已覆盖缺集',
          records: accepted,
          repair: readRepairState(task.data) || undefined
        });
        summary.existing++;
        continue;
      }

      // 搜索 MoviePilot 资源（先搜索默认站点）
      logger.debug(`  搜索默认站点资源: tmdb=${tmdb || '无'}, title=${title}`);
      let contexts = [];
      try {
        const defaultResults = await mp.searchMedia(tmdb, task.season, title, year);
        if (Array.isArray(defaultResults)) contexts.push(...defaultResults);
      } catch (e) {
        logger.warn(`  ⚠️ 默认站点搜索异常: ${e.message}`);
      }

      // 追加专属站点搜索（默认包含: 观众、天空、馒头）
      const extraSiteNames = config.moviepilot?.extraSearchSites ?? ['观众', '天空', '馒头'];
      if (Array.isArray(extraSiteNames) && extraSiteNames.length > 0) {
        try {
          const extraSiteIds = await mp.resolveSiteIds(extraSiteNames);
          if (extraSiteIds.length > 0) {
            logger.info(`  🔍 追加搜索专属站点 [${extraSiteNames.join('、')}]: tmdb=${tmdb || '无'}, title=${title}`);
            const extraContexts = await mp.searchMedia(tmdb, task.season, title, year, { sites: extraSiteIds });
            if (Array.isArray(extraContexts) && extraContexts.length > 0) {
              const prevCount = contexts.length;
              const seen = new Set(contexts.map(c => {
                const t = c.torrent_info;
                return (t && (t.enclosure || t.page_url || `${t.site_name}:${t.title}:${t.size}`)) || JSON.stringify(c);
              }));
              for (const ec of extraContexts) {
                const et = ec.torrent_info;
                const key = (et && (et.enclosure || et.page_url || `${et.site_name}:${et.title}:${et.size}`)) || JSON.stringify(ec);
                if (!seen.has(key)) {
                  seen.add(key);
                  contexts.push(ec);
                }
              }
              const addedCount = contexts.length - prevCount;
              logger.info(`     ↳ 从 [${extraSiteNames.join('、')}] 成功追加 ${addedCount} 条资源 (去重后总计 ${contexts.length} 条)`);
            }
          }
        } catch (extraErr) {
          logger.warn(`  ⚠️ 追加专属站点搜索跳过: ${extraErr.message}`);
        }
      }

      if (!contexts || !contexts.length) {
        session.updates.set(task, { state: '无合格资源', color: 'red', note: '未找到匹配资源' });
        summary.noResource++;
        continue;
      }

      // 筛选做种数 >= 2、季号匹配且能有效补缺的候选
      const eligible = contexts.filter(c => {
        const m = c.media_info, meta = c.meta_info, t = c.torrent_info;
        if (!t || !m) return false;
        const seasons = meta?.season_list || (meta?.begin_season ? [meta.begin_season] : []);
        if (seasons.length !== 1 || seasons[0] !== task.season) return false;
        const seeds = typeof t.seeders === 'number' ? t.seeders : parseInt(t.seeders, 10);
        if (!Number.isFinite(seeds) || seeds < 2) return false;

        const eps = meta?.episode_list || [];
        const isFull = isSeasonPack(meta, t, task.season, total);

        // 核心缺集过滤规则：
        if (needed && needed.length > 0) {
          if (!isFull) {
            if (eps.length > 0) {
              const hitNeeded = eps.some(ep => needed.includes(ep));
              if (!hitNeeded) return false;
            } else {
              return false;
            }
          }
        }

        // 排除已成功提交过的相同种子
        const tKey = `${t.site_name || ''}:${t.title || ''}:${t.size || ''}`;
        if (records.some(r => r.key === tKey && r.state === 'accepted')) {
          return false;
        }

        return true;
      });

      if (!eligible.length) {
        logger.warn(`  ⚠️ 检索到 ${contexts.length} 条资源，但做种数不足 2、季号不符或均不包含缺集，未选定有效资源`);
        session.updates.set(task, { state: '无合格资源', color: 'red', note: `检索到 ${contexts.length} 条，做种不足 2、季号不符或均不包含缺集` });
        summary.noResource++;
        continue;
      }

      // 区分整季包与散集多候选选取：
      const seasonPacks = eligible.filter(c => isSeasonPack(c.meta_info, c.torrent_info, task.season, total));
      const selectedCandidates = []; // Array of { context, hits, isFull }

      if (seasonPacks.length > 0) {
        seasonPacks.sort((a, b) => {
          const ta = a.torrent_info, tb = b.torrent_info;
          const sa = Number(ta.seeders) || 0, sb = Number(tb.seeders) || 0;
          const sza = Number(ta.size) || Infinity, szb = Number(tb.size) || Infinity;
          const cha = /中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan/i.test(JSON.stringify(ta.labels || []) + (ta.title || ''));
          const chb = /中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan/i.test(JSON.stringify(tb.labels || []) + (tb.title || ''));
          return (chb - cha) || ((sb >= 5 ? 1 : 0) - (sa >= 5 ? 1 : 0)) || (sza - szb) || (sb - sa);
        });
        selectedCandidates.push({
          context: seasonPacks[0],
          hits: Array.from({ length: total }, (_, i) => i + 1),
          isFull: true,
        });
      } else {
        const remainingNeeded = new Set(needed || []);
        while (remainingNeeded.size > 0) {
          const candidatesForRemaining = [];
          for (const c of eligible) {
            const eps = c.meta_info?.episode_list || [];
            const hits = eps.filter(ep => remainingNeeded.has(ep));
            if (hits.length > 0) {
              candidatesForRemaining.push({ context: c, hits });
            }
          }
          if (!candidatesForRemaining.length) break;

          candidatesForRemaining.sort((a, b) => {
            const ta = a.context.torrent_info, tb = b.context.torrent_info;
            const sa = Number(ta.seeders) || 0, sb = Number(tb.seeders) || 0;
            const sza = Number(ta.size) || Infinity, szb = Number(tb.size) || Infinity;
            const cha = /中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan/i.test(JSON.stringify(ta.labels || []) + (ta.title || ''));
            const chb = /中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan/i.test(JSON.stringify(tb.labels || []) + (tb.title || ''));
            return (
              (b.hits.length - a.hits.length) ||
              (chb - cha) ||
              ((sb >= 5 ? 1 : 0) - (sa >= 5 ? 1 : 0)) ||
              (sza - szb) ||
              (sb - sa)
            );
          });

          const bestCand = candidatesForRemaining[0];
          selectedCandidates.push({
            context: bestCand.context,
            hits: bestCand.hits,
            isFull: false,
          });
          for (const ep of bestCand.hits) {
            remainingNeeded.delete(ep);
          }
        }
      }

      if (!selectedCandidates.length) {
        logger.warn(`  ⚠️ 未能匹配到可覆盖缺集的资源`);
        session.updates.set(task, { state: '无合格资源', color: 'red', note: `检索到 ${contexts.length} 条，但无资源能覆盖缺失集号` });
        summary.noResource++;
        continue;
      }

      // 批量向 MoviePilot 提交下载
      const submittedRecs = [];
      const allCoveredEps = new Set();

      for (const item of selectedCandidates) {
        const { context: cand, hits, isFull } = item;
        const candT = cand.torrent_info;
        const candMeta = cand.meta_info || {};
        const title = String(candT.title || '');
        const site = String(candT.site_name || '');
        const seeds = Number(candT.seeders) || 0;
        const descType = isFull ? '整季' : `缺集 [${hits.join('、')}]`;

        const tKey = `${site}:${title}:${candT.size || ''}`;
        if (records.some(r => r.key === tKey && r.state === 'accepted')) {
          logger.debug(`  ⏭ 排除已提交过相同种子: ${title}`);
          continue;
        }

        logger.info(`  🎯 选定最佳资源: [${site}] ${title} (做种: ${seeds}, ${descType})`);
        try {
          logger.info(`  🚀 正在向 MoviePilot 提交下载请求: ${title}...`);
          const reply = await mp.submitDownload(cand.media_info, candT);
          const isSuccess = !!reply?.success;
          const cCovered = isFull ? Array.from({ length: total }, (_, i) => i + 1) : (candMeta.episode_list || hits);
          const record = {
            key: tKey,
            state: isSuccess ? 'accepted' : 'rejected',
            id: reply?.data?.download_id || '',
            title,
            site,
            episodes: cCovered,
            seeds,
            at: new Date().toISOString(),
          };
          records.push(record);
          store.put(storeKey, records);

          if (isSuccess) {
            logger.info(`  ✅ MoviePilot 已接受下载任务: ${title}`);
            submittedRecs.push(record);
            for (const ep of cCovered) allCoveredEps.add(ep);
            summary.submitted++;
          } else {
            const msg = reply?.message || '未知原因';
            logger.error(`  ❌ MoviePilot 拒绝添加下载: ${msg}`);
            summary.errors++;
          }
        } catch (subErr) {
          logger.error(`  💥 提交下载异常 [${title}]: ${subErr.message}`);
          summary.errors++;
        }
      }

      if (submittedRecs.length > 0) {
        const first = submittedRecs[0];
        const isSp = selectedCandidates[0]?.isFull;
        const desc = isSp ? '整季' : '零散';
        const noteStr = submittedRecs.length === 1
          ? `${first.title} (做种 ${first.seeds}, ${desc})`
          : `已提交 ${submittedRecs.length} 个资源补缺，共覆盖 ${allCoveredEps.size} 集`;

        session.updates.set(task, {
          state: '已提交下载，待复核',
          color: 'yellow',
          note: noteStr,
          records: submittedRecs,
          covered: Array.from(allCoveredEps).sort((a, b) => a - b),
          repair: readRepairState(task.data) || undefined,
          at: submittedRecs[0].at,
        });
      } else if (!submittedRecs.length && selectedCandidates.length > 0) {
        session.updates.set(task, {
          state: '下载提交失败',
          color: null,
          note: 'MoviePilot 拒绝添加下载任务',
          records: records.slice(-selectedCandidates.length),
        });
      }
    } catch (e) {
      session.updates.set(task, { state: '处理失败', color: null, note: e.message });
      summary.errors++;
    }
  }

  // 写入更新报告
  const outputPath = reportPath.replace(/\.xlsx$/i, '_补缺下载.xlsx');
  await writeReport(session, outputPath);

  logger.info(`\n📊 补缺下载与整理修复完成:`);
  logger.info(`   🔧 自动修复重转 ${summary.repaired}  ✅ 新增下载 ${summary.submitted}  📦 现有任务 ${summary.existing}  🔴 无资源 ${summary.noResource}  ❌ 错误 ${summary.errors}`);
  logger.info(`   📄 报告: ${outputPath}`);

  return { ...summary, outputPath };
}
