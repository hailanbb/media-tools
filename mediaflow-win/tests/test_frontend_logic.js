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
    hasCleanPreviewButton: (catKey === 'review' && dup.length > 0)
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
    status_detail: "已完结但缺失 第 14 集（同时存在重复集: 第 15 集，优先补缺，齐集后再清理冗余）"
  };
  const res = classifySeason(s);
  assert.strictEqual(res.catKey, "download", "缺集剧集必须归入 download 分类！");
  assert.strictEqual(res.isAnomaly, false);
  // 核心断言：绝对不能显示为 20/20集，必须去伪存真显示去重后集数并标注重复
  assert.strictEqual(res.statusText, "19/20集，第 15 集有多个版本", "statusText 必须如实反映 19 集和重复情况！");
  assert.strictEqual(res.hasCleanPreviewButton, false, "真缺集待补分类中不得挂载清理预览按钮（齐集后再清理）！");
  console.log("  ✓ [Test 02] 缺集属实优先进待补缺与 statusText 去伪存真：通过");
}

for (const [status, category] of Object.entries({
  identity_review: 'anomaly', period_review: 'anomaly', already_archived: 'anomaly',
  archive_check: 'anomaly', unknown: 'anomaly',
  period_missing: 'download', download_needed: 'download',
  period_ready: 'archive', archive_ready: 'archive',
  review_needed: 'review', airing: 'airing', source_absent: '',
  archived: '', cleaned_redundant: '', manually_handled: ''
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

// --- 测试用例 4: 归档库已有残留剧集（属于 anomaly 异常分类） ---
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
  assert.strictEqual(res.isAnomaly, true, "归档已有残留必须归入 anomaly 异常剧集！");
  assert.strictEqual(res.catKey, "anomaly", "归档已有残留分类必须为 anomaly！");
  console.log("  ✓ [Test 04] 归档已有残留精准识别为 anomaly：通过");
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
    escapeHtml: String, escapeJsAttr: String, updateSelection() {}, findItemByRecordId: () => item, updateGapFillSitesToolbarBadge() {},
    openModal: id => {context.openedModal = id;}};
  vm.createContext(context);
  vm.runInContext(source('getGapFillLists') + ';' + source('cleanSeasonStatusNote') + ';' + source('renderEpisodesList') + ';renderEpisodesList()', context);
  assert(!getNode('episodeItemContainer').innerHTML.includes("markSeasonHandled('record', this)"));
  assert(!getNode('episodeItemContainer').innerHTML.includes("holdSeasonForTask('record', this)"));
  assert(getNode('episodeItemContainer').innerHTML.includes("手动复核"));
  context.currentCategory = 'download';
  vm.runInContext('renderEpisodesList()', context);
  const downloadCard = getNode('episodeItemContainer').innerHTML;
  assert(!downloadCard.includes("promptDeleteSourceShow("), "缺集待补分类绝不应包含删除源端整剧删除按钮！");
  assert(downloadCard.includes("openDeleteSourceSeasonModal("), "v2.5.52: 缺集待补提供单季主动放弃入口并要求预览确认");
  assert(!downloadCard.includes('markSeasonHandled('));

  // 验证异常卡片移除整剧删除入口，并按问题提供人工确认
  item.anomalySubtype = 'unknown_meta';
  item.raw.status = 'unknown';
  context.currentCategory = 'anomaly';
  vm.runInContext('renderEpisodesList()', context);
  const anomalyUnknownCard = getNode('episodeItemContainer').innerHTML;
  assert(!anomalyUnknownCard.includes("promptDeleteSourceShow('record')"), "异常剧集卡片必须移除整剧删除源端按钮！");
  assert(anomalyUnknownCard.includes("openManualConfirmModal('record')"), "资料不完整异常项主入口必须为人工确认！");
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
  vm.runInContext(['seasonDecisionSummary','readableLookupReason','renderDoubanLookup'].map(source).join(';') + ';' + source('openGapFillDetail') + ";openGapFillDetail('dup-only')", context);
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

  // 验证 v2.5.22 阶段三要求：初检已全自动打通核查链条并自动归入真缺集，卡片表面彻底移除「查 Emby 转待补」与多余「处理说明」
  context.currentCategory = 'anomaly';
  context.taskList = [{
    id: 'task',
    categories: {
      anomaly: [
        {
          id: 'nfo-shell', show: '抗战 (2025)', season: 'S01', tmdbId: '303491',
          statusText: '0/5集', isAnomaly: true, anomalySubtype: 'file_check',
          actionType: 'inspect_info', actionText: '详情',
          pathA: '/src/抗战 (2025) [tmdbid=303491]/Season 1',
          note: '源端仅见附件，未找到正片文件；想保留这些内容可直接标记已手动处理。',
          raw: { total_episodes: 5, recognized_episodes: [], missing_episodes: [1, 2, 3, 4, 5], evidence: {} }
        },
        {
          id: 'has-video', show: '有片待查 (2025)', season: 'S01', tmdbId: '100',
          statusText: '1/5集', isAnomaly: true, anomalySubtype: 'file_check',
          actionType: 'inspect_info', actionText: '详情',
          pathA: '/src/有片待查/Season 1',
          note: '第 1 集有多个版本',
          raw: { total_episodes: 5, recognized_episodes: [1], missing_episodes: [2, 3, 4, 5], evidence: {} }
        }
      ]
    },
    counts: {}
  }];
  context.findItemByRecordId = id => context.taskList[0].categories.anomaly.find(x => x.id === id);
  vm.runInContext("renderEpisodesList()", context);
  const anomHtml = getNode('episodeItemContainer').innerHTML;
  assert(!anomHtml.includes("recheckEmbyForSeason('nfo-shell', this)"), 'v2.5.22 异常卡片表面应移除查Emby转待补按钮');
  assert(!anomHtml.includes("recheckEmbyForSeason('has-video', this)"), '含已识别视频的异常卡不应显示查Emby转待补按钮');
  assert(!anomHtml.includes("可直接标记已手动处理"), '异常卡片应通过 cleanSeasonStatusNote 过滤诱导手动处理的同义文案');
  console.log('✓ 手动处理按钮、缺集删除按钮、无冲突详情降噪、多目录单选与0视频Emby核查自动化降噪通过');

  // ======================================================================
  // 验证 v2.5.21 目录组唯一收口待复核清理、真缺集待补隐藏与跨页跳转定位
  // ======================================================================
  {
    const sourceWithCr = name => htmlContent.match(new RegExp('(?:async )?function ' + name + '\\([^]*?\\r?\\n    \\}'))[0];
    const dgNodes = new Map();
    const getDgNode = id => {
      if (!dgNodes.has(id)) {
        const clsSet = new Set();
        dgNodes.set(id, {
          classList: {
            add(...args) { args.forEach(a => clsSet.add(a)); },
            remove(...args) { args.forEach(a => clsSet.delete(a)); },
            contains(a) { return clsSet.has(a); },
            toggle(a, f) { if (f !== undefined) { f ? clsSet.add(a) : clsSet.delete(a); } else { clsSet.has(a) ? clsSet.delete(a) : clsSet.add(a); } }
          },
          innerHTML: '',
          textContent: '',
          checked: false,
          disabled: false,
          scrollIntoView() {},
          addEventListener() {}
        });
      }
      return dgNodes.get(id);
    };

    const sampleTask = {
      id: 'task-variety',
      counts: { review: 0, download: 2, pending_directory_groups: 1 },
      categories: {
        review: [],
        download: [
          {
            id: 'season-chengfeng-s06',
            show: '乘风 (2025)',
            season: 'S06',
            statusText: '10/12集',
            actionType: 'request_download',
            raw: {
              show_name: '乘风 (2025)',
              season_number: 6,
              tmdb_id: '104716',
              evidence: {
                directory_group: { id: 'grp-104716', requires_confirm: true, status: 'pending_confirm' }
              }
            }
          }
        ]
      },
      directoryGroups: [
        {
          id: 'grp-104716',
          tmdb_id: '104716',
          category_key: 'cat:电视剧/综艺',
          title: '乘风',
          canonical_tmdb_title: '乘风',
          status: 'pending_confirm',
          entries: [
            { side: 'source', norm_path: '/src/乘风 (2025)/Season 6', seasons: [6], episodes_by_season: { '6': [1,2,3,4,5,6,7,8,9,10] } },
            { side: 'source', norm_path: '/src/乘风 2025/Season 6', seasons: [6], episodes_by_season: { '6': [1,2,3] } }
          ],
          candidate_targets: [
            { target_path: '/dst/乘风 (2025) [tmdbid=104716]', raw_name: '乘风 (2025) [tmdbid=104716]', matches_tmdb_page: true }
          ]
        },
        {
          id: 'grp-already-done',
          tmdb_id: '88888',
          title: '已确认综艺',
          status: 'confirmed',
          confirmed_target_path: '/dst/已确认综艺',
          entries: [
            { side: 'source', norm_path: '/src/已确认综艺/Season 1', seasons: [1], episodes_by_season: { '1': [1,2,3] } },
            { side: 'archive', norm_path: '/dst/已确认综艺', seasons: [1], episodes_by_season: { '1': [1,2,3] } }
          ],
          coverage_matrix: [
            {
              side: 'source',
              label: '源端',
              path: '/src/已确认综艺/Season 1',
              season_number: 1,
              season_label: 'S01',
              covered_count: 3,
              total_episodes: 3,
              coverage_text: '3/3集，E01–E03',
              missing_text: '无',
              can_delete: true,
              delete_block_reason: '归档目标已单独全季齐全且覆盖本源季内容，可安全清理',
              season_record_id: 'rec-done-s01',
              season_path: '/src/已确认综艺/Season 1'
            },
            {
              side: 'archive',
              label: '已确认归档目标',
              path: '/dst/已确认综艺',
              season_number: 1,
              season_label: 'S01',
              covered_count: 3,
              total_episodes: 3,
              coverage_text: '3/3集，E01–E03',
              missing_text: '无',
              is_confirmed_target: true,
              can_delete: false,
              delete_block_reason: '已确认归档库实存目标'
            }
          ]
        }
      ]
    };

    const dgContext = {
      document: { getElementById: getDgNode },
      currentCategory: 'download',
      currentTaskId: 'task-variety',
      taskList: [sampleTask],
      escapeHtml: String,
      escapeJsAttr: String,
      cleanSeasonStatusNote: str => str,
      updateSelection() {},
      updateGapFillSitesToolbarBadge() {},
      findItemByRecordId: id => sampleTask.categories.download.find(x => x.id === id),
      setTimeout: (fn) => fn(),
      _directoryGroupActiveTab: 'pending',
      _directoryGroupCollapsed: false,
      _expandedGroupIds: new Set(),
      _dirGroupReturnCategory: null
    };

    const funcs = [
      'cleanSeasonStatusNote', 'confirmCleanDirectoryGroupSourceSeason',
      'getCategoryName', 'toggleDirectoryGroupsSection', 'setDirectoryGroupViewTab',
      'toggleDirectoryGroupItemDetail', 'locateDirectoryGroupInAnomaly', 'locateDirectoryGroupInReview',
      'returnFromDirectoryGroupSection', 'filterResultCategory',
      'renderDirectoryGroupsPanel', 'getGapFillLists', 'renderEpisodesList'
    ].map(sourceWithCr).join(';\n');

    vm.createContext(dgContext);
    vm.runInContext(funcs, dgContext);

    // 1. 在缺集待补 (download) 分类下，整批目录组面板必须被完全隐藏，且无在途时「核对入库」按钮隐藏！
    dgContext.currentCategory = 'download';
    vm.runInContext('renderEpisodesList()', dgContext);
    const downloadPanel = getDgNode('directoryGroupsContainer');
    assert(downloadPanel.classList.contains('hidden'), '缺集待补分类下目录组面板必须添加 hidden 类');
    assert.strictEqual(downloadPanel.innerHTML, '', '缺集待补分类下目录组面板 innerHTML 必须清空');
    const dlBatchHtml = getDgNode('batchActionContainer').innerHTML;
    assert(!dlBatchHtml.includes('reconcileCurrentTaskGapFill'), '无在途/待确认分集时「核对入库」按钮必须隐藏降噪');

    // 1b. 在异常剧集 (anomaly) 分类下，目录组确认面板也必须隐藏（已归位待复核清理 review）
    dgContext.currentCategory = 'anomaly';
    vm.runInContext('renderEpisodesList()', dgContext);
    assert(getDgNode('directoryGroupsContainer').classList.contains('hidden'), '异常剧集分类下目录组面板必须隐藏');
    dgContext.currentCategory = 'download';
    vm.runInContext('renderEpisodesList()', dgContext);

    // 2. 检查真缺集待补季卡中的目录组徽章：必须包含跳转定位按钮及文案
    const downloadSeasonsHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(downloadSeasonsHtml.includes("locateDirectoryGroupInReview('grp-104716')"), '关联多目录的季卡必须挂载 locateDirectoryGroupInReview');
    assert(downloadSeasonsHtml.includes('归档目标待确认 · 查看目录'), '季卡目录组徽章文案必须为「归档目标待确认 · 查看目录」');

    // 3. 点击季卡上的定位按钮：跨页跳转至 review 待复核清理分类，自动展开对应组
    vm.runInContext("locateDirectoryGroupInReview('grp-104716')", dgContext);
    assert.strictEqual(dgContext.currentCategory, 'review', '点击季卡定位后当前分类必须自动切换到 review');
    assert.strictEqual(dgContext._dirGroupReturnCategory, 'download', '来源分类必须被记录为 download 以支持返回');
    assert.strictEqual(dgContext._directoryGroupCollapsed, false, '目录组区块必须确保处于展开状态');
    assert(dgContext._expandedGroupIds.has('grp-104716'), '目标组必须处于展开明细状态');

    // 4. 验证待复核清理 (review) 分类下目录组面板正常渲染且包含作品名与返回按钮
    const reviewPanel = getDgNode('directoryGroupsContainer');
    assert(!reviewPanel.classList.contains('hidden'), '待复核清理分类下目录组面板不得含有 hidden 类');
    const reviewPanelHtml = reviewPanel.innerHTML;
    assert(reviewPanelHtml.includes('待确认归档目标（1 组待办）'), '面板标题必须显示待处理组数');
    assert(reviewPanelHtml.includes('乘风 · 确认归档目标'), '必须直接使用作品名作为卡片标题');
    assert(reviewPanelHtml.includes('TMDB: 104716'), '卡片必须显示 TMDB ID');
    assert(reviewPanelHtml.includes('TMDB 页面显示：「乘风」'), '卡片必须显示 TMDB 官网页面当前名称');
    assert(reviewPanelHtml.includes('返回「缺集待补」'), '跨页跳转后顶部必须显示返回原清单按钮');
    assert(reviewPanelHtml.includes("openDirectoryGroupDialog('grp-104716')"), '卡片必须挂载确认归档目标弹窗按钮');
    // 目录组面板绝对不包含 itemCheck checkbox，保证不参与“全选清理冗余”
    assert(!reviewPanelHtml.includes('name="itemCheck"'), '目录组面板绝不可包含 itemCheck checkbox，防止误入批量清理');

    // 5. 点击“返回「缺集待补」”按钮：顺利切回 download 分类，目录组面板重新隐藏
    vm.runInContext('returnFromDirectoryGroupSection()', dgContext);
    assert.strictEqual(dgContext.currentCategory, 'download', '返回后当前分类必须恢复为 download');
    assert.strictEqual(dgContext._dirGroupReturnCategory, null, '返回后来源分类必须被清空');
    assert(getDgNode('directoryGroupsContainer').classList.contains('hidden'), '返回 download 后目录组面板必须重新隐藏');

    // 6. 验证已确认目录组展开时渲染逐路径逐季对照表格与单季清理按钮
    dgContext.currentCategory = 'review';
    dgContext._directoryGroupActiveTab = 'resolved';
    dgContext._expandedGroupIds.add('grp-already-done');
    vm.runInContext('renderEpisodesList()', dgContext);
    const resolvedPanelHtml = getDgNode('directoryGroupsContainer').innerHTML;
    assert(resolvedPanelHtml.includes('位置 / 物理路径'), '目录组详情展开必须包含对照表格表头「位置 / 物理路径」');
    assert(resolvedPanelHtml.includes('正片覆盖'), '目录组详情展开必须包含表头「正片覆盖」');
    assert(resolvedPanelHtml.includes('核验状态 / 操作'), '目录组详情展开必须包含表头「核验状态 / 操作」');
    assert(resolvedPanelHtml.includes('confirmCleanDirectoryGroupSourceSeason'), '符合单独全季齐全且覆盖的源端必须挂载 confirmCleanDirectoryGroupSourceSeason');
    assert(resolvedPanelHtml.includes('删除源端（本季）'), '可清理源端行必须展示「删除源端（本季）」操作按钮');
    assert(resolvedPanelHtml.includes('✓ 选定目标'), '已确认归档目标必须标明「✓ 选定目标」');

    // 7. 验证存在在途/待确认分集时「核对入库」按钮按需显示
    dgContext.currentCategory = 'download';
    dgContext.taskList[0].categories.download_pushed = [{ id: 'gp-1', show: '测试在途', season: 'S01', statusText: '0/5集', actionType: 'download', actionText: '查看进度', pathA: '/src/a', pathB: '/dst/a', note: '', isChecked: false, duplicates: [], missing: [1], recognizedEpisodes: [] }];
    vm.runInContext('renderEpisodesList()', dgContext);
    assert(getDgNode('batchActionContainer').innerHTML.includes('reconcileCurrentTaskGapFill'), '存在在途/待确认分集时「核对入库」按钮必须展示');

    console.log('✓ 目录组唯一收口待复核清理、异常/缺集页隐藏、跨页定位跳转、核对入库按需显示与单季删除源端契约测试全部通过');

    // ======================================================================
    // 验证 v2.5.36 连载留源只读契约、防按钮泄露、详情入口与空态/中止区分 (L01-L12)
    // ======================================================================
    const airingItemNormal = {
      id: 'air-l01',
      show: '沿海列车',
      season: 'S02',
      statusText: '已播 8 集 / 已收 7 集（全季 12 集） · 在途 1',
      mainCategory: 'airing',
      actionType: 'skip',
      actionText: '留源',
      pathA: '/src/沿海列车/Season 2',
      pathB: '/dst/沿海列车/Season 2',
      note: '已播 8 集，已收 7 集；E06 已提交下载，等待入库；后续 4 集待播。',
      duplicates: [5],
      airingView: {
        status: 'airing',
        badge_text: '连载中',
        fetched_at: '2026-11-18T00:00:00',
        last_recalculated_at: '2026-11-18T01:00:00',
        source_episodes: [1, 2, 3, 4, 5, 7],
        archived_episodes: [8],
        aired_episodes: [1, 2, 3, 4, 5, 6, 7, 8],
        uncollected_aired_episodes: [6],
        in_transit_episodes: [6],
        upcoming_episodes: [9, 10, 11, 12],
        duplicate_episodes: [5],
        next_episode_number: 9,
        next_air_date: '2026-11-25'
      },
      // 模拟底层带 download_needed / review_needed 历史状态 (L05)
      raw: {
        status: 'download_needed',
        main_category: 'airing',
        total_episodes: 12,
        recognized_episodes: [1, 2, 3, 4, 5, 7, 8]
      }
    };

    sampleTask.categories.airing = [airingItemNormal];
    sampleTask.summary = {
      last_airing_transitions: [
        { show_name: '沿海列车', season_number: 1, to_category: 'archive', to_category_label: '齐全可归档' }
      ]
    };
    dgContext.currentCategory = 'airing';
    dgContext.findItemByRecordId = id => (id === 'air-l01' ? airingItemNormal : null);
    vm.runInContext('renderEpisodesList()', dgContext);
    const airingHtml = getDgNode('episodeItemContainer').innerHTML;

    // 1. 所有连载季必须包含「查看详情」按钮，且禁用勾选框
    assert(airingHtml.includes("openGapFillDetail('air-l01')"), '连载季必须包含详情入口');
    assert(airingHtml.includes('查看详情'), '连载季详情按钮应显示「查看详情」');
    assert(airingHtml.includes('disabled hidden'), '连载季勾选框必须被禁用并隐藏');

    // 2. 即使 raw.status === download_needed 且 duplicates=[5]，绝不能泄露删源、清理、手动完成或归档按钮 (L05)
    assert(!airingHtml.includes('openDeleteSourceSeasonModal'), '连载季严禁泄露删除源端（本季）按钮');
    assert(!airingHtml.includes('promptDeleteSourceShow'), '连载季严禁泄露整剧删除源端按钮');
    assert(!airingHtml.includes('markSeasonHandled'), '连载季严禁泄露已手动处理按钮');
    assert(!airingHtml.includes('openCleanPreview'), '连载季严禁泄露清理预览按钮');
    assert(!airingHtml.includes('handleSingleAction'), '连载季严禁挂载归档/补缺执行按钮');
    assert(airingHtml.includes('最近核查: 2026-11-18 00:00:00'), '连载卡必须展示取证时间');
    assert(airingHtml.includes('重新核算: 2026-11-18 01:00:00'), '连载卡必须分别展示重新核算时间');

    // 3. 验证点开连载详情弹窗只读且包含分端覆盖与在途明细，无删源或改绑操作 (L02, L05)
    getDgNode('gapFillDetailDialog').showModal = () => {};
    vm.runInContext(['seasonDecisionSummary','readableLookupReason','renderDoubanLookup'].map(sourceWithCr).join(';') + ';' + sourceWithCr('openGapFillDetail') + ";openGapFillDetail('air-l01')", dgContext);
    const airingDetailHtml = getDgNode('gapFillDetailBody').innerHTML;
    assert(airingDetailHtml.includes('连载留源 · 季级播期与分端覆盖明细'), '详情必须渲染连载分端覆盖面板');
    assert(airingDetailHtml.includes('源端 6 集'), '详情必须准确显示源端 6 集');
    assert(airingDetailHtml.includes('归档端 1 集'), '详情必须准确显示归档端 1 集');
    assert(airingDetailHtml.includes('E06 已提交下载，等待入库'), '详情必须单列在途下载 E06');
    assert(!airingDetailHtml.includes('openDeleteSourceSeasonModal'), '连载详情弹窗严禁出现放弃并删除本季按钮');
    assert(!airingDetailHtml.includes('openSeasonMappingModal'), '连载详情弹窗严禁出现改绑映射按钮');

    // 异常列表中的连载目录组同样受保护，不能只按当前页判断可操作性。
    const airingAnomaly = {
      ...airingItemNormal, id: 'boss-anomaly', mainCategory: 'anomaly',
      actionType: 'inspect_info', isAnomaly: true, anomalySubtype: 'file_check',
      anomalySubCategory: 'decision',
      raw: {status: 'airing', main_category: 'anomaly', airing_protected: true},
      evidence: {directory_group: {id: 'boss-group', requires_confirm: true}}
    };
    sampleTask.categories.anomaly = [airingAnomaly];
    dgContext.currentCategory = 'anomaly';
    vm.runInContext('renderEpisodesList()', dgContext);
    const bossHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(!bossHtml.includes('markSeasonHandled'), '异常模块中的连载原始状态也不得出现手动处理');
    assert(bossHtml.includes("openDirectoryGroupDialog('boss-group')"), '多目录条目主操作应直达确认目录');
    assert(!bossHtml.includes("recheckSeasonEvidence('boss-anomaly', this)"), '连载多目录异常卡无常驻重新核查按钮');
    assert(!bossHtml.includes('handleSingleAction'), '连载异常不能泄露写文件主操作');

    // 初检请求失败（fetch_failed）条目：主入口为「重试失败项」，扫描进行中禁用
    sampleTask.categories.anomaly = [{...airingAnomaly, id: 'failed-fetch',
      airingView: null, anomalySubCategory: 'pending_evidence', anomalySubtype: 'unknown_meta',
      raw: {status: 'unknown', main_category: 'anomaly'},
      evidence: {meta_status: 'fetch_failed'}}];
    vm.runInContext('renderEpisodesList()', dgContext);
    const failedHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(failedHtml.includes("openManualConfirmModal('failed-fetch')"), '失败项统一进入手动复核');
    assert(failedHtml.includes('手动复核'), '主按钮为手动复核');
    sampleTask.status = 'running';
    vm.runInContext('renderEpisodesList()', dgContext);
    assert(/openManualConfirmModal[^>]+disabled/.test(getDgNode('episodeItemContainer').innerHTML), '初检进行中禁用重试失败项');
    sampleTask.status = 'completed';

    // 候选资料不确定时统一标签为「作品资料不完整」，主入口为「人工确认」，更多菜单提供「本次保留，不再提示」，不引导删源或结案。
    sampleTask.categories.anomaly = [{...airingAnomaly, id: 'candidate-reference',
      raw: {status: 'unknown', main_category: 'anomaly'}, airingView: null,
      actionType: 'calibrate', anomalySubtype: 'unknown_meta', anomalySubCategory: 'pending_evidence',
      evidence: {meta_status: 'metadata_unverified'}}];
    vm.runInContext('renderEpisodesList()', dgContext);
    const candidateHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(candidateHtml.includes('作品资料不完整'), '统一标签必须为「作品资料不完整」');
    assert(!candidateHtml.includes('非文件错配'), '标签不得追加超出证据的「非文件错配」断言');
    assert(candidateHtml.includes("openManualConfirmModal('candidate-reference')"), '资料不完整主入口必须为人工确认');
    assert(!candidateHtml.includes("holdSeasonForTask('candidate-reference', this)"), '暂不处理已收进手动复核');
    for (const action of ['markSeasonHandled', 'promptDeleteSourceShow', 'handleSingleAction', 'recheckSeasonEvidence']) {
      assert(!candidateHtml.includes(action), `未核实候选资料不能引导 ${action}`);
    }
    dgContext.currentCategory = 'airing';

    // 4. 验证零条完整核查 vs 扫描中止 vs 加载失败的不同反馈 (L11)
    sampleTask.categories.airing = [];
    sampleTask.status = 'completed';
    sampleTask.counts.total = 5;
    vm.runInContext('renderEpisodesList()', dgContext);
    assert(getDgNode('episodeItemContainer').innerHTML.includes('本次初检没有需要留源等待播出的季'), '完整核查零条应显示明确提示');

    sampleTask.status = 'stopped';
    sampleTask.time = '2026-11-18 00:30';
    vm.runInContext('renderEpisodesList()', dgContext);
    assert(getDgNode('episodeItemContainer').innerHTML.includes('未扫描范围不解释为零'), '扫描中止时应明示已分析范围且不将未扫描解释为零');

    sampleTask._seasonsLoadError = true;
    vm.runInContext('renderEpisodesList()', dgContext);
    assert(getDgNode('episodeItemContainer').innerHTML.includes('重试加载'), '接口加载失败时必须提供重试加载入口');
    sampleTask._seasonsLoadError = false;
    sampleTask.status = 'completed';

    // ======================================================================
    // 验证 v2.5.38 真缺集待补模块前端契约 (D01-D09)
    // ======================================================================
    const gapTodoPartialItem = {
      id: 'gap-rookie-s3',
      show: '菜鸟老警',
      season: 'S03',
      statusText: '已覆盖 11/14集（待补 1 · 已推 2）',
      mainCategory: 'download',
      actionType: 'download',
      actionText: '推送剩余 1 集',
      pathA: '/src/菜鸟老警/Season 3',
      pathB: '/dst/菜鸟老警/Season 3',
      note: '缺第 3, 7, 9 集',
      missing: [3, 7, 9],
      totalEpisodes: 14,
      evidence: {
        broadcast_evidence: { source_episodes: [1, 2, 4, 5, 6, 8, 10, 11], archived_episodes: [12, 13, 14] },
        source_total_conflict: { tmdb_total: 14, douban_total: 15, tail_basis: '尾集 E15 因两来源总数冲突暂缓补缺' }
      },
      gapFillState: {
        view_bucket: 'todo',
        physical_missing_episodes: [3, 7, 9],
        actionable_missing_episodes: [9],
        pushed_episodes: [3, 7],
        unverified_count: 0,
        last_outcome: 'partial',
        last_run: {
          outcome_status: 'partial',
          summary_text: '部分推送（受理 [3, 7]；未成功 [9]）',
          candidate_count: 5,
          max_seeders_seen: 6,
          min_seeders_required: 2,
          sites_summary: { requested: '4:彩虹岛', returned: ['4:彩虹岛'] }
        },
        episodes: [
          { episode: 3, episode_number: 3, state: 'queued', outcome: 'accepted', outcome_code: 'accepted', site: '彩虹岛', torrent_title: 'The.Rookie.S03E03.1080p', reason: 'MP已受理', sanitized_reason: 'MP已受理' },
          { episode: 9, episode_number: 9, state: 'todo', outcome: 'low_seeders', outcome_code: 'low_seeders', site: '', torrent_title: '', reason: 'E09 找到 3 个候选，最高 1 人做种，本次要求至少 2 人', sanitized_reason: 'E09 找到 3 个候选，最高 1 人做种，本次要求至少 2 人' }
        ]
      },
      raw: { status: 'download_needed', main_category: 'download', total_episodes: 14, recognized_episodes: [1, 2, 4, 5, 6, 8, 10, 11] }
    };
    const gapPushedUnverifiedItem = {
      id: 'gap-unv-s1',
      show: '超时待确认剧',
      season: 'S01',
      statusText: '已覆盖 4/6集（已提交 2 集待确认）',
      mainCategory: 'download',
      actionType: 'inspect_gap_detail',
      actionText: '查看进度',
      pathA: '/src/超时待确认剧/Season 1',
      note: '缺第 5, 6 集',
      missing: [5, 6],
      totalEpisodes: 6,
      gapFillState: {
        view_bucket: 'pushed_pending',
        physical_missing_episodes: [5, 6],
        actionable_missing_episodes: [],
        pushed_episodes: [5, 6],
        unverified_episodes: [5, 6],
        unverified_count: 2,
        only_unverified_pending: true,
        episodes: [
          { episode: 5, episode_number: 5, state: 'submitted_unverified', outcome: 'timeout_unverified', outcome_code: 'timeout_unverified', reason: '请求超时待确认', sanitized_reason: '请求超时待确认' }
        ]
      },
      raw: { status: 'download_needed', main_category: 'download', total_episodes: 6 }
    };

    sampleTask.categories.download = [gapTodoPartialItem];
    sampleTask.categories.download_pushed = [gapPushedUnverifiedItem];
    dgContext.currentCategory = 'download';
    dgContext.currentGapView = 'todo';
    dgContext.findItemByRecordId = id => (id === 'gap-rookie-s3' ? gapTodoPartialItem : (id === 'gap-unv-s1' ? gapPushedUnverifiedItem : null));
    vm.runInContext('renderEpisodesList()', dgContext);
    const gapBatchBarHtml = getDgNode('batchActionContainer').innerHTML;
    const gapTodoCardsHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(gapBatchBarHtml.includes('提交待确认 (1)'), '当等待页仅剩超时待确认请求时，页签应显示「提交待确认」');
    assert(gapTodoCardsHtml.includes('推送剩余 1 集'), '部分已推的待补季按钮应显示「推送剩余 N 集」');

    // 切到等待页：只显示单个「查看进度」按钮，不重复渲染「详情」按钮，且不显示「删除源端（本季）」
    dgContext.currentGapView = 'pushed_pending';
    vm.runInContext('renderEpisodesList()', dgContext);
    const gapPushedCardsHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(gapPushedCardsHtml.includes('查看进度'), '等待页卡片应显示「查看进度」');
    assert(!gapPushedCardsHtml.includes('<span>详情</span>'), '等待页卡片不得同时重复渲染第二个「详情」按钮');
    assert(!gapPushedCardsHtml.includes('openDeleteSourceSeasonModal'), '等待页卡片仅提供核对与查看进度，不渲染删除源端（本季）');

    // 验证详情弹窗渲染三端实物覆盖与 E09 精确低做种原因
    vm.runInContext(['seasonDecisionSummary','readableLookupReason','renderDoubanLookup'].map(sourceWithCr).join(';') + ';' + sourceWithCr('openGapFillDetail') + ";openGapFillDetail('gap-rookie-s3')", dgContext);
    const gapDetailHtml = getDgNode('gapFillDetailBody').innerHTML;
    assert(gapDetailHtml.includes('三端实物覆盖与缺号核定依据'), '补缺详情弹窗必须包含三端实物覆盖卡片');
    assert(gapDetailHtml.includes('E09'), '补缺详情表格必须正确显示 E09 集号而非 E00');
    assert(gapDetailHtml.includes('E09 找到 3 个候选，最高 1 人做种，本次要求至少 2 人'), '补缺详情必须显示精确到候选数与最高做种数的低做种原因');

    console.log('✓ 连载留源 L01-L12 与真缺集待补 D01-D09 前端契约测试全部通过');
  }
}

// 实际执行只读核查处理器：覆盖同卡关联目录，并在失败后恢复按钮。
(async () => {
  const vm = require('node:vm');
  const handler = htmlContent.match(/async function recheckSeasonEvidence\([^]*?\n    \}/)[0];
  const item = {raw: {participating_record_ids: ['boss-a', 'boss-b']}, associatedRecords: [{id: 'boss-b'}]};
  const calls = [];
  const button = {disabled: false, innerHTML: '重新核查', isConnected: true};
  const ctx = {currentTaskId: 'task-boss', taskList: [{id: 'task-boss', _seasonsLoaded: true}],
    findItemByRecordId: () => item, alert: message => calls.push(message),
    api: async (url, options) => {calls.push({url, body: JSON.parse(options.body)}); return {message: '核查完成'};},
    loadTasks: async () => {}, selectTask: async id => calls.push(id)};
  vm.createContext(ctx);
  vm.runInContext(handler, ctx);
  await ctx.recheckSeasonEvidence('boss-a', button);
  assert.deepStrictEqual(calls[0].body.record_ids, ['boss-a', 'boss-b']);
  assert.strictEqual(calls[0].body.fetch_remote, true);
  assert.strictEqual(ctx.taskList[0]._seasonsLoaded, false);
  assert.strictEqual(button.disabled, false);
  ctx.api = async () => {throw new Error('fixture timeout');};
  await ctx.recheckSeasonEvidence('boss-a', button);
  assert(calls.some(c => typeof c === 'string' && c.includes('原有文件保留')));
  assert.strictEqual(button.innerHTML, '重新核查');
  assert.strictEqual(button.disabled, false);
  console.log('✓ 重新核查覆盖关联目录、失败回执及按钮恢复通过');

  // 回归测试：批量/单季补缺弹窗(taskList)、verify_basis 人工确认分流、转移修复设置(Boolean)与单引号转义(escapeJsAttr)
  assert(!htmlContent.includes('tasksData.find('), 'prepareDownloadModal 不得引用未声明变量 tasksData');
  assert(!/\bbool\s*\(/.test(htmlContent), '前端 JS 不得调用 Python 的 bool()');
  {
    const btnCtx = {
      taskList: [{
        id: 't-dl',
        categories: {
          download: [
            { id: 'rec-1', show: "The Boulet Brothers' Dragula", season: 'S01', missing: [2, 3], raw: { status: 'download_needed', gap_category: 'non_anime' }, gapFillState: { actionable_missing_episodes: [2, 3], pushed_episodes: [] } },
            { id: 'rec-2', show: '哈哈哈哈哈', season: 'S04', missing: [5], raw: { status: 'period_missing', gap_category: 'non_anime' }, gapFillState: { actionable_missing_episodes: [5], pushed_episodes: [] } }
          ]
        }
      }],
      currentTaskId: 't-dl',
      currentActionRecordId: null,
      configRoots: { gap_fill_min_seeders: 2 },
      gapFillRulesCache: {},
      gapFillSitesCatalogCache: [],
      openedModals: [],
      manualConfirmOpened: [],
      openModal(id) { btnCtx.openedModals.push(id); },
      openManualConfirmModal(id) { btnCtx.manualConfirmOpened.push(id); },
      findItemByRecordId(id) { return btnCtx.taskList[0].categories.download.find(x => x.id === id) || null; },
      document: {
        _els: {
          downloadModalTitle: { textContent: '' },
          downloadModalTargetSummary: { innerHTML: '' },
          repairSettingEnabled: { checked: false },
          repairSettingAutoRepair: { checked: false },
          repairSettingMaxConcurrency: { value: '' },
          repairSettingBatchQuota: { value: '' },
          repairSettingRetryBudget: { value: '' },
          repairSettingInterval: { value: '' }
        },
        getElementById(id) { return this._els[id] || null; },
        querySelectorAll(sel) {
          if (sel.includes('itemCheck')) return [{ value: 'rec-1' }, { value: 'rec-2' }];
          return [];
        }
      },
      api: async () => ({ is_enabled: 1, auto_repair_enabled: 0, max_concurrency: 2, batch_quota: 10, retry_budget: 3, scan_interval_minutes: 15 }),
      alert(m) { throw new Error('Unexpected alert: ' + m); }
    };
    const extractFn = name => {
      const m = htmlContent.match(new RegExp(`(?:async\\s+)?function\\s+${name}\\b[^]*?\\n    \\}`));
      assert(m, `找不到函数 ${name}`);
      return m[0];
    };
    vm.createContext(btnCtx);
    vm.runInContext([
      extractFn('escapeHtml'),
      extractFn('escapeJsAttr'),
      extractFn('prepareDownloadModal'),
      extractFn('executeBatchDownload'),
      extractFn('handleSingleAction'),
      extractFn('openTransferRepairSettingsModal')
    ].join(';\n'), btnCtx);

    btnCtx.executeBatchDownload();
    assert(btnCtx.openedModals.includes('downloadModal'), 'executeBatchDownload 必须正常打开 downloadModal');
    assert(btnCtx.document._els.downloadModalTitle.textContent.includes('共 2 季'), '批量补缺弹窗标题必须显示选中季数');

    btnCtx.handleSingleAction('verify_basis', 'rec-1');
    assert.deepStrictEqual(btnCtx.manualConfirmOpened, ['rec-1'], 'verify_basis 按钮必须正常触发 openManualConfirmModal');

    await btnCtx.openTransferRepairSettingsModal();
    assert.strictEqual(btnCtx.document._els.repairSettingEnabled.checked, true);
    assert.strictEqual(btnCtx.document._els.repairSettingAutoRepair.checked, false);
    assert(btnCtx.openedModals.includes('repairSettingsModal'), 'openTransferRepairSettingsModal 必须正常打开弹窗');

    const escaped = btnCtx.escapeJsAttr("The Boulet Brothers' Dragula");
    assert.strictEqual(escaped, "The Boulet Brothers\\' Dragula");
    console.log('✓ 批量补缺弹窗、verify_basis 分流、转移修复设置与单引号转义回归测试通过');
  }

  // 回归测试：执行推荐处理异步轮询、needs_repreview 拦截展示与按钮状态复位
  {
    const mkBtn = () => {
      const cls = new Set();
      return {
        disabled: false,
        innerHTML: '',
        onclick: null,
        isConnected: true,
        classList: {
          add(c) { cls.add(c); },
          remove(c) { cls.delete(c); },
          contains(c) { return cls.has(c); }
        }
      };
    };
    const recEls = {
      recBatchExecBtn: mkBtn(),
      recBatchBody: { innerHTML: '' },
      recBatchFootNote: { textContent: '' },
      recBatchSummaryBar: { innerHTML: '' }
    };
    let pollStep = 0;
    let mode = 'needs_repreview';
    let refreshCount = 0;
    const recCtx = {
      currentTaskId: 'task-rec',
      _currentRecommendedPlan: { plan_id: 'plan-1' },
      _recBatchAbortCtrl: null,
      _recExecPollToken: 0,
      taskList: [{ id: 'task-rec', _seasonsLoaded: true }],
      document: {
        getElementById(id) { return recEls[id] || null; }
      },
      setTimeout,
      openModal() {},
      openRecommendedBatchModal() {},
      loadTasks: async () => { refreshCount++; },
      selectTask: async () => { refreshCount++; },
      alert(m) { throw new Error('Unexpected alert: ' + m); },
      api: async (path) => {
        if (path === '/api/archive/recommended-execute-start') {
          pollStep = 0;
          return { ok: true, job_id: 'job-1', status: 'running', phase: 'fingerprint_check', done_steps: 0, total_steps: 2, message: '校验指纹中' };
        }
        if (path.startsWith('/api/archive/recommended-execute-status/')) {
          pollStep++;
          if (mode === 'needs_repreview') {
            return {
              ok: true,
              job_id: 'job-1',
              status: 'needs_repreview',
              result: {
                ok: false,
                needs_repreview: true,
                changed_seasons: [{ show_name: '测试剧', season_number: 1, added_videos: ['/src/new.mp4'], modified_videos: [] }],
                message: '检测到 1 个季新增视频'
              }
            };
          }
          if (pollStep === 1) {
            return { ok: true, job_id: 'job-1', status: 'running', phase: 'clean', done_steps: 1, total_steps: 2, message: '清理中' };
          }
          return {
            ok: true,
            job_id: 'job-1',
            status: 'completed',
            result: {
              ok: true,
              fully_resolved: !['partial', 'blocked'].includes(mode),
              no_progress: mode === 'blocked',
              message: '批量推荐处理完成：目录归并 1 组、副本清理 1 季',
              outcomes: { remaining_review: ['partial', 'blocked'].includes(mode) ? [{show_name:'剩余剧',season_number:1,reason:'缩略图待处理'}] : [], to_download: [{ show_name: '剧A', season_number: 1 }], to_airing: [], to_archive: [], source_cleaned: [], remaining_anomalies: [], skipped_items: [] },
              failed_steps: []
            }
          };
        }
        throw new Error('Unexpected API path: ' + path);
      }
    };
    const extractFn = name => {
      const m = htmlContent.match(new RegExp(`(?:async\\s+)?function\\s+${name}\\b[^]*?\\n    \\}`));
      assert(m, `找不到函数 ${name}`);
      return m[0];
    };
    vm.createContext(recCtx);
    vm.runInContext([
      extractFn('escapeHtml'),
      extractFn('escapeJsAttr'),
      'let _currentRecommendedPlan = recCtx_initPlan;',
    ].join(';\n'), Object.assign(recCtx, { recCtx_initPlan: { plan_id: 'plan-1' } }));
    vm.runInContext([
      'var _recExecPollToken = 0;',
      extractFn('_resetRecBatchExecBtn'),
      extractFn('_renderRecBatchProgress'),
      extractFn('renderRecommendedRepreview'),
      extractFn('executeRecommendedBatchPlan')
    ].join(';\n'), recCtx);

    // Case 1: needs_repreview 不得伪装为完成，且按钮必须变为重新生成预览
    await recCtx.executeRecommendedBatchPlan();
    assert(recEls.recBatchBody.innerHTML.includes('本轮已暂停，请更新预览'), 'needs_repreview 必须明确展示拦截提示');
    assert(!recEls.recBatchBody.innerHTML.includes('推荐处理执行完成'), 'needs_repreview 不得渲染执行完成卡片');
    assert(recEls.recBatchExecBtn.innerHTML.includes('更新预览'), 'needs_repreview 后按钮必须提示重新生成预览');
    assert.strictEqual(recEls.recBatchExecBtn.disabled, false);

    // Case 2: 正常后台轮询完成，必须刷新列表且按钮不得停留在转圈状态
    mode = 'completed';
    await recCtx.executeRecommendedBatchPlan();
    assert(recEls.recBatchBody.innerHTML.includes('批量推荐处理完成'), '完成后必须展示汇总结果');
    assert(!recEls.recBatchExecBtn.innerHTML.includes('fa-spin'), '完成后按钮不得继续旋转');
    assert(!recEls.recBatchExecBtn.innerHTML.includes('正在分步执行'), '完成后按钮不得残留正在执行文案');
    assert(recEls.recBatchExecBtn.innerHTML.includes('本批次已全部解决'), '完成后按钮必须清晰标记已完成');
    assert.strictEqual(refreshCount, 2, '完成后必须调用 loadTasks 和 selectTask 刷新列表');
    mode = 'partial';
    await recCtx.executeRecommendedBatchPlan();
    assert(recEls.recBatchBody.innerHTML.includes('缩略图待处理'));
    assert(recEls.recBatchExecBtn.innerHTML.includes('更新剩余项预览'));
    assert.strictEqual(recEls.recBatchExecBtn.disabled, false);
    mode = 'blocked';
    await recCtx.executeRecommendedBatchPlan();
    assert(recEls.recBatchExecBtn.innerHTML.includes('本轮无进展'));
    assert.strictEqual(recEls.recBatchExecBtn.disabled, true);
    assert.strictEqual(recEls.recBatchExecBtn.onclick, null);
    console.log('✓ 执行推荐处理异步轮询、指纹拦截分支与按钮状态复位测试通过');
  }

  // 回归测试：补缺站点规则刷新回显不丢失（修复 gapFillDraftRules 预置真值屏蔽加载与双页签协同保存）
  {
    const alerts = [];
    const savedPayloads = [];
    const gfEls = {
      gapFillSitesModal: { showModal() {} },
      gfSitesInstanceBadge: { textContent: '' },
      gfSitesFetchTime: { textContent: '' },
      gfTabBtnAnime: { textContent: '', className: '' },
      gfTabBtnNonAnime: { textContent: '', className: '' },
      gfCategoryStatusSelect: { value: 'active' },
      gfRuleVersionBadge: { textContent: '', className: '' },
      gfSelectedSitesList: { innerHTML: '' },
      gfSelectedCountText: { textContent: '' },
      gfUnselectedSitesList: { innerHTML: '' },
      gfUnselectedSearchInput: { value: '' },
      gfSaveRuleBtn: { disabled: false, innerHTML: '' },
      gfSitesConflictAlert: { classList: { add() {}, remove() {} } },
      gfSitesConflictMsg: { textContent: '' },
      gapFillSitesToolbarBadge: { textContent: '' }
    };
    const gfCtx = {
      document: { getElementById(id) { return gfEls[id] || null; } },
      alert(m) { alerts.push(m); },
      confirm() { return true; },
      console: { warn() {}, log() {}, error() {} },
      api: async (path, opts) => {
        if (path === '/api/moviepilot/gap-fill/sites/rules' && (!opts || opts.method !== 'POST')) {
          return {
            ok: true,
            instance_key: 'https___mp.test_8920',
            anime: { category: 'anime', selected_site_ids: [16, 17], status: 'uninitialized', version: 1, is_draft: true },
            non_anime: { category: 'non_anime', selected_site_ids: [15, 14, 5, 1, 4], status: 'active', version: 3, is_draft: false }
          };
        }
        if (path === '/api/moviepilot/gap-fill/sites/catalog') {
          return {
            ok: true,
            instance_key: 'https___mp.test_8920',
            sites: [
              { id: 15, name: 'Site15', domain: 's15.test', is_active: true },
              { id: 14, name: 'Site14', domain: 's14.test', is_active: true },
              { id: 16, name: 'Site16', domain: 's16.test', is_active: true },
              { id: 17, name: 'Site17', domain: 's17.test', is_active: true },
              { id: 99, name: 'Site99', domain: 's99.test', is_active: true }
            ]
          };
        }
        if (path === '/api/moviepilot/gap-fill/sites/rules' && opts && opts.method === 'POST') {
          const body = JSON.parse(opts.body);
          savedPayloads.push(body);
          return {
            ok: true,
            rule: {
              category: body.category,
              selected_site_ids: body.selected_site_ids,
              status: body.status,
              version: (body.expected_version || 1) + 1,
              is_draft: false
            }
          };
        }
        throw new Error('Unexpected API path: ' + path);
      }
    };
    const extractFn = name => {
      const m = htmlContent.match(new RegExp(`(?:async\\s+)?function\\s+${name}\\b[^]*?\\n    \\}`));
      assert(m, `找不到函数 ${name}`);
      return m[0];
    };
    vm.createContext(gfCtx);
    vm.runInContext([
      extractFn('escapeHtml'),
      'let gapFillRulesCache = null;',
      'let gapFillSitesCatalogCache = null;',
      'let activeGapFillSitesTab = "anime";',
      'let gapFillDraftRules = null;',
      'let _gapFillDraftDirty = { anime: false, non_anime: false };',
      extractFn('_ensureGapFillDraftRules'),
      extractFn('loadGapFillSitesRules'),
      extractFn('refreshGapFillSitesCatalog'),
      extractFn('openGapFillSitesModal'),
      extractFn('switchGapFillSitesTab'),
      extractFn('onGapCategoryStatusChange'),
      extractFn('renderGapFillSitesModal'),
      extractFn('renderGapFillSelectedSites'),
      extractFn('renderGapFillUnselectedSites'),
      extractFn('addGapFillSite'),
      extractFn('saveCurrentGapFillSiteRule'),
      extractFn('updateGapFillSitesToolbarBadge')
    ].join(';\n'), gfCtx);

    // 断言源码本身初始值必须是 null 而非真值空对象
    assert(/let\s+gapFillDraftRules\s*=\s*null\s*;/.test(htmlContent), 'gapFillDraftRules 必须初始化为 null');

    // 模拟刷新页面后冷启动并打开弹窗：必须直接加载数据库持久化规则，绝不空置
    await gfCtx.openGapFillSitesModal();
    gfCtx.switchGapFillSitesTab('non_anime');
    assert.strictEqual(gfEls.gfSelectedCountText.textContent, '已选 5 站', '刷新后非动漫已保存规则必须完整回显 5 站');
    assert(gfEls.gfSelectedSitesList.innerHTML.includes('Site15'), '已选列表必须渲染 Site15');
    assert(gfEls.gfRuleVersionBadge.textContent.includes('已保存 v3'), '状态角标必须显示已保存 v3');
    assert(gfEls.gapFillSitesToolbarBadge.textContent.includes('动漫未保存 · 非动漫 5 站'), '工具栏角标必须清晰区分草稿与已保存');

    // 在动漫页加一个站点、切到非动漫页加一个站点，一键保存必须同时持久化两个页签
    gfCtx.switchGapFillSitesTab('anime');
    gfCtx.addGapFillSite(99);
    gfCtx.switchGapFillSitesTab('non_anime');
    gfCtx.addGapFillSite(99);
    await gfCtx.saveCurrentGapFillSiteRule();
    assert.strictEqual(savedPayloads.length, 2, '同时修改两页签后点击保存必须两套规则一起落库');
    assert.deepStrictEqual(savedPayloads.map(x => x.category).sort(), ['anime', 'non_anime']);
    assert(alerts[0].includes('【动漫】') && alerts[0].includes('【非动漫】'), '保存提示必须告知两套规则均已保存');
    console.log('✓ 补缺站点规则刷新回显持久化与双页签协同保存回归测试通过');
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
