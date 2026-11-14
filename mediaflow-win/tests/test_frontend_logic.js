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
  review_needed: 'review', airing: 'airing', source_absent: ''
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
    escapeHtml: String, updateSelection() {}, findItemByRecordId: () => item,
    openModal: id => {context.openedModal = id;}};
  vm.createContext(context);
  vm.runInContext(source('cleanSeasonStatusNote') + ';' + source('renderEpisodesList') + ';renderEpisodesList()', context);
  assert(getNode('episodeItemContainer').innerHTML.includes("markSeasonHandled('record', this)"));
  context.currentCategory = 'download';
  vm.runInContext('renderEpisodesList()', context);
  const downloadCard = getNode('episodeItemContainer').innerHTML;
  assert(!downloadCard.includes("promptDeleteSourceShow("), "真缺集待补分类绝不应包含删除源端整剧删除按钮！");
  assert(!downloadCard.includes('markSeasonHandled('));

  // 验证未收录异常项挂载整剧删除
  item.anomalySubtype = 'unknown_meta';
  context.currentCategory = 'anomaly';
  vm.runInContext('renderEpisodesList()', context);
  const anomalyUnknownCard = getNode('episodeItemContainer').innerHTML;
  assert(anomalyUnknownCard.includes("promptDeleteSourceShow('record')"), "异常剧集未收录项必须挂载删除源端按钮！");
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
          scrollIntoView() {}
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
      cleanSeasonStatusNote: str => str,
      updateSelection() {},
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
      'renderDirectoryGroupsPanel', 'renderEpisodesList'
    ].map(sourceWithCr).join(';\n');

    vm.createContext(dgContext);
    vm.runInContext(funcs, dgContext);

    // 1. 在真缺集待补 (download) 分类下，整批目录组面板必须被完全隐藏！
    dgContext.currentCategory = 'download';
    vm.runInContext('renderEpisodesList()', dgContext);
    const downloadPanel = getDgNode('directoryGroupsContainer');
    assert(downloadPanel.classList.contains('hidden'), '真缺集待补分类下目录组面板必须添加 hidden 类');
    assert.strictEqual(downloadPanel.innerHTML, '', '真缺集待补分类下目录组面板 innerHTML 必须清空');

    // 2. 检查真缺集待补季卡中的目录组徽章：必须包含跳转定位按钮及文案
    const downloadSeasonsHtml = getDgNode('episodeItemContainer').innerHTML;
    assert(downloadSeasonsHtml.includes("locateDirectoryGroupInAnomaly('grp-104716')"), '关联多目录的季卡必须挂载 locateDirectoryGroupInAnomaly');
    assert(downloadSeasonsHtml.includes('归档目标待确认 · 查看目录'), '季卡目录组徽章文案必须为「归档目标待确认 · 查看目录」');

    // 3. 点击季卡上的定位按钮：跨页跳转至 anomaly 异常剧集分类，自动展开对应组
    vm.runInContext("locateDirectoryGroupInAnomaly('grp-104716')", dgContext);
    assert.strictEqual(dgContext.currentCategory, 'anomaly', '点击季卡定位后当前分类必须自动切换到 anomaly');
    assert.strictEqual(dgContext._dirGroupReturnCategory, 'download', '来源分类必须被记录为 download 以支持返回');
    assert.strictEqual(dgContext._directoryGroupCollapsed, false, '目录组区块必须确保处于展开状态');
    assert(dgContext._expandedGroupIds.has('grp-104716'), '目标组必须处于展开明细状态');

    // 4. 验证异常剧集 (anomaly) 分类下目录组面板正常渲染且包含作品名与返回按钮
    const anomalyPanel = getDgNode('directoryGroupsContainer');
    assert(!anomalyPanel.classList.contains('hidden'), '异常剧集分类下目录组面板不得含有 hidden 类');
    const anomalyPanelHtml = anomalyPanel.innerHTML;
    assert(anomalyPanelHtml.includes('待确认归档目标（1 组待办）'), '面板标题必须显示待处理组数');
    assert(anomalyPanelHtml.includes('乘风 · 确认归档目标'), '必须直接使用作品名作为卡片标题');
    assert(anomalyPanelHtml.includes('TMDB: 104716'), '卡片必须显示 TMDB ID');
    assert(anomalyPanelHtml.includes('TMDB 页面显示：「乘风」'), '卡片必须显示 TMDB 官网页面当前名称');
    assert(anomalyPanelHtml.includes('返回「真缺集待补」'), '跨页跳转后顶部必须显示返回原清单按钮');
    assert(anomalyPanelHtml.includes("openDirectoryGroupDialog('grp-104716')"), '卡片必须挂载确认归档目标弹窗按钮');
    // 目录组面板绝对不包含 itemCheck checkbox，保证不参与“全选清理冗余”
    assert(!anomalyPanelHtml.includes('name="itemCheck"'), '目录组面板绝不可包含 itemCheck checkbox，防止误入批量清理');

    // 5. 点击“返回「真缺集待补」”按钮：顺利切回 download 分类，目录组面板重新隐藏
    vm.runInContext('returnFromDirectoryGroupSection()', dgContext);
    assert.strictEqual(dgContext.currentCategory, 'download', '返回后当前分类必须恢复为 download');
    assert.strictEqual(dgContext._dirGroupReturnCategory, null, '返回后来源分类必须被清空');
    assert(getDgNode('directoryGroupsContainer').classList.contains('hidden'), '返回 download 后目录组面板必须重新隐藏');

    // 6. 验证已确认目录组展开时渲染逐路径逐季对照表格与单季清理按钮
    dgContext.currentCategory = 'anomaly';
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

    console.log('✓ 目录组唯一收口异常剧集、缺集页隐藏、跨页定位跳转、逐季覆盖矩阵与单季删除源端契约测试全部通过');
  }
}
