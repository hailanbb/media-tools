/**
 * 阶段一：剧集初检
 * 扫描 CloudDrive2 源目录，逐剧逐季核对本地集号与 TMDB 登记集数，生成报告
 *
 * 支持智能识别与多级路径过滤：
 * 1. 默认：扫描 config.paths.source 根目录下的全部可用分类与剧集
 * 2. 指定分类：通过 options.category 仅扫描特定分类（如 "华语剧"）
 * 3. 关键字过滤：通过 options.show 仅匹配特定剧名（如 "凡人修仙传"）
 * 4. 指定精确路径：通过 options.path 可直接指定网盘任意分类目录或单部剧目录，程序自适应识别
 */
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { TMDBClient } from '../clients/tmdb.js';
import { showInfo, seasonNumber } from '../core/title-matcher.js';
import { episodeFromName, MEDIA_REGEX, range, uniq } from '../core/episode-parser.js';
import { writeReport } from '../core/report-adapter.js';
import { join, resolve } from 'node:path';
import { PROJECT_ROOT } from '../config.js';

/**
 * 计算分类相对于源根目录的相对路径 (如 "动漫/日韩动漫" 或 "电视剧/国产剧")
 * @param {string} dirPath
 * @param {string} sourceRoot
 * @returns {string}
 */
function computeCategoryRelPath(dirPath, sourceRoot) {
  const normDir = dirPath.replace(/\/+$/, '');
  const normSrc = (sourceRoot || '').replace(/\/+$/, '');
  if (normSrc && normDir.startsWith(normSrc)) {
    const rel = normDir.slice(normSrc.length).replace(/^\/+/, '');
    if (rel) return rel;
  }
  return normDir.split('/').pop() || '未分类';
}

/**
 * 执行初检阶段
 * @param {object} config - 完整配置
 * @param {object} logger
 * @param {object} [options] - 过滤与路径选项: { path?: string, category?: string, show?: string }
 * @returns {Promise<object>} 执行摘要
 */
export async function runInspect(config, logger, options = {}) {
  const cd2 = new CloudDriveClient({
    origin: config.clouddrive2.origin,
    token: config.clouddrive2.token,
    logger,
  });
  const tmdb = new TMDBClient({
    apiKey: config.tmdb?.apiKey || '',
    language: config.tmdb?.language || 'zh-CN',
    logger,
  });

  const origin = config.clouddrive2.origin;
  const rootSource = (config.paths.source || '/115/Library_incs/nas').replace(/\/+$/, '');
  const basePath = (options.path || rootSource).replace(/\/+$/, '');

  // 1. 根据规则禁用全库初检：未指定任何检测范围且指向根目录时阻断
  if (basePath === rootSource && !options.category && !options.show) {
    throw new Error('已根据要求禁用全库初检。请使用交互式选剧初检 (node bin/tv-workflow.js -i)，或通过 --category / --show / --path 指定检测范围。');
  }

  const results = [];
  const summary = { total: 0, green: 0, blue: 0, yellow: 0, red: 0, noFill: 0, errors: [] };

  logger.info(`📂 正在连接 CloudDrive2，读取目标目录: ${basePath}`);

  let rootEntries;
  try {
    rootEntries = await cd2.listFiles(basePath);
  } catch (e) {
    throw new Error(`无法读取目标路径 ${basePath}: ${e.message}`);
  }

  // ── 智能识别目录层级 ──────────────────────────────────────────────────
  // 结构: Map<分类名, Array<{ name: string, path: string, directory: boolean, preloadedContents?: Array }>>
  const categoryMap = new Map();

  // 1. 判断是否直接是“单部剧集目录”（自身直接包含 Season 1、Season 2 等）
  const isSingleShow = rootEntries.some(f => f.directory && seasonNumber(f.name) !== null);

  if (isSingleShow) {
    const showName = basePath.split('/').pop();
    const parentPath = basePath.slice(0, basePath.lastIndexOf('/'));
    const catName = computeCategoryRelPath(parentPath, rootSource);
    logger.info(`🎯 目标识别为【单部剧集】: [${catName}] ${showName}`);
    categoryMap.set(catName, [{
      name: showName,
      path: basePath,
      directory: true,
      preloadedContents: rootEntries,
    }]);
  } else {
    // 递归收集分类目录（支持 /nas -> 动漫/电视剧 -> 日韩动漫/国产剧 -> 剧集 多级结构）
    async function collectCategories(dirPath, entries) {
      const dirs = entries.filter(f => f.directory);
      if (!dirs.length) return [];

      // 采样第一个子目录判断其是否是具体剧集目录（包含 Season 季目录或直接媒体文件）
      let isShowDir = false;
      try {
        const sampleSub = await cd2.listFiles(dirs[0].path);
        isShowDir = sampleSub.some(f => (f.directory && seasonNumber(f.name) !== null) || MEDIA_REGEX.test(f.name));
      } catch {}

      if (isShowDir) {
        // 当前 dirPath 为分类目录，dirs 即为该分类下的剧集列表
        const catRel = computeCategoryRelPath(dirPath, rootSource);
        return [{ catRel, path: dirPath, shows: dirs }];
      }

      // 否则当前 dirPath 是上层目录（媒体类型或根目录），继续向下层分类递归
      const list = [];
      for (const d of dirs) {
        try {
          const subEntries = await cd2.listFiles(d.path);
          const subCats = await collectCategories(d.path, subEntries);
          list.push(...subCats);
        } catch (e) {
          logger.warn(`  无法读取子目录 ${d.name}: ${e.message}`);
        }
      }
      return list;
    }

    const collected = await collectCategories(basePath, rootEntries);
    for (const item of collected) {
      if (options.category) {
        const catFilter = options.category.toLowerCase();
        if (!item.catRel.toLowerCase().includes(catFilter)) {
          continue;
        }
      }
      categoryMap.set(item.catRel, item.shows);
    }
  }

  // ── 剧名/分类关键字过滤 ────────────────────────────────────────────────
  if (options.show) {
    const kw = options.show.toLowerCase();
    for (const [catName, shows] of categoryMap) {
      // 若分类名本身直接命中关键字（如用户输入“香港剧集”），保留该分类下全部剧集
      if (catName.toLowerCase().includes(kw)) {
        continue;
      }
      const filtered = shows.filter(s => s.name.toLowerCase().includes(kw));
      if (filtered.length) {
        categoryMap.set(catName, filtered);
      } else {
        categoryMap.delete(catName);
      }
    }
    const matchCount = [...categoryMap.values()].reduce((sum, s) => sum + s.length, 0);
    logger.info(`🔍 过滤 [${options.show}]：共匹配到 ${matchCount} 部剧`);
  }

  const totalShows = [...categoryMap.values()].reduce((sum, s) => sum + s.length, 0);
  if (totalShows === 0) {
    logger.warn('⚠️ 未在指定范围内发现任何符合条件的剧集目录');
    return { ...summary, outputPath: null };
  }

  // ── 逐分类逐剧初检 ────────────────────────────────────────────────────
  let processed = 0;

  for (const [catName, shows] of categoryMap) {
    const sortedShows = [...shows].sort((a, b) => (a.modified || '').localeCompare(b.modified || ''));

    for (const show of sortedShows) {
      processed++;
      const info = showInfo(show.name);
      logger.progress('初检', processed, totalShows, `${catName} / ${show.name}`);

      // 列出季目录
      let showContents;
      if (show.preloadedContents) {
        showContents = show.preloadedContents;
      } else {
        try {
          showContents = await cd2.listFiles(show.path);
        } catch (e) {
          summary.errors.push({ show: show.name, error: e.message });
          continue;
        }
      }

      const seasonDirs = showContents
        .filter(f => f.directory && seasonNumber(f.name) !== null)
        .sort((a, b) => seasonNumber(a.name) - seasonNumber(b.name));

      if (!seasonDirs.length) {
        results.push({
          category: catName, name: show.name, season: null,
          path: show.path, showPath: show.path,
          link: `${origin}/?page=files&path=${encodeURIComponent(show.path)}`,
          data: { '分类': catName, '剧集目录': show.name, '季数': '', '处理结果': '无季目录' },
          color: 'other',
        });
        continue;
      }

      // 查询 TMDB（如果有 ID）
      if (info.tmdb && tmdb.apiKey) {
        try {
          await tmdb.getShowDetails(info.tmdb);
        } catch (e) {
          logger.warn(`  TMDB 查询失败: ${e.message}`);
        }
      }

      for (const sDir of seasonDirs) {
        const sNum = seasonNumber(sDir.name);
        summary.total++;

        let files;
        try {
          files = await cd2.listFiles(sDir.path);
        } catch (e) {
          summary.errors.push({ show: show.name, season: sNum, error: e.message });
          continue;
        }

        const videos = files.filter(f => !f.directory && MEDIA_REGEX.test(f.name));
        const episodes = videos
          .map(f => episodeFromName(f.name, sNum))
          .filter(n => n !== null);
        const identified = uniq(episodes);
        const duplicates = episodes.filter((n, idx) => episodes.indexOf(n) !== idx);
        const unknown = videos.filter(f => episodeFromName(f.name, sNum) === null).map(f => f.name);

        // 查询 TMDB 登记集数与播出状态
        let tmdbTotal = null, broadcastStatus = '未确认';
        if (info.tmdb && tmdb.apiKey) {
          try {
            const seasonDetail = await tmdb.getSeasonDetails(info.tmdb, sNum);
            if (seasonDetail?.episodes?.length) {
              tmdbTotal = seasonDetail.episodes.length;
              const lastEp = seasonDetail.episodes[seasonDetail.episodes.length - 1];
              const airDate = lastEp?.air_date ? new Date(lastEp.air_date) : null;
              if (airDate && airDate < new Date()) broadcastStatus = '已播完';
              else if (airDate) broadcastStatus = '连载中';
              else broadcastStatus = '未确认';
            }
          } catch {
            // TMDB 接口查询异常时跳过
          }
        }

        const total = tmdbTotal || identified.length || 0;
        const missing = tmdbTotal ? range(tmdbTotal).filter(n => !identified.includes(n)) : [];
        const isComplete = tmdbTotal && missing.length === 0;

        let color = 'other';
        if (isComplete && broadcastStatus === '已播完') {
          color = 'green'; summary.green++;
        } else if (broadcastStatus === '连载中') {
          color = 'blue'; summary.blue++;
        } else if (videos.length === 0) {
          color = 'red'; summary.red++;
        } else if (missing.length > 0 || unknown.length > 0 || uniq(duplicates).length > 0) {
          color = 'yellow'; summary.yellow++;
        } else {
          summary.noFill++;
        }

        const compact = arr => arr.length ? arr.join('、') : '';

        results.push({
          category: catName, name: show.name, season: sNum,
          path: sDir.path, showPath: show.path,
          link: `${origin}/?page=files&path=${encodeURIComponent(sDir.path)}`,
          data: {
            '分类': catName,
            '剧集目录': show.name,
            '季数': sNum,
            '源云存储链接': `${origin}/?page=files&path=${encodeURIComponent(sDir.path)}`,
            '视频文件数': videos.length,
            '已识别集数': identified.length,
            '来源登记集数': tmdbTotal || '',
            '已识别集号': compact(identified),
            '重复集号': compact(uniq(duplicates)),
            '缺失集号': tmdbTotal ? compact(missing) : '未确认',
            '无法识别集号的文件': compact(unknown),
            'TMDB ID': info.tmdb || '',
            '数据来源': tmdbTotal ? 'tmdb' : '',
            '播出状态': broadcastStatus,
            '处理结果': isComplete && broadcastStatus === '已播完' ? '齐全'
              : missing.length ? `缺 ${missing.length} 集` : '待核实',
            '待播集号': '',
            '播期未知集号': '',
            '报告版本': 'tv-workflow/1',
          },
          color,
        });

        logger.debug(`  Season ${sNum}: ${identified.length}/${total || '?'} 集, 标记: ${color}`);
      }
    }
  }

  // 写入初检报告
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const tag = options.show ? `_${options.show}` : options.category ? `_${options.category}` : '';
  const reportsDir = resolve(PROJECT_ROOT, 'reports');
  const outputPath = join(reportsDir, `初检报告${tag}_${timestamp}.xlsx`);
  await writeReport({ tasks: results, mode: 'inspect' }, outputPath);

  logger.info(`\n📊 初检完成: 共核对 ${summary.total} 季`);
  logger.info(`   🟢 齐全 ${summary.green}  🔵 连载 ${summary.blue}  🟡 待处理 ${summary.yellow}  🔴 空 ${summary.red}  ⬜ 其他 ${summary.noFill}`);
  logger.info(`   📄 报告: ${outputPath}`);

  return { ...summary, outputPath };
}
