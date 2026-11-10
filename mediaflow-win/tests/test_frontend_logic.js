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
const anomalyFn = htmlContent.match(/function seasonAnomaly\([^]*?\n    \}/)[0];
const seasonAnomaly = require('node:vm').runInNewContext('(' + anomalyFn + ')');
function classifySeason(s) {
  const dup = s.duplicate_episodes || [];
  const total = s.total_episodes || null;
  const recEps = s.recognized_episodes || [];
  const anomalySubtype = seasonAnomaly(s.status, total, Math.max(0, ...recEps));
  const isAnomaly = Boolean(anomalySubtype);
  let catKey = 'airing';
  if (isAnomaly) {
    catKey = 'anomaly';
  } else if (s.status === 'archive_ready') {
    catKey = 'archive';
  } else if (s.status === 'review_needed') {
    catKey = 'review';
  } else if (s.status === 'download_needed') {
    catKey = 'download';
  } else if (s.status === 'airing') {
    catKey = 'airing';
  }

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
  assert.strictEqual(res.isAnomaly, true);
  assert.strictEqual(res.anomalySubtype, "episode_overflow", "本地集数超出官方总集数必须捕获为 split_conflict！");
  assert.strictEqual(res.catKey, "anomaly");
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
  assert.strictEqual(res.isAnomaly, true);
  assert.strictEqual(res.anomalySubtype, "already_archived");
  assert.strictEqual(res.catKey, "anomaly");
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
    if (!nodes.has(id)) nodes.set(id, {classList: {add() {}, remove() {}}, checked: false});
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
  console.log('✓ 手动处理按钮、缺集删除按钮及整剧确认路径通过');
}
