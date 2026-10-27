/**
 * MoviePilot v2.15.6 整理修复引擎
 * 移植自原仓库 transfer-repair.js
 *
 * 专门解决 MoviePilot 下载完成后，由于识别错误、转移失败等原因导致的整理历史阻塞问题。
 * 原则：
 * 1. 仅对明确 TMDB ID 与已确认缺集的项进行单集修复
 * 2. 仅删除指定的单条失败历史记录（deletesrc=false, deletedest=false 绝不删除源文件或目标文件）
 * 3. 提交前进行 preview 预检，确保目标路径与季号精确对应
 * 4. 再次执行 manualTransfer 重新触发整理
 */
import { episodeFromName, MEDIA_REGEX } from './episode-parser.js';
import { isRepairPending, isRepairUncertain } from './repair-state.js';

const parent = p => p.slice(0, p.lastIndexOf('/')) || '/';
const valid = p => typeof p === 'string' && p.startsWith('/') && !/[\\\x00-\x1f]/.test(p) && !p.includes('//') && !p.split('/').some(s => s === '.' || s === '..');
const within = (p, root) => valid(p) && valid(root) && p.startsWith(root.replace(/\/$/, '') + '/');

/**
 * 校验历史记录条目身份是否与当前任务匹配
 */
export function matchHistoryIdentity(task, h) {
  if (!h || !task.tmdb || !Array.isArray(task.needed)) return false;
  return String(h.tmdbid) === String(task.tmdb) &&
    (h.type === '电视剧' || h.type === '动漫') &&
    /^S\d+$/i.test(h.seasons || '') &&
    Number(h.seasons.slice(1)) === task.season &&
    /^E\d+$/i.test(h.episodes || '') &&
    task.needed.includes(Number(h.episodes.slice(1)));
}

/**
 * 构建手动转移请求参数
 */
export function buildTransferRequest(task, h, srcItem, targetRoot) {
  const ep = Number(h.episodes.slice(1));
  const category = task.category || '';
  const name = task.name || task.title;
  const isAnime = /动漫/.test(category);
  const targetDir = `${targetRoot}/${category}/${name}/Season ${task.season}`;

  return {
    fileitem: {
      storage: srcItem.storage || 'local',
      path: srcItem.path,
      type: 'file',
      name: srcItem.name || srcItem.path.split('/').pop(),
      size: srcItem.size,
      modify_time: srcItem.modify_time,
    },
    target_storage: 'local',
    target_path: targetDir,
    media_info: {
      type: isAnime ? '动漫' : '电视剧',
      tmdb_id: Number(task.tmdb),
      title: task.title,
      year: task.year || '',
      category,
    },
    meta_info: {
      begin_season: task.season,
      end_season: task.season,
      season_list: [task.season],
      begin_episode: ep,
      end_episode: ep,
      episode_list: [ep],
    },
    transfer_type: 'copy',
    overwrite_mode: 'never',
  };
}

/**
 * 检查并生成修复计划（只读）
 * @param {object} task - 任务对象
 * @param {object} mp - MoviePilotClient 实例
 * @param {object} config - { source: '下载目录', target: '媒体库目录' }
 * @param {function} [checkpoint]
 * @returns {Promise<object>} 修复计划
 */
export async function checkRepair(task, mp, config, checkpoint = () => {}) {
  if (!task.tmdb || !task.needed?.length) {
    throw new Error('修复需要明确的 TMDB ID 与缺失集号');
  }

  checkpoint();
  // 1. 校验版本
  try {
    const globalInfo = await mp.getSystemGlobal();
    const ver = globalInfo?.data?.BACKEND_VERSION;
    if (ver && !ver.startsWith('v2.15') && !ver.startsWith('v2.16')) {
      // 仅记录警告，不强阻
    }
  } catch {
    // 忽略版本探测错误
  }

  checkpoint();
  // 2. 查询该剧在 MoviePilot 中的转移历史
  const historyData = await mp.getTransferHistories(task.title, 1, 100);
  const histories = Array.isArray(historyData?.data?.list) ? historyData.data.list : (Array.isArray(historyData) ? historyData : []);

  const matches = histories.filter(h => matchHistoryIdentity(task, h));
  const items = [];
  const notes = [];

  for (const ep of task.needed) {
    checkpoint();
    const epMatches = matches.filter(h => Number(h.episodes.slice(1)) === ep);
    if (!epMatches.length) {
      notes.push(`E${ep}: 未找到匹配的历史记录`);
      continue;
    }
    if (epMatches.length > 1) {
      notes.push(`E${ep}: 存在多条冲突的历史记录，需人工核实`);
      continue;
    }

    const h = epMatches[0];
    const srcPath = h.src || h.src_fileitem?.path;
    if (!srcPath || !within(srcPath, config.source) || !MEDIA_REGEX.test(srcPath)) {
      notes.push(`E${ep}: 历史源文件不在指定下载目录下或非视频`);
      continue;
    }

    // 检查源文件是否存在
    const srcDir = parent(srcPath);
    let srcFiles;
    try {
      srcFiles = await mp.listStorage(srcDir);
    } catch (e) {
      notes.push(`E${ep}: 无法读取源目录: ${e.message}`);
      continue;
    }

    const srcFile = srcFiles.find(f => f.path === srcPath);
    if (!srcFile || srcFile.size <= 0) {
      notes.push(`E${ep}: 源文件不存在或大小为零`);
      continue;
    }

    const req = buildTransferRequest(task, h, srcFile, config.target);

    // 预检
    try {
      const previewRes = await mp.previewTransfer(req);
      const pData = previewRes?.data;
      if (previewRes?.success !== true || pData?.summary?.failed > 0) {
        notes.push(`E${ep}: 预检提示转移不可行 (${previewRes?.message || '未知'})`);
        continue;
      }

      items.push({
        id: h.id,
        episode: ep,
        source: srcPath,
        target: pData?.items?.[0]?.target || '',
        history: h,
        sourceFile: srcFile,
        request: req,
        state: 'ready',
        steps: [],
        note: '已预检通过，等待确认执行',
      });
    } catch (e) {
      notes.push(`E${ep}: 预检异常: ${e.message}`);
    }
  }

  return {
    schema: 1,
    taskKey: task.key,
    state: items.length ? 'ready' : 'blocked',
    at: new Date().toISOString(),
    episodes: items.length ? items.map(i => i.episode) : task.needed,
    items,
    note: notes.join('；') || '已生成修复计划',
    checkedAt: new Date().toISOString(),
  };
}

/**
 * 执行修复计划（解除历史后重转）
 * @param {object} task
 * @param {object} plan - checkRepair 生成的计划
 * @param {object} mp - MoviePilotClient
 * @param {object} config
 * @param {object} options - { save: (record) => void, checkpoint: () => void }
 * @returns {Promise<object>} 更新后的执行记录
 */
export async function executeRepair(task, plan, mp, config, { save = () => {}, checkpoint = () => {} } = {}) {
  if (plan.taskKey !== task.key || plan.state !== 'ready' || !plan.items.length || isRepairUncertain(plan)) {
    throw new Error('修复计划未就绪或存在未决状态');
  }

  const record = structuredClone(plan);
  record.at = new Date().toISOString();
  const persist = () => {
    record.at = new Date().toISOString();
    save(record);
  };

  for (const item of record.items) {
    checkpoint();
    // 1. 删除指定的转移历史记录（固定不删文件）
    item.state = record.state = 'history-delete-pending';
    item.steps.push({ action: '解除历史', state: 'pending', at: new Date().toISOString(), historyId: item.id });
    persist();

    try {
      const delReply = await mp.deleteTransferHistory(item.id);
      if (delReply?.success === false) {
        throw new Error(`删除历史失败: ${delReply.message || '未知原因'}`);
      }
      item.steps.at(-1).state = 'verified';
      item.steps.at(-1).finishedAt = new Date().toISOString();
      item.state = record.state = 'history-deleted';
      persist();

      checkpoint();
      // 2. 重新提交手动转移
      item.state = record.state = 'transfer-pending';
      item.steps.push({ action: '重转', state: 'pending', at: new Date().toISOString() });
      persist();

      const transferReply = await mp.executeManualTransfer(item.request);
      if (transferReply?.success === false) {
        throw new Error(`重转提交失败: ${transferReply.message || '未知原因'}`);
      }

      item.steps.at(-1).state = 'submitted';
      item.steps.at(-1).finishedAt = new Date().toISOString();
      item.state = 'submitted';
      persist();
    } catch (e) {
      item.note = e.message;
      item.state = record.state = 'uncertain';
      if (item.steps.length) item.steps.at(-1).error = e.message;
      persist();
      throw e;
    }
  }

  record.state = 'submitted';
  record.note = '重转请求已发出，请等待 MoviePilot 写入并由复核阶段核对文件';
  persist();
  return record;
}
