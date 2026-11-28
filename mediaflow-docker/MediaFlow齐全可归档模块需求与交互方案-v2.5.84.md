# MediaFlow「齐全可归档」模块需求与交互方案 v2.5.84

> **文档基线**：修订日期 **2026-11-28**，内容已全面核对并对齐实际源码 **v2.5.84**（五模块互斥分流与 4D 内容等价保留 `v2.5.45`、归并目标防 `cat:` 污染与同名零覆盖 `v2.5.51`、附随相邻扩展集独立保留 `v2.5.53`、补缺到位后完整实物复核 `v2.5.58`、清源附件目录递归保全与剧根收尾 `v2.5.59~v2.5.60`、「确认源已移删」按需外部对账 `v2.5.61~v2.5.62`、多目录全季智能合并与纯空壳自动回收 `v2.5.83`、**分类与归档准入统一 `work_cat == "archive"`、受阻精准模块导航及「人工复核无误 · 强制归档」安全闭环 `v2.5.84`**）。文件名已同步更新为最新版本号 `MediaFlow齐全可归档模块需求与交互方案-v2.5.84.md`。

全局导航展示顺序统一为：**待复核清理 → 异常剧集 → 缺集待补 → 齐全可归档 → 连载留源**。  
分流短路计算遵循**“清理优先、异常次之、播期先于缺集与归档”**原则：存在同内容可替代副本、同 TMDB ID 多目录规范化归并或归档已齐待清源动作时，优先进入「待复核清理」；具体身份／口径／读取争议进入「异常剧集」；已确认连载或待播季进入「连载留源」；完结且存在真实缺口进入「缺集待补」；**身份可信、确认已播毕、主集数覆盖齐全且源端有待迁入媒体内容的作品季进入本模块**。

各模块共用作品身份、期／集／段、内容类别、剪辑版本和技术副本的统一识别结果。作品季数、物理目录记录数、目录组数与计划迁移文件数严格分开统计，绝不混加。一次归档操作完成后自动执行双端实物后验、源剧根安全收尾、任务摘要刷新与五模块重算，无需另点整任务重新初检。

- **总体架构依据**：[五模块处理流程重构与智能清理方案 v2.5.44](MediaFlow五模块处理流程重构与智能清理方案-v2.5.44.md)
- **五模块关联方案**：[待复核清理](MediaFlow待复核清理模块需求与交互方案-v2.5.61.md) · [异常剧集](MediaFlow异常剧集模块需求与交互方案-v2.5.61.md) · [缺集待补](MediaFlow缺集待补模块需求与交互方案-v2.5.86.md) · [齐全可归档](MediaFlow齐全可归档模块需求与交互方案-v2.5.84.md) · [连载留源](MediaFlow连载留源模块需求与交互方案-v2.5.61.md)
- **跨模块联动方案**：[剧集订阅智能清理](MediaFlow剧集订阅智能清理需求与交互方案-v2.5.72.md) · [电影订阅智能清理](MediaFlow电影订阅智能清理需求与交互方案-v2.5.72.md) · [媒体整理智能修复](MediaFlow媒体整理智能修复需求与交互方案-v2.5.83.md)

---

## 一、模块职责、准入边界与跨模块互斥分流规范

### 1.1 核心职责与跨模块协作边界

用户从初检任务进入「齐全可归档」，可单季归档，也可勾选多季批量归档。系统通过 CloudDrive2（CD2）按季将源端媒体与伴生资产安全迁入归档成品库（`MF_ARCHIVE_ROOT`）。一部剧的 `S01` 已播毕齐全、`S02` 仍在连载时，支持单独归档 `S01` 并自动保护源剧根公共海报与 `S02` 目录。

| 业务情形 | 模块归属与协作分工 |
| :--- | :--- |
| 同集技术副本淘汰、同 TMDB ID 异名多目录全季归并（`v2.5.83`） | **待复核清理**先行执行「批量处理多目录问题」或「批量冗余清理」，完成后齐全季自动流转进入本模块 |
| 归档库已独立覆盖本季全部正片，源端仅剩重复残留或纯附件目录 | **待复核清理**执行附件递归保全与「清理已归档源端」，本模块不重复搬移已齐媒体 |
| 作品身份冲突、季映射未核实、大幅超号或跨作品混放 | **异常剧集**通过「手动复核」补充事实或修正映射；若人工核实文件无误，也可直接发起强制归档预检（`v2.5.84`） |
| 尚在连载或待首播（即便当前已播集数全齐） | **连载留源**继续保护源端接收后续更新，常规归档门禁自动拦截 |
| 已播毕但存在正片缺号，或处于补缺下载「等待入库／补缺后待复核」 | **缺集待补**完成推送与「核对入库」，经 `post_download_review` 完整实物复核唯一补齐后自动转入本模块 |
| **已播毕且主集数覆盖齐全（含合法附随扩展集或多内容独立保留版）** | **齐全可归档**执行只读预检、整季移入／增量合并／保留版本归档、先导片分流与源剧根空壳回收 |
| 媒体已确认迁入目标库，仅余源端空壳回收失败或未知附件留源 | 媒体记为 `archived` 移出归档待办，回执标记 `archived_pending_cleanup` 明示留源路径，绝不重搬已到位媒体 |

### 1.2 准入条件与分类—执行单一事实源铁律（v2.5.84 核心校准）

1. **一作品季一主卡**：以 `build_work_key(rec)`（优先 `tmdb:{id}:S{season}`，无 ID 时按规范剧名＋季号回退）聚合，满足全局互斥不变量 `review + anomaly + download + archive + airing + held == total_work_seasons`。
2. **分类器与归档接口准入完全对齐**：
   - 五模块分类器 `get_record_work_main_category(db, rec)` 与归档接口 `/api/archive/preview`、`/api/archive/execute` 共用同一权威准入判定：
     $$\text{常规可归档} \iff \text{status} \in \{\texttt{archive\_ready}, \texttt{period\_ready}\} \;\lor\; \text{work\_cat} == \texttt{"archive"}$$
   - **彻底消除“列表显示在齐全可归档、点击归档却报 `review_needed` 受阻”的割裂缺陷**：对于原始诊断状态保留为 `review_needed`，但经分类器权威核实属于以下两类合法完结齐全情形的作品季，常规预检与执行直接放行：
     - **附随相邻扩展集保留（`supplemental_coverage_preserved`）**：官方总集数 $\ge 6$、已核验作品身份绑定（`metadata_binding_verified=True`）、播期确认已完结（`broadcast_evidence.status == "finished"`）、主集数 `1..total` 100% 覆盖无重复，且超出总集数的本地扩展集满足数量 $\le 3$ 且最大集号 $\le \text{total} + 4$（典型案例如《奇迹少女 (2015) `[tmdbid=65334]`》S06 官方 26 集完结、本地含 `E01–E27` 共 27 集），不篡改官方总集数，预检摘要显式提示扩展集随本季一并迁入；
     - **多内容／多剪辑独立版本保留（`all_distinct_retained`）**：同集多文件经 `review_engine` 4D 内容等价判定属于不同内容版本（如正片＋会员加长版／纯享版／日记）而全部保留且 `can_clean=False`，直接作为齐全季整体迁入。
3. **补缺入库后的实物复核前置约束（v2.5.58）**：下载回执或 Emby 汇总数字本身不赋予归档资格；补缺到货后必须经 `review_after_download` 强读源端与归档端实物，确认无新增技术副本、无未识别错季文件且无 `post_download_review` 阻塞后，方可进入本模块。

| 当前已核实事实 | 五模块去向与展示动作 |
| :--- | :--- |
| 源季独立齐全，或源与唯一目标季互补后集齐 `1..total`，无未决副本 | 进入 **齐全可归档**（`archive_ready` 或 `work_cat == "archive"`），主按钮显示 **「一键归档」** |
| 已播毕分段综艺，整期或必需上下段齐全，需完整保留多版本 | 进入 **齐全可归档**（`period_ready`），主按钮显示 **「保留全部版本归档」** |
| 同作品季多个源目录指向唯一归档目标且集数互补无重叠、合集覆盖全季 | 自动聚拢入本模块同一归档计划（`_expand_complementary_records`），预检明示关联源目录数 |
| 同 TMDB ID 存在未确认多目录组，或仍含可淘汰的同内容重复副本 | 留驻 **待复核清理**，先执行全季智能归并或冗余清理 |
| 已完成归档（`archived`）、确认源已移删（`source_absent`）或已手动结案 | 归入终态 `TERMINAL_STATUSES`，严格从五模块待办列表与统计数字中剔除 |

---

## 二、工作台界面布局、卡片交互与预检确认流

### 2.1 模块视图与批量操作栏

- **顶栏模块卡片**：「齐全可归档」主卡展示当前任务就绪作品季总数；未加载、加载异常与核实为零时分别显示加载态、「重试加载」与明确空态说明（任务扫描中途停止时如实标注已分析范围，不将未扫描目录谎报为零）。
- **批量操作与最近回执**：
  - 列表顶部支持「全选」本页可归档剧季，实时统计已勾选季数，点击批量按钮打开统一预检确认窗；
  - 支持通过 `GET /api/archive/last-batch/{task_id}` 查看本任务最近一次归档批次的结构化回执（刷新页面或短时断线后仍可从 `OperationLog` 恢复查看逐季结果，并对部分归档／待核实项发起后续对账）。

### 2.2 季条目卡片信息与操作按钮

1. **卡片展示要素**：
   - 作品名称、季号、正片集数覆盖进度、`TMDB ID`、状态说明（含附随扩展集或多内容版本保留提示）；
   - 明确区分展示 **源端路径** 与 **预计归档目标路径**（目标路径仅代表计算落点，不代表已完成归档）；当存在关联互补目录时支持在详情内展开核对。
2. **卡片右侧按钮**：
   - **「详情」**：打开 `#gapFillDetailDialog` 诊断抽屉，首屏展示当前结论、下一步建议、资料总集数、播期状态、源端与目标路径及豆瓣多通道核验摘要，折叠区提供完整文件清单与历史记录；
   - **「一键归档」／「保留全部版本归档」**：打开 `#archiveModal` 执行单季只读预检；
   - **「确认源已移删」**（`v2.5.62` 由原「核对源目录」统一更名）：仅用于用户已在 CD2 / 115 外部手动移走或删除该源季目录后的只读核实；强读确认 `NotFound` 后记为 `source_absent` 移出待办（不伪称已归档，空目录或读取异常保留原卡片）。

### 2.3 归档预检确认弹窗（`#archiveModal`）与受阻精准导航（v2.5.84）

点击单季或批量归档后，前端绝不直接改写网盘，必须先调用 `POST /api/archive/preview` 渲染只读计划：

1. **可执行项展示**：
   - 批量时顶部汇总 `共选 N 季：可执行 X 季 · 受阻 Y 季` 及唯一 `plan_id`；主确认按钮动态显示 `确认归档 X 季`（部分受阻时显示 `确认归档可执行的 X 季`，零季可执行时禁用并显示 `无可执行季`）；
   - 每季卡片右上角标注模式徽章（`整季移入` / `增量合并至已有季` / `保留全部版本归档`），逐行展示：
     - `✓` 正片视频数、总文件数与实际目标路径（增量合并同时展示目标原有保留文件数）；
     - `• 附随扩展集一并归档`（当存在超出官方收录集数的保留扩展集时显式提示集号与官方集数）；
     - `• 先导特辑分流`（`E00 / SP01` 及伴生字幕迁往 `Season 0` 的数量与路径）；
     - `• 预期留源`（同名非视频伴生文件或源剧根留存项）与 `• 源剧根处理` 预告；
     - 折叠面板 `查看 N 个计划迁入文件及完整路径`（逐文件列出 `源文件名 → 目标完整路径`）。
2. **受阻项精准模块导航（根治跨模块空跳，v2.5.84）**：
   - 后端 `_resolve_blocked_next_step` 为每条预检项返回真实所属分类 `current_category` 与精准 `next_step`，前端结合当前所在页签 `currentCategory` 渲染唯一准确的引导按钮，**严禁在条目不属于异常模块时盲目渲染“前往异常剧集确认目标”**：

| 受阻项真实所在模块（`current_category`） | 预检弹窗内渲染的导航动作 |
| :--- | :--- |
| `anomaly`（且当前不在「异常剧集」） | 红色按钮 **「前往『异常剧集』复核」**（关闭弹窗并切换至 `anomaly`） |
| `review` 或未确认多目录组（且当前不在「待复核清理」） | 靛蓝按钮 **「前往『待复核清理』处理」**（关闭弹窗并切换至 `review`） |
| `download`（且当前不在「缺集待补」） | 蓝色按钮 **「前往『缺集待补』查看」**（关闭弹窗并切换至 `download`） |
| 与当前所在页签相同（如本就在 `archive` 因目标同名冲突受阻） | 边框按钮 **「查看本季详情与依据」**（关闭预检窗并直接打开本季详情抽屉） |
| 源或目标目录瞬时读取异常（`state == "needs_recheck"`） | 底部显示 **「重新预检」**（`#archiveModalRetryBtn`），重读最新云盘状态 |

---

## 三、只读预检引擎、三种迁入模式与源剧根零残留收尾

### 3.1 前置零写入安全门禁顺序（A05, A07）

无论预检还是正式执行，系统严格遵循**“全部前置检查 100% 通过后才允许发出首个写操作”**铁律（严禁在发现同季冲突或多候选目录前提前创建目录或提前搬移 `E00` 特辑）：

$$\text{① 终态与路径边界} \to \text{② 分类/冲突/目录组门禁} \to \text{③ 源季强读与分段检查} \to \text{④ 跨季/平铺隔离检查} \to \text{⑤ 特辑与目标同季歧义/冲突检查} \to \text{⑥ 开始写入}$$

1. **受控根路径与父子关系硬校验**：要求已配置 `MF_ARCHIVE_ROOT`；若配置了 `MF_SOURCE_ROOT`，源季路径必须严格位于 `source_root/` 下，目标季路径必须严格位于 `archive_root/` 下；拒绝源与目标相同、源与目标互相包含、或源剧根等于目标父目录的任何越界请求。
2. **源目录强读与先导特辑只读剥离（`_identify_specials_in_season`）**：调用 `cd2.list_files(..., force_refresh=True)` 强读源季目录；只读识别集号为 `[0]` 或匹配 `SxxE00 / E00 / SP01` 的先导特辑视频及其同文件干（含 `-thumb`）伴生字幕、NFO、缩略图，将其与常规正片 `regular_contents` 分离规划。
3. **跨季与平铺未隔离拦截（`_find_cross_season_or_unisolated_items`）**：若源季为平铺剧根（`norm_src_show == norm_src_season`）且包含子目录，或常规视频解析出其他正片季号（`season_number != rec.season_number`），前置暂停并列出具体冲突项。
4. **目标候选目录歧义拦截（A05）**：
   - 若目标剧根下同时存在多个匹配本季号或同名季目录（如同时存在 `Season 1` 与 `Season 01`，`len(candidate_seasons) > 1`），立即安全停止并回传全部 `candidate_paths`；
   - 若含待分流特辑且目标剧根存在多个特别篇候选目录（`Season 0 / Season 00 / Specials / 特别篇` 中同时存在 $\ge 2$ 个），或唯一特别篇目录中已存在同名特辑视频，立即安全拦截。

### 3.2 三种物理归档模式

前置门禁全部通过后（如有待迁移先导特辑，先调用 `_migrate_specials_to_season_0` 迁入目标 `Season 0`），按目标目录实况分流执行：

| 归档模式（`mode`） | 触发条件 | 物理执行策略与防覆盖机制 |
| :--- | :--- | :--- |
| **整季移入**<br/>（`full_season_move`） | 目标剧根下尚无本季目录，且非常规保留版本综艺 | 写入 `in_progress` 操作日志后，对整个源季目录调用 `cd2.move_file(source_season_path, target_parent, skip_existing=True)`，并在内存更新 `target_parent_files_cache`（使同剧第 2 个互补源目录自动平滑走增量合并） |
| **增量合并至已有季**<br/>（`incremental_merge`） | 目标剧根下已存在唯一同季目录（如已有 `Season 01`，或同剧第 2 个互补目录汇入） | 先调用 `_find_same_episode_conflicts` 核验目标季无同名视频（`duplicate_videos`）且无同集异名冲突（常规模式）；随后逐文件将源端独有文件 `move_file(..., skip_existing=True)` 迁入已有目标季。目标已存在的同名非视频伴生文件（如 `sub.srt`）跳过移动、双端保留并记入 `expected_retained`；源季视频全部迁出且无残留附件时自动删除源季空壳目录 |
| **保留全部版本归档**<br/>（`preserve_versions`） | `rec.status == "period_ready"`（分段综艺多版本共存就绪） | 执行前实时调用 `run_inspection` 二次复核期段状态与目标路径未漂移，通过后以 `skip_existing=True` 整季迁入，完整保留上下段、整期版及全部伴生文件 |

### 3.3 源剧集根目录公共刮削资产同步与空壳回收（A01, A12）

同一部剧在当前批次的所有选中季度处理完毕后，若存在成功完成的常规归档且非平铺目录，自动执行一次 `_inspect_and_cleanup_source_show_dir(cd2, norm_src_show, norm_target_parent)`：

1. **多季留源保护**：强读源剧根目录，若仍存在其他季度子目录（如连载中的 `Season 2`）、其他任意子目录或任何视频文件，**立即保留源剧根目录及全部公共海报／NFO**，在回执 `cleanup_result` 中如实写明留源原因。
2. **末季公共资产同步**：若源剧根已无任何子目录与视频，且剩余文件均为受白名单识别的公共刮削资产（`_is_known_show_public_asset`：`tvshow.nfo`、`poster / folder / cover / fanart / clearart / clearlogo / logo / banner / landscape / backdrop / thumb / theme / season*` 等图片与元数据），逐个以 `skip_existing=True` 迁入目标剧根（绝不覆盖目标库已有同名刮削文件）。
3. **未知附件保全与空壳销毁后验**：若源剧根存在未知非刮削附件（如压缩包、说明文档等），安全保留源剧根并标记 `archived_pending_cleanup`；仅当复查源剧根已彻底清空（或仅余因目标已存在而跳过的已知同名刮削资产）时删除源剧根，并**再次强读上级父目录确认源剧根路径已不可见**，杜绝“回执成功但空目录仍在”的假清理。

---

## 四、「人工复核无误 · 强制归档」安全闭环规范（v2.5.84 新增）

### 4.1 设计定位与适用场景

针对以下**经用户人工核对确认媒体文件无误，但常规自动化规则因保守防线予以拦截**的真实业务场景，系统提供受控的 **「人工复核无误 · 强制归档」** 通道：

- 冷门剧集、纪录片、海外短剧在 TMDB / 豆瓣缺少分集条目或官方总集数录入滞后／失准（状态处于 `unknown / archive_check / period_review`）；
- 本地扩展集超出 `supplemental_coverage_preserved` 默认自动放行阈值（如超出 $>3$ 集），但用户已核实全部为本季正式加更内容；
- 季内包含上下段命名文件（如 `上/下`、`Part 1/2`）或文件名带有特殊跨季标签，用户确认无需拆分清理直接整季入库；
- 归档库已有 `1080p` 版本，源端新收 `4K 导演剪辑版`（不同文件名），用户明确希望合并至同一目标季双版本共存，而非作为冲突拦截。

### 4.2 软性放行项 vs 物理安全硬红线对照表

强制归档遵循**“放行分类与元数据软门禁、死守网盘物理防覆盖硬红线”**的第一性原理：

| 检查维度 | 常规归档（`force_archive=False`） | 强制归档（`force_archive=True`） |
| :--- | :--- | :--- |
| **软性状态门禁**（`review_needed / unknown / period_review / archive_check / identity_review / download_needed` 等非终态） | 拦截（返回 `can_force_archive=True`） | **允许放行**（摘要标注 `状态限制(...)`） |
| **作品身份／归属告警**（`conflict_info.blocking`）与 **未确认同 ID 多目录组** | 拦截（返回 `can_force_archive=True`） | **允许放行**（按当前记录明确预览的 `target_path` 迁入） |
| **分段编号文件**（`segment_number`）与 **保留版本汇入已有目标季** | 拦截（返回 `can_force_archive=True`） | **允许放行**（完整保留各分段／版本文件迁入） |
| **独立季目录内跨季文件名标记**（`not is_flat_show`） | 拦截（返回 `can_force_archive=True`） | **允许放行**（摘要标注 `跨季命名标记(...)`） |
| **已有目标季同集异名文件**（`same_ep_conflicts`，如不同分辨率／剪辑版且文件名不同） | 拦截（返回 `can_force_archive=True`） | **允许放行**（走 `incremental_merge` `skip_existing=True` 双版本并存，零覆盖） |
| **终态记录**（`archived / source_absent / manually_handled` 等） | **硬拦截**（`can_force_archive=False`） | **硬拦截**（严禁重复搬移终态记录） |
| **受控根路径越界、源与目标相同／互含／同剧根** | **硬拦截**（`can_force_archive=False`） | **硬拦截**（100% 拒绝越界路径） |
| **源季目录读取失败，或未检测到任何有效视频文件** | **硬拦截**（`can_force_archive=False`） | **硬拦截**（严禁将空壳或纯字幕目录虚报归档） |
| **平铺剧根目录含未隔离子目录**（`is_flat_show` 含 `子目录:...`） | **硬拦截**（`can_force_archive=False`） | **硬拦截**（防止整目录移动误带无关子目录） |
| **目标存在多个同季候选目录或多个 `Season 0` 候选目录** | **硬拦截**（`can_force_archive=False`） | **硬拦截**（落点歧义必须先消解，零写入） |
| **目标季或目标 `Season 0` 已存在同名视频文件**（`duplicate_videos / dup_s0`） | **硬拦截**（`can_force_archive=False`） | **硬拦截**（杜绝同名覆盖或静默跳过导致源视频残留） |
| **预检后源端文件列表变化或目标新出现同季目录**（快照漂移） | **硬拦截**（提示重新预检） | **硬拦截**（仅允许搬移用户刚刚预检确认过的确切文件快照） |

### 4.3 四处前端交互入口、二次确认流与全链路审计

为保护「连载留源」模块只读纯净性（连载卡片与连载详情**严禁**挂载任何写操作或强制归档按钮），强制归档在前端精确开放以下四处入口，且**全部必须先经过 `#archiveModal` 只读预检清单二次确认**：

1. **归档预检弹窗（`#archiveModal`）**：当常规预检存在 `can_force_archive=True` 的受阻项时，受阻卡片内与弹窗底部同步显示琥珀色按钮 **「已复核无误 · 强制归档预检」**（`enableForceArchiveInModal()`）。点击后重调 `POST /api/archive/preview`（`force_archive: true`），弹窗顶部亮起琥珀色安全提示横幅，模式徽章切换为 `强制归档 · 整季移入` / `强制归档 · 增量合并至已有季`，摘要明示已放行的软门禁项，主确认按钮切换为琥珀色 **「确认强制归档 N 季」**。
2. **本季详情弹窗（`#gapFillDetailDialog`）**：对任意非连载（`!isAiring && !airing_protected`）、非终态且含本地视频的剧季，在顶部「当前结论」卡片内提供 **「已复核无误 · 强制归档」**（`openForceArchiveFromDetail`）。
3. **手动复核弹窗（`#manualConfirmModal`）**：在「异常剧集」打开手动复核面板时，顶部操作栏提供 **「已复核无误 · 强制归档」**（`#mcForceArchiveBtn`），无需强填外部缺失的资料集数即可直接进入强制归档预检。
4. **「待复核清理」单季卡片**：对非连载、非 `already_archived` 且含本地视频的待复核卡片，提供次级快捷按钮 **「强制归档」**（`openForceArchivePreview`），支持保留全部现有文件直接归档。
5. **持久化审计可追溯**：执行强制归档时（支持请求显式传 `force_archive=True` 或通过 `plan_id` 从预检计划自动继承），`OperationLog.detail_json` 强制记录 `"force_archive": true`，响应回执携带 `"force_archive": true`，且 `SeasonRecord.status_detail` 自动追加 `（人工复核强制归档）`。

---

## 五、同剧互斥锁、双端实物后验、六态回执与断点续传恢复

### 5.1 规范路径有序双锁与预检快照防漂移（A04, A11）

1. **有序双锁防死锁并发控制**：`execute_archive` 按 `(norm_src_show, norm_target_parent)` 将待执行记录按剧分组，每组执行前通过 `_acquire_show_locks([norm_src_show, norm_target_parent])` 按规范化路径字典序认领源剧根与目标剧根 `asyncio.Lock`：
   - 同一部剧的多季或多互补源目录严格串行执行，最后统一执行一次源剧根收尾；
   - 不同任务或双击请求若指向同一源剧或同一目标剧，在锁内二次检查 `rec.status == "archived"` 与 `_COMPLETED_PLAN_SEASONS[(plan_id, rec.id)]`，重复提交直接幂等返回既有回执，零重复移动。
2. **预检快照一致性校验**：若执行请求绑定了 `plan_id`，锁内强读源季与目标父目录后立即比对 `saved_plan.snapshot`（`source_file_names` 与 `target_season_exists`）；一旦预检后云盘新增／减少了文件或新出现了目标季目录，立即阻断并提示 `请重新预检`。

### 5.2 双端实物后验与六态结构化逐季回执（A08, A10, A12, A14）

系统绝不单凭 `cd2.move_file` 返回 `success=True` 就宣告归档完成，每次移动后必须执行双端强读后验，并显式区分“目录为空”与“读取报错”：

- **整季移动后验**：先强读源剧根确认源季目录已移出，再强读目标季目录核对 `planned_regular_names` 全部文件均已到位；
- **增量合并后验**：强读源季目录确认无正片视频残留；若无任何残留文件则清理源季空壳目录。

每季执行返回统一的 `_make_receipt` 结构化回执，前端严格按 `outcome` 枚举驱动状态更新（严禁靠正则解析提示文本猜结果）：

| 回执状态（`outcome`） | 判定条件 | 数据库状态与用户下一步操作 |
| :--- | :--- | :--- |
| `archived` | 计划迁入视频与配套全部核实到位，源季已移出／清空，源剧根已收尾或按预期留源（如仍有连载 S02 或同名伴生字幕） | `rec.status = "archived"`，移出五模块待办；刷新后主卡消失（同作品季若仍有未处理记录则重算） |
| `archived_pending_cleanup` | 目标端正片已 100% 核实到位，但源季空壳删除失败、源剧根回收失败或存在未知非视频附件留源 | `rec.status = "archived"`（媒体不再重搬），回执 `next_step = "check_retained"` 明示留源清单供核查 |
| `partial_archived` | 增量合并后源端仍残留部分正片视频，或整季移动后目标季缺失部分计划文件 | `rec.status = "review_needed"`，`OperationLog` 记 `partial_archived`，引导点击 **「核对剩余项」** 断点续传 |
| `archive_unverified` | 移动指令已发出，但末次强读源端或目标端目录失败，或网盘一致性延迟导致源季仍可见 | `rec.status = "review_needed"`，`OperationLog` 记 `archive_unverified`，暂缓重跑整季，引导点击 **「核对两端」** |
| `blocked` | 前置安全门禁拦截（冲突、多候选、路径越界、快照漂移或移动明确报错），确认零媒体破坏 | 保留原待办或更新冲突详情，按 `next_step` 精准引导复核或重新预检 |
| `cancelled_not_started` | 批量流水线中用户点击「停止」，尚未派发至后端的队列剩余季 | 保留原就绪状态与勾选资格；已派发季独立结算并汇总至本批次回执 |

### 5.3 断点对账与剩余项续传（`POST /api/archive/reconcile`，A09）

针对 `partial_archived`（部分归档）与 `archive_unverified`（结果待核实）条目，用户点击「核对两端」或「核对剩余项」触发 `reconcile_archive_season`：

1. 读取该季最近一条 `archive / archive_incremental_merge` `OperationLog` 中的 `planned_files` 与目标季路径，加锁后强读源季与目标季实物清单；
2. 计算已到达目标文件 `arrived_files` 与仍在源端未到位文件 `remaining_in_source`；
3. 当 `resume_remaining=True` 且存在 `remaining_in_source` 时，**仅对尚未到位的剩余文件逐个执行 `move_file(..., skip_existing=True)` 续传**，绝不覆盖目标已有文件，也不重复搬移已到位项；
4. 续传后二次强读双端：若源端已无待迁正片视频且目标端视频到位，自动回收空源季目录与空源剧根，将 `SeasonRecord.status` 更新为 `archived`、日志标记 `completed` 并刷新任务汇总。

---

## 六、核心接口契约与架构模块落点

### 6.1 核心 REST 接口一览

| 接口路径 | 方法 | 请求模型／关键参数 | 核心职责与返回契约 |
| :--- | :---: | :--- | :--- |
| `/api/archive/preview` | `POST` | `ArchivePreviewRequest`<br/>`season_record_ids`, `task_id`, `force_archive: bool = False` | **只读归档预检**：扩展互补目录，执行前置门禁与目标探查，缓存 `_ARCHIVE_PREVIEW_PLANS[plan_id]`，返回 `executable_ids`, `blocked_ids` 及每季含 `current_category`, `can_force_archive`, `mode_label`, `regular_files_to_move`, `special_files_to_move`, `expected_retained`, `summary_lines`, `snapshot` 的计划明细 |
| `/api/archive/execute` | `POST` | `_ArchiveRequest`<br/>`season_record_ids`, `plan_id`, `batch_id`, `force_archive: bool = False` | **执行归档迁移**：继承或校验 `force_archive` 与 `plan_id` 快照，按剧加有序双锁，执行特辑分流、整季移动／增量合并／保留版本归档、双端后验与源剧根空壳回收，返回 `ArchiveExecuteResponse`（含逐季 `results` 与六态 `summary`） |
| `/api/archive/last-batch/{task_id}` | `GET` | 路径参数 `task_id` | **查询最近批次结果**：优先读内存 `_LAST_ARCHIVE_BATCH_BY_TASK`，未命中则回溯 `OperationLog` 聚合最近 `batch_id` 的逐季结构化结果，支撑刷新／断线恢复查看 |
| `/api/archive/reconcile` | `POST` | `ArchiveReconcileRequest`<br/>`season_record_id`, `resume_remaining: bool = False` | **双端对账与断点续传**：核实 `partial_archived / archive_unverified` 条目的两端实况，支持仅续传源端剩余未到位文件并自动收尾转 `archived` |
| `/api/tasks/{task_id}/seasons/{record_id}/reconcile-source` | `POST` | 路径参数 `task_id`, `record_id` | **确认源已移删对账**：零写入强读核实源目录是否已在网盘外部被移走或删除，确认 `NotFound` 后记为 `source_absent` 移出待办 |

### 6.2 源码模块落点

- **五模块权威分流与扩展集／多版本保留判定**：[app/core/season_classifier.py](app/core/season_classifier.py)（`classify_work_season_records`、`get_record_work_main_category`、`supplemental_coverage_preserved`、`consolidate_seasons_to_work_records`）
- **归档预检、强制归档、增量合并、特辑分流、源剧根收尾与断点续传**：[app/api/archive.py](app/api/archive.py)
- **多目录组门禁、全季智能合并与纯空壳递归自愈**：[app/core/directory_governance.py](app/core/directory_governance.py) · [app/core/directory_merge.py](app/core/directory_merge.py)
- **初检诊断、归档目标路径推导与分段综艺核验**：[app/core/inspector.py](app/core/inspector.py)（`compute_archive_target`） · [app/core/segments.py](app/core/segments.py)
- **外部移删只读对账与清源附件保全**：[app/services/source_presence.py](app/services/source_presence.py) · [app/core/source_cleanup_assets.py](app/core/source_cleanup_assets.py)
- **前端归档预检弹窗、精准模块跳转、四处强制归档入口与批次流水线**：[frontend/index.html](frontend/index.html)（`openArchivePreviewModal`、`enableForceArchiveInModal`、`openForceArchivePreview`、`openForceArchiveFromDetail`、`openForceArchiveFromManualConfirm`、`confirmArchiveDone`、`reconcileArchiveSeason`）

---

## 七、核心验收标准（A01–A18）与自动化测试映射索引

### 7.1 全场景验收矩阵（A01–A18）

| 编号 | 验收场景 | 可观察通过标准 |
| :---: | :--- | :--- |
| **A01** | 同剧多季部分完结归档（`S01` 完结、`S02` 连载） | 仅选 `S01` 归档成功并迁入目标库；源端 `S02` 与剧根 `poster.jpg` 完整留源且回执写明原因；刷新后 `S01` 移出待办、`S02` 仍在「连载留源」 |
| **A02** | 已有唯一目标季增量合并与同名字幕双端保留 | 源 `Season 1` 含 `E03 + sub.srt`，目标 `Season 01` 已有 `E01–E02 + sub.srt`；预检识别为 `增量合并至已有季`，执行后 `E03` 落入 `Season 01`（不建第二个同季目录），两份 `sub.srt` 双端保留且登记 `archived` |
| **A03** | 季内先导特辑（`E00 / SP01`）及伴生文件智能分流 | 特辑不抵扣正片集数；归档时自动将 `E00` 及同干字幕／缩略图迁入目标 `Season 0`（复用既有特别篇目录或新建），正片迁入 `Season N` |
| **A04** | 分段综艺保留版本归档与预检快照漂移保护 | `period_ready` 完整保留上下段与整期版；预检生成 `plan_id` 后若源端新增文件或目标新出现同季目录，执行立即拦截并要求重新预检 |
| **A05** | 目标同名视频冲突与多候选季目录前置零写入拦截 | 目标存在同名视频或同时存在 `Season 1` 与 `Season 01`（或多个 `Season 0` 候选）时，在首个写操作前拦截并列出候选／冲突文件，源端 `E00` 与正片 100% 原地不动 |
| **A06** | 同作品季互补多源目录聚拢与有意共存隔离 | 指向同一目标且集数互补无重叠的多个源目录自动纳入同一计划（首目录整移、次目录增量合并）；未确认多目录组转「待复核清理」；标记“有意共存”的收藏保持各自独立目标 |
| **A07** | 同集异名版本冲突（常规模式）与平铺跨季拦截 | 常规模式下目标同集异名文件或平铺剧根含子目录／跨季视频时安全拦截零写入，不影响同批次其他独立合格剧季执行 |
| **A08** | 整季移动末次读源失败或目标缺计划文件防虚报 | 移动回执成功但末次读源异常记 `archive_unverified`，目标缺失任一计划文件记 `partial_archived`，两者均严禁登记为 `archived` |
| **A09** | 中断后 `reconcile` 双端对账与仅续传剩余文件 | 部分移动后调用 `/api/archive/reconcile`（`resume_remaining=True`），仅对源端未到位文件续传，零覆盖、零重复搬移，全齐后自动完成收尾并转 `archived` |
| **A10** | 批量流水线中途停止与跨任务切换保护 | 批量选 3 季，第 1 季完成、第 2 季执行中点停止：第 1 季计成功，第 2 季独立结算，第 3 季记 `cancelled_not_started` 保留勾选资格；执行期切换任务不污染其他任务视图 |
| **A11** | 同计划重复提交幂等与跨请求同剧互斥锁 | 同一 `(plan_id, record_id)` 重复提交直接返回 `already_done=True` 且零额外 `move_file` 调用；并发指向同一源或目标剧根时按序串行无死锁 |
| **A12** | 源剧根刮削资产同步、空壳销毁后验与未知附件留源 | 末季归档自动同步 `tvshow.nfo / poster.jpg` 并强读父目录确认空剧根已消失；若空壳删除失败或含未知附件，媒体记 `archived`、回执标 `archived_pending_cleanup` 不触发重搬 |
| **A13** | 终态剔除与五模块不变量守恒 | `archived / source_absent / manually_handled` 记录不进入待办且不可重复归档；五模块主卡数与暂不处理数之和严格等于活跃作品季总数 |
| **A14** | 批量混合结果展示与刷新异常韧性 | 一季成功、一季受阻时分别清晰展示，不因整体 `ok=True` 吞没受阻原因；`last-batch` 支持刷新后恢复查阅 |
| **A15** | 附随扩展集／多内容保留季常规归档直通（`v2.5.84`） | 如《奇迹少女》S06（官方 26 集已播毕、本地 `1..27` 集完整无重复，`work_cat == "archive"`），无需强制归档即可通过常规预检与执行，预检摘要明示附随扩展集 `[27]` 一并迁入 |
| **A16** | 预检受阻项精准模块导航（`v2.5.84`） | 受阻项位于 `review` 引导前往「待复核清理」，位于 `anomaly` 引导前往「异常剧集」，位于 `download` 引导前往「缺集待补」，已在当前模块提供「查看本季详情与依据」，绝不跨模块空跳 |
| **A17** | 人工复核无误后强制归档与双版本增量共存（`v2.5.84`） | `force_archive=True` 放行 `unknown / review_needed`、上下段文件及同集异名多剪辑版（如 `1080p` + `4K DirectorCut` 同季零覆盖并存），`OperationLog` 与 `status_detail` 完整写入强制归档审计标记 |
| **A18** | 强制归档下的物理安全硬红线零妥协（`v2.5.84`） | 即使显式传入 `force_archive=True`，遇到目标同名视频冲突、多候选目标季、越界路径、平铺未隔离子目录、无视频空目录或预检后文件漂移时，依然 100% 拦截且网盘零写入 |

### 7.2 自动化回归测试套件映射

- [tests/test_v2584_archive_alignment_and_force.py](tests/test_v2584_archive_alignment_and_force.py)（A15–A18：《奇迹少女》S06 附随扩展集常规归档复现、三模块受阻精准导航、强制归档双版本共存与 `OperationLog` 审计、四大物理安全硬红线零写入拦截）
- [tests/test_archiver.py](tests/test_archiver.py)（A01–A02、A05、A07–A09、A11、A13：路径推导、多季留源、增量合并同名字幕保留、E00 前置零写入拦截、丢文件部分归档与 `reconcile` 仅续传剩余项、终态剔除）
- [tests/test_cmctv_and_specials.py](tests/test_cmctv_and_specials.py)（A03：压制组防误杀与季内 `E00 / SP01` 先导特辑分流 `Season 0`）
- [tests/test_governance.py](tests/test_governance.py) · [tests/test_segments_auto_verify.py](tests/test_segments_auto_verify.py)（A04、A05：分段综艺自动核验、保留版本归档与增量读失败／多候选拦截）
- [tests/test_v2583_merge_empty_shell_and_batch_split.py](tests/test_v2583_merge_empty_shell_and_batch_split.py) · [tests/test_v2551_review_closure.py](tests/test_v2551_review_closure.py)（A06、A12：多目录全季智能合并、纯空壳／`Season 0`／系统垃圾回收、防 `cat:` 污染与 115/CD2 退避重试）
- [tests/test_v2558_post_download.py](tests/test_v2558_post_download.py) · [tests/test_v2561_source_presence.py](tests/test_v2561_source_presence.py)（补缺到位后完整实物复核门禁、「确认源已移删」外部消失只读对账）
- [tests/test_season_classifier.py](tests/test_season_classifier.py) · [tests/audit_frontend.js](tests/audit_frontend.js) · [tests/test_frontend_logic.js](tests/test_frontend_logic.js)（五模块互斥分流、连载只读契约与全量 DOM／事件映射审计）

---

## 附录：关键版本演进脉络（v2.5.36 ~ v2.5.84）

| 版本 | 日期 | 本模块关键演进摘要 |
| :---: | :---: | :--- |
| **v2.5.84** | 2026-11-28 | **分类准入统一、受阻精准导航与「人工复核无误 · 强制归档」**：统一认可权威分类 `work_cat == "archive"`（根治含附随扩展集的完结齐全季在预检／执行被 `review_needed` 误拦）；受阻项按真实 `current_category` 精准导航杜绝误跳「异常剧集」；新增四入口只读预检二次确认的强制归档通道（放行软性状态／分段／同集异名多版本，坚守同名零覆盖与双端核验硬红线）；本方案全面重构并更名为 `v2.5.84` |
| **v2.5.83** | 2026-11-27 | **前置多目录全季智能合并与纯空壳自动回收**：修复非 `S01` 多季目录合并卡死，支持递归纯空壳、`Season 0` 特辑与系统垃圾安全收尾及 115/CD2 短退避重试，「待复核清理」拆分为多目录与冗余清理双批量入口，顺畅上游齐全入列链路 |
| **v2.5.74–80** | 2026-11-24~25 | **豆瓣三通道熔断回切与续季跨年容差**：强化初检完结播期与官方集数核验准确度，减少已完结剧季因单源元数据缺失被误挡在归档门外 |
| **v2.5.61–62** | 2026-11-21 | **外部移删只读对账与按钮去歧义更名**：新增按需强读对账接口并将卡片按钮统一更名为「确认源已移删」，外部已删源目录确认 `NotFound` 后记 `source_absent` 退出待办，不伪造归档完成 |
| **v2.5.59–60** | 2026-11-20 | **清源附件目录递归保全与剧集外层真实回执**：打通归档已齐后季内及剧集外层「合并保留」纯附件外壳向归档端独立位置的零覆盖保全与双端核验，细化外层保留与移除回执 |
| **v2.5.57–58** | 2026-11-20 | **补缺到位完整实物复核与人工确认离线重算**：「核对入库」后强制重跑本季身份、版本与副本实物分析，唯一补齐才放行入列可归档；有效人工确认保存后即时重算转入本模块 |
| **v2.5.53** | 2026-11-20 | **附随相邻扩展集独立保留与统一详情首屏**：确立完结且正片全齐时少量相邻扩展集（如综艺加更集）不改官方总数、不误挂异常的保留规则；统一详情弹窗首屏结论与源／目标路径标注 |
| **v2.5.51–52** | 2026-11-20 | **五模块展示顺序定型与归并防污染加固**：全局导航定型为 `待复核清理 → 异常剧集 → 缺集待补 → 齐全可归档 → 连载留源`；根治 `cat:` 内部键混入路径，强化同名禁覆盖与空目录强读后验 |
| **v2.5.45** | 2026-11-15 | **五模块「清理优先」重构与 4D 内容等价模型**：建立作品季单主卡归并、独立多剪辑版本保留（`all_distinct_retained`）消除分流循环与同剧目录治理门禁 |
| **v2.5.36–44** | 2026-11-10~15 | **归档预检快照、增量合并与断点续传奠基**：落地 `preview / execute / last-batch / reconcile` 闭环、季内文件级增量合并（F08）、`E00` 特辑分流 `Season 0`、同剧双锁互斥与源剧根零残留清理 |
