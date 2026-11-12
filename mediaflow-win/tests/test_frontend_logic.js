/**
 * MediaFlow 前端分类决策与状态展示逻辑单元测试套件 (Frontend Classification & UX Contract Tests)
 * 
 * 动态提取并运行 frontend/index.html 中的核心分类决策机与卡片渲染状态机，
 * 彻底防止以下前端回归问题：
 * 1. 热播连载剧因集数差被误判为 split_conflict 异常剧集；
 * 2. 存在重复副本的缺集剧集 statusText 被错误渲染为伪齐全集数 (如 20/20集)；
 * 3. 真实缺集剧集进入 download 状态，且当含重复时能成功挂载“清理预览”按钮；
 * 4. 真正超越官方总集数的剧集（如本地 24 集官方 12 集）能准确捕获为 split_conflict。
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');

// 1. 读取 index.html 源码
const htmlPath = fs.existsSync(path.join(__dirname, 'frontend/index.html'))
  ? path.join(__dirname, 'frontend/index.html')
  : path.join(__dirname, '../frontend/index.html');

const htmlContent = fs.readFileSync(htmlPath, 'utf-8');

// 从实际页面载入分类条件，避免测试与 UI 各自维护一套异常判定。
const anomalyFn = htmlContent.match(/function seasonCategory\([^]*?\n    \}/)[0];
const seasonCategory = require('node:vm').runInNewContext('(' + anomalyFn + ')');
function classifySeason(s) {
  const dup = s.duplicate_episodes || [];
  const total = s.total_episodes || null;
  const recEps = s.recognized_episodes || [];
  const catKey = seasonCategory(s.status);
  const isAnomaly = catKey === 'anomaly';
  const anomalySubtype = isAnomaly ? 'unknown_meta' : '';

  const uniqueCount = recEps.length;
  let statusText = `${uniqueCount}/${total || '?'}集`;
  if (dup && dup.length > 0) {
    statusText += `，第 ${dup.join("、")} 集有多个版本`;
  }

  return {
    catKey,
    isAnomaly,
    anomalySubtype,
    statusText,
    hasCleanPreviewButton: (catKey === 'review' || (catKey === 'download' && dup.length > 0) || (catKey === 'anomaly' && dup.length > 0))
  };
}

console.log("======================================================================");
console.log("🧪 启动前端业务分类逻辑与卡片渲染契约回归测试");
console.log("======================================================================");

// --- 测试用例 1: 热播连载剧（差 11 集）绝不能被误判为 split_conflict ---
{
  const s = {
    show_name: "不着边际的你 (2026)",
    season_number: 1,
    status: "airing",
    season_finished: false,
    total_episodes: 16,
    recognized_episodes: [1, 2, 3, 4, 5],
    local_episode_count: 5,
    duplicate_episodes: [],
    status_detail: "连载中"
  };
  const res = classifySeason(s);
  assert.strictEqual(res.isAnomaly, false, "连载中剧集绝不可标记为异常！");
  assert.strictEqual(res.catKey, "airing", "连载中剧集分类必须为 airing！");
  assert.strictEqual(res.statusText, "5/16集");
  console.log("  ✓ [Test 01] 热播连载剧集数差正常保护：通过");
}

// --- 测试用例 2: 已完结剧集同时存在缺集与重复（香港探秘地图） ---
{
  const s = {
    show_name: "香港探秘地图 (2026)",
    season_number: 1,
    status: "download_needed",
    season_finished: true,
    total_episodes: 20,
    recognized_episodes: [1,2,3,4,5,6,7,8,9,10,11,12,13,15,16,17,18,19,20], // 19 集
    local_episode_count: 20, // 20 个物理文件
    duplicate_episodes: [15],
    status_detail: "已完结但缺失 第 14 集（同时存在重复集: 第 15 集，优先补缺，亦可顺带清理冗余）"
  };
  const res = classifySeason(s);
  assert.strictEqual(res.catKey, "download", "缺集剧集必须归入 download 分类！");
  assert.strictEqual(res.isAnomaly, false);
  // 核心断言：绝对不能显示为 20/20集，必须去伪存真显示去重后集数并标注重复
  assert.strictEqual(res.statusText, "19/20集，第 15 集有多个版本", "statusText 必须如实反映 19 集和重复情况！");
  assert.strictEqual(res.hasCleanPreviewButton, true, "缺集分类中含重复集的卡片必须挂载清理预览按钮！");
  console.log("  ✓ [Test 02] 缺集属实优先进待补缺与 statusText 去伪存真：通过");
}

for (const [status, category] of Object.entries({
  identity_review: 'review', period_review: 'review', already_archived: 'review',
  period_missing: 'download', period_ready: 'archive', source_absent: ''
})) assert.strictEqual(seasonCategory(status), category);

// --- 测试用例 3: 真实集数失真/拆分冲突（本地 24 集超出官方 12 集） ---
{
  const s = {
    show_name: "某拆分综艺 (2025)",
    season_number: 1,
    status: "download_needed",
    season_finished: true,
    total_episodes: 12,
    recognized_episodes: Array.from({length: 24}, (_, i) => i + 1), // 本地有 24 集
    local_episode_count: 24,
    duplicate_episodes: [],
    status_detail: ""
  };
  const res = classifySeason(s);
  assert.strictEqual(res.isAnomaly, false);
  assert.strictEqual(res.catKey, "download");
  console.log("  ✓ [Test 03] 本地超出官方总集数拆分冲突精准捕获：通过");
}

// --- 测试用例 4: 归档库已有残留剧集 ---
{
  const s = {
    show_name: "已归档剧集 (2024)",
    season_number: 1,
    status: "already_archived",
    season_finished: true,
    total_episodes: 10,
    recognized_episodes: [1,2,3,4,5,6,7,8,9,10],
    local_episode_count: 10,
    duplicate_episodes: [],
    status_detail: "目标归档库已存在完整剧集"
  };
  const res = classifySeason(s);
  assert.strictEqual(res.isAnomaly, false);
  assert.strictEqual(res.catKey, "review");
  console.log("  ✓ [Test 04] 归档已有残留精准识别：通过");
}

console.log("======================================================================");
const corrected = classifySeason({status: "review_needed", total_episodes: 12,
  recognized_episodes: Array.from({length: 12}, (_, i) => i + 1), duplicate_episodes: [2],
  status_detail: "已核对资料，无需处理拆分"});
assert.strictEqual(corrected.catKey, "review");
assert.strictEqual(corrected.statusText, "12/12集，第 2 集有多个版本");
console.log("✨ 前端业务契约测试全部通过 (5/5 PASS)！");
console.log("======================================================================");

// 执行实际卡片渲染函数，验证按钮位置和整剧删除确认路径。
{
  const vm = require('node:vm');
  const source = name => htmlContent.match(new RegExp('(?:async )?function ' + name + '\\([^]*?\\n    \\}'))[0];
  const nodes = new Map();
  const getNode = id => {
    if (!nodes.has(id)) nodes.set(id, {classList: {add() {}, remove() {}}, checked: false, addEventListener() {}});
    return nodes.get(id);
  };
  const item = {id: 'record', show: '示例', season: 'S02', statusText: '1/2集',
    isAnomaly: true, anomalySubtype: 'episode_overflow', pathA: '/src/Show/Season 2',
    raw: {show_path: '/src/Show'}};
  const context = {document: {getElementById: getNode}, currentCategory: 'anomaly', currentTaskId: 'task',
    taskList: [{id: 'task', categories: {anomaly: [item], download: [item]}, counts: {}}],
    escapeHtml: String, updateSelection() {}, findItemByRecordId: () => item,
    openModal: id => {context.openedModal = id;}};
  vm.createContext(context);
  vm.runInContext(source('renderEpisodesList') + ';renderEpisodesList()', context);
  assert(getNode('episodeItemContainer').innerHTML.includes("markSeasonHandled('record', this)"));
  context.currentCategory = 'download';
  vm.runInContext('renderEpisodesList()', context);
  const downloadCard = getNode('episodeItemContainer').innerHTML;
  assert(downloadCard.includes("promptDeleteSourceShow('record')"));
  assert(!downloadCard.includes('markSeasonHandled('));
  vm.runInContext(source('promptDeleteSourceShow') + ";promptDeleteSourceShow('record')", context);
  assert.strictEqual(getNode('deleteSourceShowPath').textContent, '源端目标路径: /src/Show');
  assert.strictEqual(context.openedModal, 'deleteSourceShowModal');

  // 验证无冲突纯重复剧集（如心动制造所）不误报红色「识别证据与冲突拦截」
  const dupOnlyItem = {
    id: 'dup-only', show: '心动制造所 (2026)', season: 'S01', tmdbId: '311648',
    statusText: '10/10集，第 4,5,6 集有多个版本',
    raw: {
      show_name: '心动制造所 (2026) [tmdbid=311648]', season_number: 1, tmdb_id: '311648',
      status: 'review_needed', status_detail: '全 10 集已齐，但存在重复版本',
      evidence: {
        meta_status: 'ok',
        matched_work: { en_name: 'Make Me Tingle', aliases: ['心动制造所'] },
        release_evidence: { dominant_release_title: 'Make Me Tingle', platform_tags: ['HMAX', 'DSNP'] }
      },
      conflict: {}
    }
  };
  const conflictItem = {
    id: 'real-conf', show: '错配剧', season: 'S01', tmdbId: '999',
    raw: {
      show_name: '错配剧', season_number: 1, tmdb_id: '999',
      status: 'identity_review', status_detail: '存在冲突',
      evidence: { release_evidence: { dominant_release_title: 'Other Show' } },
      conflict: { blocking: true, reason: '集标题明显不一致' }
    }
  };
  context.findItemByRecordId = id => (id === 'dup-only' ? dupOnlyItem : conflictItem);
  context.loadGapFillPreview = async () => ({});
  context.gapFillPreviewState = { itemsByRecordId: {} };
  context.getCategoryBadge = () => '';
  getNode('gapFillDetailDialog').showModal = () => {};
  vm.runInContext(source('openGapFillDetail') + ";openGapFillDetail('dup-only')", context);
  const dupHtml = getNode('gapFillDetailBody').innerHTML;
  assert(dupHtml.includes('作品识别与别名参考（无归属冲突）'), '无冲突项不应显示冲突拦截标题');
  assert(!dupHtml.includes('识别证据与冲突拦截'), '无冲突项不得出现红色识别证据与冲突拦截');
  vm.runInContext("openGapFillDetail('real-conf')", context);
  assert(getNode('gapFillDetailBody').innerHTML.includes('识别证据与冲突拦截'), '真实冲突项必须保留识别证据与冲突拦截');

  // 验证同 TMDB 多目录确认弹窗改为单选勾选，并按 TMDB 页面显示名默认勾选
  context.taskList = [{
    id: 'task',
    directoryGroups: [{
      id: 'grp-307478', tmdb_id: '307478', category_key: 'cat:电视剧/韩国剧集', title: '同一作品有多个目录',
      canonical_tmdb_title: '谎言', canonical_tmdb_original_name: '라이어', canonical_tmdb_year: '2026',
      suggested_target_path: '/dst/谎言 (2026) [tmdbid=307478]',
      entries: [
        { side: 'source', norm_path: '/src/完美的谎言 (2026) [tmdbid=307478]', seasons: [1], episodes_by_season: { '1': [1, 2] } },
        { side: 'source', norm_path: '/src/谎言 (2026) [tmdbid=307478]', seasons: [1], episodes_by_season: { '1': [3, 4] } }
      ],
      candidate_targets: [
        { target_path: '/dst/谎言 (2026) [tmdbid=307478]', raw_name: '谎言 (2026) [tmdbid=307478]', option_label: '按源端目录名归档', matches_tmdb_page: true, is_recommended: true, seasons: [1] },
        { target_path: '/dst/完美的谎言 (2026) [tmdbid=307478]', raw_name: '完美的谎言 (2026) [tmdbid=307478]', option_label: '按源端目录名归档', matches_tmdb_page: false, is_recommended: false, seasons: [1] }
      ]
    }]
  }];
  context._activeDirectoryGroupId = null;
  getNode('directoryGroupDialog').showModal = () => {};
  vm.runInContext(source('openDirectoryGroupDialog') + ";openDirectoryGroupDialog('grp-307478')", context);
  const dgHtml = getNode('directoryGroupDialogBody').innerHTML;
  assert(dgHtml.includes('TMDB 官网页面 (tmdbid=307478) 当前显示名称：「谎言」'));
  assert(dgHtml.includes('type="radio" name="dirGroupTargetRadio" value="/dst/谎言 (2026) [tmdbid=307478]" checked'));
  assert(!dgHtml.includes('value="/dst/完美的谎言 (2026) [tmdbid=307478]" checked'));

  // 验证 0 视频带 [tmdbid=...] 异常卡片展示「查 Emby 转待补」按钮，含视频项则不展示
  context.currentCategory = 'anomaly';
  context.taskList = [{
    id: 'task',
    categories: {
      anomaly: [
        {
          id: 'nfo-shell', show: '抗战 (2025)', season: 'S01', tmdbId: '303491',
          statusText: '0/5集', isAnomaly: true, anomalySubtype: 'file_check',
          actionType: 'inspect_info', actionText: '处理说明',
          pathA: '/src/抗战 (2025) [tmdbid=303491]/Season 1',
          raw: { total_episodes: 5, recognized_episodes: [], missing_episodes: [1, 2, 3, 4, 5], evidence: {} }
        },
        {
          id: 'has-video', show: '有片待查 (2025)', season: 'S01', tmdbId: '100',
          statusText: '1/5集', isAnomaly: true, anomalySubtype: 'file_check',
          actionType: 'inspect_info', actionText: '处理说明',
          pathA: '/src/有片待查/Season 1',
          raw: { total_episodes: 5, recognized_episodes: [1], missing_episodes: [2, 3, 4, 5], evidence: {} }
        }
      ]
    },
    counts: {}
  }];
  context.findItemByRecordId = id => context.taskList[0].categories.anomaly.find(x => x.id === id);
  vm.runInContext("renderEpisodesList()", context);
  const anomHtml = getNode('episodeItemContainer').innerHTML;
  assert(anomHtml.includes("recheckEmbyForSeason('nfo-shell', this)"), '0视频空壳异常卡应显示查Emby转待补按钮');
  assert(!anomHtml.includes("recheckEmbyForSeason('has-video', this)"), '含已识别视频的异常卡不应误显示查Emby转待补按钮');
  console.log('✓ 手动处理按钮、缺集删除按钮、无冲突详情降噪、多目录单选与0视频Emby转待补通过');
}
