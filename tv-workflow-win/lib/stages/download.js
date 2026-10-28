/**
 * 阶段二：补缺下载（含 MoviePilot 整理历史修复联动、活跃下载队列实时对齐与多维精准选种）
 *
 * 核心流程：
 * 1. 读取初检报告中的缺失项
 * 2. 【整理修复优先】检查 MoviePilot 整理历史：若已下载但转移受阻，自动解除单条历史并重新触发转移
 * 3. 【实时下载队列对齐】查询 MoviePilot 活跃下载任务，若缺集已在下载中则避免重复提交，仅补齐剩余缺集
 * 4. 对需下载项，搜索默认站点并追加专属站点（如“观众”、“天空”、“馒头”），按多维规则打分优选：
 *    - 整季包能效比决策（Yield Ratio）：缺集少且散集可100%覆盖时优先散集，避免下几十G大包
 *    - 散集贪心多候选覆盖：清晰度梯队（优先1080p/2160p，惩罚720p/SD）、单集健康码率（200MB~3.5GB）、冗余惩罚、发布组一致性
 *    - 频控与提交熔断：连续提交间隔 0.5s，单季上限 15 个任务
 * 5. 写回更新报告并保存防重复记录到持久化存储
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
export function isSeasonPack(meta, t, taskSeason, total) {
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
 * 将数字列表折叠成紧凑格式，如 [10, 12, 13, 14, 15] -> '10、12-15'
 */
export function formatRanges(eps) {
  if (!eps || !eps.length) return '';
  const sorted = Array.from(new Set(eps)).sort((a, b) => a - b);
  const ranges = [];
  let start = sorted[0];
  let prev = sorted[0];
  for (let i = 1; i < sorted.length; i++) {
    const n = sorted[i];
    if (n === prev + 1) {
      prev = n;
    } else {
      ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
      start = n;
      prev = n;
    }
  }
  ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
  return ranges.join('、');
}

/**
 * 解析 MoviePilot active download 中的集数字符串（如 'E01-E06', 'E12', ''）
 */
export function parseActiveEpisodes(epStr, total = 0) {
  const s = String(epStr || '').trim();
  if (!s) {
    return total > 0 ? new Set(Array.from({ length: total }, (_, i) => i + 1)) : new Set();
  }
  const eps = new Set();
  const re = /(?:E|EP)?(\d+)(?:\s*[-~～至到_\ufffd]\s*(?:E|EP)?(\d+))?/gi;
  let m;
  while ((m = re.exec(s)) !== null) {
    const start = parseInt(m[1], 10);
    if (m[2]) {
      const end = parseInt(m[2], 10);
      const [from, to] = start <= end ? [start, end] : [end, start];
      for (let i = from; i <= to; i++) eps.add(i);
    } else {
      eps.add(start);
    }
  }
  return eps;
}

/**
 * 解析清晰度并计算画质梯队得分（优先 1080p/2160p，惩罚 720p/480p）
 */
export function getResolutionScore(t = {}, meta = {}) {
  const text = `${t.title || ''} ${meta.resource_pix || ''} ${(t.labels || []).join(' ')}`.toLowerCase();
  if (/2160p|4k|uhd/.test(text)) return 900;
  if (/1080p|1080i|fhd/.test(text)) return 1000;
  if (/720p|hd(?![a-z0-9])/.test(text)) return -500;
  if (/480p|576p|sd(?![a-z0-9])/.test(text)) return -1000;
  return 0;
}

/**
 * 健康码率区间奖励：单集 200MB~3.5GB 属于标准画质甜点区间
 */
export function getBitrateHealthScore(sizeBytes, epCount) {
  if (!epCount || epCount <= 0 || !sizeBytes || sizeBytes <= 0 || !Number.isFinite(sizeBytes)) return 0;
  const sizeMb = (sizeBytes / epCount) / (1024 * 1024);
  if (sizeMb >= 200 && sizeMb <= 3500) return 200;
  if (sizeMb < 80) return -600;
  if (sizeMb > 15000) return -150;
  return 0;
}

/**
 * 提取发布组名称，如 CHDWEB, ADWeb, FLUX
 */
export function extractReleaseGroup(title) {
  const m = /[-@]([A-Za-z0-9_]+)(?:\s*\[.*?\])?(?:\.mkv|\.mp4)?$/i.exec(String(title || '').trim());
  return m ? m[1].toLowerCase() : '';
}

/**
 * 综合计算候选资源的选取加权得分（清晰度、码率、做种、多候选净增益与发布组一致性）
 */
export function calculateCandidateScore(cand, hits, allCandEps, preferredGroup = null) {
  const t = cand?.torrent_info || {};
  const meta = cand?.meta_info || {};
  const title = String(t.title || '');
  const hitsCount = hits.length;
  const epCount = allCandEps && allCandEps.length ? allCandEps.length : 1;
  const redundantCount = Math.max(0, epCount - hitsCount);

  // 1. 命中集数净增益 (命中+350，冗余集扣120以避免为了1集下整季大包)
  const netGain = hitsCount * 350 - redundantCount * 120;

  // 2. 清晰度梯队分 (1080p: +1000, 2160p: +900, 720p: -500, SD: -1000)
  const resScore = getResolutionScore(t, meta);

  // 3. 单集健康码率分
  const sizeBytes = Number(t.size) || 0;
  const bitrateScore = getBitrateHealthScore(sizeBytes, epCount);

  // 4. 中文与字幕偏好
  const labelsText = JSON.stringify(t.labels || []) + title;
  const hasChinese = /中文|中字|简中|繁中|国语|chs|cht|简日|繁日|双语|baha|巴哈|简繁|内封|mikan/i.test(labelsText);
  const chineseScore = hasChinese ? 400 : 0;

  // 5. 做种数偏好 (做种>=5加200，并微量累加健康种)
  const seeds = Number(t.seeders) || 0;
  const seedsScore = seeds >= 5 ? (200 + Math.min(seeds, 50)) : (seeds * 20);

  // 6. 发布组连续性偏好 (后续分集尽量与已选中的首选发布组一致)
  const group = extractReleaseGroup(title);
  const groupScore = (preferredGroup && group && group === preferredGroup) ? 250 : 0;

  return netGain + resScore + bitrateScore + chineseScore + seedsScore + groupScore;
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

  // ── 预取 MoviePilot 实时活跃下载队列（用于防重与排队对齐） ──
  let activeDownloads = [];
  try {
    const rawActive = await mp.getActiveDownloads();
    if (Array.isArray(rawActive)) {
      activeDownloads = rawActive;
    } else if (rawActive && typeof rawActive === 'object') {
      activeDownloads = rawActive.data || rawActive.items || [];
    }
    logger.info(`📡 MoviePilot 活跃下载队列: 共有 ${activeDownloads.length} 个下载任务`);
  } catch (actErr) {
    logger.warn(`⚠️ 查询 MoviePilot 活跃下载队列跳过: ${actErr.message}`);
  }

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

      // ──【实时下载队列核对与动态去重】──
      const activeEpisodesInQueue = new Set();
      for (const item of activeDownloads) {
        const media = item?.media || {};
        const itTmdb = String(media.tmdbid || item.tmdbid || '').trim();
        const itSeasonStr = String(media.season || item.season_episode || '').trim();
        const sMatch = /\d+/.exec(itSeasonStr);
        const itSeason = sMatch ? parseInt(sMatch[0], 10) : null;

        let isMatch = false;
        if (tmdb && itTmdb && itTmdb === String(tmdb)) {
          isMatch = (itSeason === task.season);
        } else if (!tmdb || !itTmdb) {
          const itName = media.title || item.name || item.title || '';
          if (norm(itName) === norm(task.title)) {
            isMatch = itSeason != null ? (itSeason === task.season) : true;
          }
        }

        if (isMatch) {
          const epsFound = parseActiveEpisodes(media.episode, total);
          for (const ep of epsFound) activeEpisodesInQueue.add(ep);
        }
      }

      // 若活跃下载队列已完全覆盖所需缺失集
      if (needed && needed.length > 0 && needed.every(ep => activeEpisodesInQueue.has(ep))) {
        const coveredStr = formatRanges(needed);
        logger.info(`  📥 下载队列正在下载全部缺失集 (覆盖: ${coveredStr})，跳过重复提交`);
        session.updates.set(task, {
          state: '已提交下载，待复核',
          color: 'yellow',
          note: `下载队列正在下载缺集 (覆盖: ${coveredStr})`,
          repair: readRepairState(task.data) || undefined,
          at: new Date().toISOString(),
        });
        summary.existing++;
        continue;
      }

      // 若活跃下载队列覆盖了部分缺失集，剔除已在下载中的集数，仅需补齐剩余缺失
      if (needed && activeEpisodesInQueue.size > 0) {
        const overlap = needed.filter(ep => activeEpisodesInQueue.has(ep));
        if (overlap.length > 0) {
          const remainingNeeded = needed.filter(ep => !activeEpisodesInQueue.has(ep));
          logger.info(`  📥 下载队列已在下载集数: ${formatRanges(overlap)}，本次仅需补齐剩余: ${formatRanges(remainingNeeded)}`);
          task.needed = remainingNeeded;
          if (remainingNeeded.length === 0) {
            session.updates.set(task, {
              state: '已提交下载，待复核',
              color: 'yellow',
              note: `下载队列正在下载缺集 (覆盖: ${formatRanges(overlap)})`,
              repair: readRepairState(task.data) || undefined,
              at: new Date().toISOString(),
            });
            summary.existing++;
            continue;
          }
        }
      }

      // ── 检查本地持久化防重记录 ──
      const storeKey = `dl:${norm(task.name)}:${task.season}`;
      const records = store.get(storeKey, []);
      const nowTs = Date.now();
      const validAccepted = [];
      for (const r of records) {
        if (['accepted', 'existing'].includes(r.state)) {
          let isRecent = false;
          if (r.at) {
            const tRecord = new Date(r.at).getTime();
            if (nowTs - tRecord < 1800000) { // 30 分钟内
              isRecent = true;
            }
          }
          const rEps = r.episodes || [];
          if (isRecent || (rEps.length > 0 && rEps.some(ep => activeEpisodesInQueue.has(ep)))) {
            validAccepted.push(r);
          }
        }
      }

      if (task.needed && task.needed.every(n => validAccepted.some(r => r.episodes?.includes(n)))) {
        session.updates.set(task, {
          state: '已提交下载，待复核',
          color: 'yellow',
          note: '近期下载记录已覆盖缺集',
          records: validAccepted,
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
      const currentNeeded = task.needed || [];
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
        if (currentNeeded && currentNeeded.length > 0) {
          if (!isFull) {
            if (eps.length > 0) {
              const hitNeeded = eps.some(ep => currentNeeded.includes(ep));
              if (!hitNeeded) return false;
            } else {
              return false;
            }
          }
        }

        // 排除近期已成功提交过的相同种子
        const tKey = `${t.site_name || ''}:${t.title || ''}:${t.size || ''}`;
        if (validAccepted.some(r => r.key === tKey && r.state === 'accepted')) {
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

      // 区分整季包与散集
      const seasonPacks = eligible.filter(c => isSeasonPack(c.meta_info, c.torrent_info, task.season, total));
      const standaloneCandidates = eligible.filter(c => !isSeasonPack(c.meta_info, c.torrent_info, task.season, total));
      const selectedCandidates = []; // Array of { context, hits, isFull }

      // ── 整季包能效比决策 (Season Pack Yield Ratio) ──
      let preferStandalone = false;
      if (seasonPacks.length > 0 && currentNeeded.length > 0 && total > 0) {
        const missingCount = currentNeeded.length;
        const missingRatio = missingCount / total;
        if (missingCount <= 3 && missingRatio < 0.35) {
          const standaloneCovered = new Set();
          for (const sc of standaloneCandidates) {
            const scEps = sc.meta_info?.episode_list || [];
            for (const ep of scEps) standaloneCovered.add(ep);
          }
          if (currentNeeded.every(ep => standaloneCovered.has(ep))) {
            preferStandalone = true;
            logger.info(`  💡 缺失 ${missingCount} 集 (占比 ${(missingRatio * 100).toFixed(1)}%)，且散集可 100% 覆盖，优先采用散集精准补缺（避免下整季大包）`);
          }
        }
      }

      if (seasonPacks.length > 0 && !preferStandalone) {
        const allEps = Array.from({ length: total }, (_, idx) => idx + 1);
        let bestSp = seasonPacks[0];
        let bestScore = calculateCandidateScore(bestSp, allEps, allEps);
        for (let idx = 1; idx < seasonPacks.length; idx++) {
          const score = calculateCandidateScore(seasonPacks[idx], allEps, allEps);
          if (score > bestScore) {
            bestScore = score;
            bestSp = seasonPacks[idx];
          }
        }
        selectedCandidates.push({
          context: bestSp,
          hits: allEps,
          isFull: true,
        });
      } else {
        // 散集贪心多候选覆盖算法（含画质梯队、健康码率、冗余惩罚、发布组一致性与单季15个上限熔断）
        const remainingNeeded = new Set(currentNeeded);
        let preferredGroup = null;
        const maxSubmissions = 15;

        while (remainingNeeded.size > 0 && selectedCandidates.length < maxSubmissions) {
          const candidatesForRemaining = [];
          for (const c of eligible) {
            const eps = c.meta_info?.episode_list || [];
            const hits = eps.filter(ep => remainingNeeded.has(ep));
            if (hits.length > 0) {
              const score = calculateCandidateScore(c, hits, eps, preferredGroup);
              candidatesForRemaining.push({ context: c, hits, score });
            }
          }
          if (!candidatesForRemaining.length) break;

          candidatesForRemaining.sort((a, b) => b.score - a.score);
          const bestCand = candidatesForRemaining[0];

          selectedCandidates.push({
            context: bestCand.context,
            hits: bestCand.hits,
            isFull: false,
          });

          for (const ep of bestCand.hits) {
            remainingNeeded.delete(ep);
          }

          if (!preferredGroup) {
            preferredGroup = extractReleaseGroup(bestCand.context?.torrent_info?.title);
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

      for (let candIdx = 0; candIdx < selectedCandidates.length; candIdx++) {
        const item = selectedCandidates[candIdx];
        const { context: cand, hits, isFull } = item;
        const candT = cand.torrent_info;
        const candMeta = cand.meta_info || {};
        const title = String(candT.title || '');
        const site = String(candT.site_name || '');
        const seeds = Number(candT.seeders) || 0;
        const descType = isFull ? '整季' : `缺集 ${formatRanges(hits)}`;

        const tKey = `${site}:${title}:${candT.size || ''}`;
        logger.info(`  🎯 选定最佳资源 [${candIdx + 1}/${selectedCandidates.length}]: [${site}] ${title} (做种: ${seeds}, ${descType})`);

        // 请求节流：连续提交之间暂停 0.5s，保护 MoviePilot / 下载器客户端稳定
        if (candIdx > 0) {
          await new Promise(resolve => setTimeout(resolve, 500));
        }

        try {
          logger.info(`  🚀 正在向 MoviePilot 提交下载请求: ${title}...`);
          const reply = await mp.submitDownload(cand.media_info, candT);
          const isSuccess = !!reply?.success;
          const cCovered = isFull ? Array.from({ length: total }, (_, idx) => idx + 1) : (candMeta.episode_list || hits);
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
        let noteStr = '';
        if (submittedRecs.length === 1 && selectedCandidates[0]?.isFull) {
          const first = submittedRecs[0];
          noteStr = `[${first.site}] ${first.title} (做种 ${first.seeds}, 整季覆盖)`;
        } else if (submittedRecs.length === 1) {
          const first = submittedRecs[0];
          const covStr = formatRanges(Array.from(allCoveredEps));
          noteStr = `[${first.site}] ${first.title} (覆盖: ${covStr})`;
        } else {
          const covStr = formatRanges(Array.from(allCoveredEps));
          const uncovered = currentNeeded.filter(ep => !allCoveredEps.has(ep));
          if (uncovered.length > 0) {
            const uncovStr = formatRanges(uncovered);
            noteStr = `已提交 ${submittedRecs.length} 个资源补缺 (覆盖: ${covStr}，仍缺(无源): ${uncovStr})`;
          } else {
            noteStr = `已提交 ${submittedRecs.length} 个资源补缺 (完整覆盖缺集: ${covStr})`;
          }
        }

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
