# MediaFlow 项目审查报告

审查日期：2026-11-09。对象：`E:/GoogleAI/ls/mediaflow` 当前工作区，版本常量 2.5.4。

## 总体判断

项目已形成可用的模块划分：异步 API、外部客户端、初检/复核引擎、数据库记录和统一前端批处理。现有测试对常见集号识别、重复版本选择、订阅分类有一定覆盖。

当前不宜把“源端自动删除”“一键清理残留”视为已经具备可靠保护的操作。最应优先解决的是删除前的证据验证、失败时停止操作、外部结果与数据库状态一致性；其次是已经明确损坏的补缺接口契约和任务生命周期。界面与说明中的“100%保留”“彻底免疫超时”“安全清理”等承诺超出了实际实现提供的保障。

以下 P0 表示存在数据丢失路径，应阻断相关删除功能的交付；并不表示本次发现了用户数据实际丢失。P1 为核心功能错误或重要保护缺口，P2 为稳定性和维护质量问题。

## 范围与验证边界

- 阅读了用户指定规范、README、后端核心模块、前端、部署/启动配置及测试代码，按实际调用链核对。
- 工作区原有 8 个已修改文件，以及未跟踪的规范文档和 scratch 目录；本次未修改项目源码、已有改动或真实数据。
- 验证环境为本机 Python 3.11.15、Node 22.23.2；容器声明为 Python 3.12。本次没有构建 Docker，也没有验证真实 CloudDrive2/MoviePilot 版本兼容性。
- 未读取真实秘密配置，未调用真实网盘的移动/删除、MP 下载/退订等操作。所有新增行为复现使用模拟客户端、模拟数据库或进程内 HTTP 请求。
- 本次只在独立输出目录生成本报告、复现脚本、结果 JSON 和隔离日志。

## 一、有问题的部分

### F01 · P0：归档后“空壳”判断不成立，可能连带删除未迁移文件

位置：[archive.py:170](E:/GoogleAI/ls/mediaflow/app/api/archive.py:170)、[archive.py:181](E:/GoogleAI/ls/mediaflow/app/api/archive.py:181)、[archive.py:528](E:/GoogleAI/ls/mediaflow/app/api/archive.py:528)。

代码只检查有没有可识别的 Season 子目录或根层视频。`Extras`、未识别季名、自定义附件目录都不会阻止删除父目录。归档过程迁移海报等文件失败后只记录警告，随后仍删除源剧目录；迁移返回 `success:false` 也未检查。残留清理中的父目录删除同样只看 Season/视频，不保护其他资产。

**复现：**源端只剩非空 `Extras` 子目录时，函数仍调用删除源剧目录；模拟海报迁移抛异常后，同样执行父目录删除。

**建议与验收：**只有重新列出后确认为真正空目录，才能自动删除父目录。任何资产迁移失败、未知目录、未知文件都保留并提示；公共资产同名冲突应有明确保留策略。添加“附件目录存在”“资产移动失败/返回失败”“再次列目录失败”三类真实路由级测试，均应断言没有调用父目录删除。

### F02 · P0：源端残留清理缺少后端安全门禁，上游错误还能生成删除资格

位置：[inspector.py:374](E:/GoogleAI/ls/mediaflow/app/core/inspector.py:374)、[archive.py:460](E:/GoogleAI/ls/mediaflow/app/api/archive.py:460)、[frontend/index.html:1831](E:/GoogleAI/ls/mediaflow/frontend/index.html:1831)。

读取归档季目录失败时，初检仍把源记录设成 `already_archived`；源端集号为空时也可能走同一状态。这相当于把“无法验证”当成“已验证可删”。`clean_source_redundant()` 拿到任意 SeasonRecord 就执行删除，没有限制记录状态、校验源目录边界、重新读取目标并验证覆盖关系。

前端异常分类同时容纳已归档残留、未知元数据和集数拆分异常，但批量按钮统一调用源端残留删除。勾选未知元数据/拆分异常也会进入这一接口，后端无法兜底。

**复现：**目标季读取抛异常，产出 `already_archived`；将 `download_needed` 记录直接送入清理函数，仍发起删除。

**建议与验收：**读取错误必须进入独立的 `verification_failed` 状态；删除前在后端验证源路径位于受控源根的严格子目录、源目标不重合、当前目标可读且覆盖源正片，并验证需保留的字幕/附件。前端仅允许已验证残留被批量勾选。对过期记录和目录内容变更重新生成清理计划，验证失败时零删除。

### F03 · P1：补缺流水线请求与响应均不符合后端契约

位置：[frontend/index.html:2572](E:/GoogleAI/ls/mediaflow/frontend/index.html:2572)、[moviepilot.py:444](E:/GoogleAI/ls/mediaflow/app/api/moviepilot.py:444)、[download_service.py:422](E:/GoogleAI/ls/mediaflow/app/services/download_service.py:422)。

前端提交 `{record_ids:[...]}`，后端必需字段是 `season_record_ids`，会返回 422。前端读取 `res.details[0].success` 等字段，但后端返回 `submitted`、`ok`、`errors`。即使只修正请求字段，后端返回 `ok:false` 且没有 details 时，前端仍落入“处理完成/成功”分支，日志和计数继续失真。

**复现：**用实际 `_GapFillRequest` 解析前端请求体，得到缺少 `season_record_ids` 的校验错误。

**建议与验收：**统一请求及返回模型；明确区分成功、已在下载队列、无资源、外部拒绝、参数错误；用实际前端调用函数与真实 FastAPI 路由做契约测试，覆盖 422、`ok:false` 和多条下载提交结果。

### F04 · P1：删除已执行并提交，审计日志字段错误却向用户报告删除失败

位置：[archive.py:621](E:/GoogleAI/ls/mediaflow/app/api/archive.py:621)、[archive.py:645](E:/GoogleAI/ls/mediaflow/app/api/archive.py:645)、[operation.py:27](E:/GoogleAI/ls/mediaflow/app/models/operation.py:27)。

`delete_source_show()` 先删除网盘目录、删除季记录并 commit，随后构造 OperationLog 时使用不存在的 `operation_type`、`details_json`；模型字段实际为 `action`、`detail_json`。结果必然在该构造阶段报错，返回 `ok:false`，但先前的删除已经完成。

**复现：**模拟调用得到 `'operation_type' is an invalid keyword argument for OperationLog`，此前数据库已提交一次。

**建议与验收：**修正字段并重排操作日志流程，至少在外部执行前创建操作意图，执行后记录结果。日志应保留已删记录的快照，避免依赖被删除季记录的外键。单测必须验证“删除成功但写日志失败”能准确报告部分成功，而不是诱导用户重试。

### F05 · P1：多处忽略外部业务失败，状态和成功计数会作假

位置：[archive.py:291](E:/GoogleAI/ls/mediaflow/app/api/archive.py:291)、[archive.py:497](E:/GoogleAI/ls/mediaflow/app/api/archive.py:497)、[moviepilot.py:353](E:/GoogleAI/ls/mediaflow/app/api/moviepilot.py:353)、[moviepilot.py:221](E:/GoogleAI/ls/mediaflow/app/api/moviepilot.py:221)。

CD2 的 `delete_file()` 返回包含 success 的字典，并不保证业务失败一定抛异常，但删除调用方普遍只 await；MP 手工下载和订阅删除也存在“HTTP 没抛异常即成功”的处理。复核清理接口最终还固定返回 `ok=True`，即便没有删除成功或全部列目录失败。

**复现：**CD2 返回 `success:false`，记录仍变成 `cleaned_redundant`，回复成功，甚至继续删除父目录；手工下载返回 `success:false`，API 仍答复 `ok:true`。

**建议与验收：**在客户端层统一校验业务结果，未知/空响应也不能无条件当成功；路由返回成功、失败、跳过和部分成功数量。每个写接口至少覆盖一次 HTTP 200 但业务失败的响应。

### F06 · P1：清理计划违背字幕保护承诺，也会误删保留视频的缩略图

位置：[review_engine.py:375](E:/GoogleAI/ls/mediaflow/app/core/review_engine.py:375)、[review_engine.py:399](E:/GoogleAI/ls/mediaflow/app/core/review_engine.py:399)、[archive.py:225](E:/GoogleAI/ls/mediaflow/app/api/archive.py:225)。

README、API 注释与前端承诺字幕不删除，但引擎明确把被淘汰版本的同名字幕加入删除计划。另一个不对称匹配：判定删除视频的缩略图时支持 `-thumb`，判定保留视频的附件却不支持，所以正在保留的视频缩略图也被当作孤儿。

**复现：**720p 视频被淘汰时，其 `.zh.srt` 也被删除；唯一保留的 `Show.S01E01.mkv` 对应 `Show.S01E01-thumb.jpg` 进入删除列表。

**建议与验收：**按当前产品承诺绝对保留字幕；保留附件匹配与删除附件匹配使用同一规则。未识别视频的附属刮削文件也不应仅因未进入 to_keep 就自动删除。

### F07 · P1：初检和清理使用两套集号模型，多集文件处理不一致

位置：[episode_parser.py:115](E:/GoogleAI/ls/mediaflow/app/core/episode_parser.py:115)、[review_engine.py:106](E:/GoogleAI/ls/mediaflow/app/core/review_engine.py:106)、[review_engine.py:231](E:/GoogleAI/ls/mediaflow/app/core/review_engine.py:231)。

初检支持 `S01E01-E02` 覆盖两集，清理引擎只提取第一个集号。文件格式支持也不一致：清理引擎含 strm/webm/iso 等，初检视频白名单不含这些格式。

**复现：**1080p 合集 `S01E01-E02` 加 720p 的 E01、E02，清理把更高画质合集判为残卷淘汰。该例未造成缺集，但已错误评估文件覆盖范围和最佳版本。

**建议与验收：**统一为“每个文件覆盖一组集号”的解析结果，所有保留/删除计划计算覆盖集合；删除前验证所保留文件完整覆盖删除文件中必须保留的集号，并统一媒体扩展名。

### F08 · P1：跨库增量被判为可归档，却被实际执行器拦截；缺集清单仍只反映源库

位置：[inspector.py:365](E:/GoogleAI/ls/mediaflow/app/core/inspector.py:365)、[archive.py:139](E:/GoogleAI/ls/mediaflow/app/api/archive.py:139)、[inspector.py:393](E:/GoogleAI/ls/mediaflow/app/core/inspector.py:393)。

源 E02 + 目标 E01、总数为 2，初检会标记 `archive_ready` 并提示增量归档；归档执行器发现目标 Season 1 已存在就统一标记 conflict、拒绝执行，没有文件级合并分支。同时 missing 在跨库合并前计算，未用联合集合重算，后续补缺仍可能重复补目标已有集。

**复现：**上述场景状态为 archive_ready，但 missing 仍为 `[1]`；实际归档回复 0/1 成功、同季冲突。

**建议与验收：**实现明确的文件级增量计划，或者在尚未支持前准确显示“需人工合并”。跨库状态、缺集集合、按钮能力必须一致，覆盖源有 E02/目标有 E01 的端到端用例。

### F09 · P1：初检生命周期缺少互斥、持久恢复和安全结果替换

位置：[inspection.py:34](E:/GoogleAI/ls/mediaflow/app/api/inspection.py:34)、[task_service.py:24](E:/GoogleAI/ls/mediaflow/app/services/task_service.py:24)、[task_service.py:184](E:/GoogleAI/ls/mediaflow/app/services/task_service.py:184)、[frontend/index.html:1281](E:/GoogleAI/ls/mediaflow/frontend/index.html:1281)。

只拦截数据库和内存都为 running 的重复启动，paused/stopped 仍可再次启动，旧 worker 可能尚未退出。没有受管理的后台 Task 句柄、运行代次或互斥；进度、暂停/停止事件全在进程内，重启后丢失，多进程也不共享。

初检完成/停止后统一删除旧 SeasonRecord 再插入新记录，人工校准和已有记录 ID 会丢失；若扫描部分失败，`result.errors` 没有参与完成判定，旧结果可能被不完整结果覆盖。停止结果虽然写入数据库，但前端只为 completed 加载明细，stopped 的保留进度难以查看。暂停检查又位于并发信号量之前，已经排队通过检查的 worker 后续仍能继续启动；目录发现阶段没有暂停/停止检查。

**复现：**paused 状态再次调用 run，返回成功并启动新 worker。其他结论由生命周期调用链确认，未做真实断电/多进程压测。

**建议与验收：**每个任务只有一个有效运行代次；运行和停止中禁止重复启动，取消后等待旧 worker 退出；按剧路径/季号稳定更新记录，扫描失败保留旧版本并展示失败范围；持久化运行状态并在启动时标记中断任务；前端展示 stopped 的部分结果。

### F10 · P1：元数据失败被当作“已完结”，搜索候选也缺少足够身份校验

位置：[inspector.py:618](E:/GoogleAI/ls/mediaflow/app/core/inspector.py:618)、[inspector.py:983](E:/GoogleAI/ls/mediaflow/app/core/inspector.py:983)。

季集详情为空或调用异常时直接 `finished=True`。标题兜底会遍历前五条候选，只要某季集数大于 0 就填入缺失季并标成 finished，不确认它与指定剧目/年份是否同一实体。不同候选的不同季可能拼进一份结果；解析到的新 TMDB 身份没有随完整元数据返回给调用者。

**复现：**季总数接口成功、剧集详情抛异常，仍产出 `finished:true`。

**建议与验收：**完结为 true/false/unknown；失败保留 unknown，不能自动归档。绑定媒体 ID 优先于模糊标题；对搜索候选验证标题、年份、别名和季信息，低可信匹配要求复核，并记录每个季数据的来源与置信状态。

### F11 · P1：前端直接拼接 HTML 和内联事件，既有注入风险也有普通文件名兼容问题

位置：[frontend/index.html:1933](E:/GoogleAI/ls/mediaflow/frontend/index.html:1933)、[frontend/index.html:2407](E:/GoogleAI/ls/mediaflow/frontend/index.html:2407)、[frontend/index.html:2657](E:/GoogleAI/ls/mediaflow/frontend/index.html:2657)。

网盘目录名、剧名、资源标题、错误消息等未经上下文转义进入 innerHTML。目录路径又被插进 `onclick="drillDownFolder('...')"`，普通 `O'Brien` 路径即可破坏事件字符串。攻击者若能影响目录名或上游返回内容，可能构造脚本执行内容，在当前页面权限下调用写接口。

此项通过数据流审阅确认危险渲染位置，未在真实浏览器执行恶意载荷。

**建议与验收：**普通文本统一 textContent；事件使用 addEventListener 和闭包传值；确需富文本的日志由可信模板创建节点。测试单引号、双引号、尖括号、反斜杠、中文和多行目录名，确认原样显示且不产生额外 DOM/事件。

### F12 · P1：启用鉴权后首页和 API 无法正常配合；路径边界也不足以保护删除

位置：[main.py:91](E:/GoogleAI/ls/mediaflow/app/main.py:91)、[frontend/index.html:938](E:/GoogleAI/ls/mediaflow/frontend/index.html:938)、[archive.py:614](E:/GoogleAI/ls/mediaflow/app/api/archive.py:614)、[schemas/task.py:22](E:/GoogleAI/ls/mediaflow/app/schemas/task.py:22)。

启用 MF_AUTH_TOKEN 时 `/` 不在静态白名单；前端没有登录/令牌输入流程，api() 也不携带 Authorization。虽然 `/static/index.html` 能加载静态页面，业务请求仍会 401。

删除整剧的边界是字符串 startswith：源根 `/src` 会错误接受 `/src.finish`，且允许等于源根；source_root 为空则不执行边界校验。任务创建允许任意路径，其他清理接口更缺少统一路径门禁。当前配置也未阻止源根/归档根重合或互相包含。

**复现：**以占位令牌启用鉴权后，进程内请求 `/` 返回 401。路径前缀与配置问题由代码确认。

**建议与验收：**完成登录/鉴权交互并限制 API 白名单为精确路径；对所有写操作统一规范化 POSIX 路径，拒绝相对路径、父级逃逸、源根本身以及源/目标重合。边界必须按路径段验证，空配置默认拒绝删除。

### F13 · P2：逐项 HTTP 解决了批量累计等待，但没有解决单项超时与可靠执行

位置：[clouddrive2.py:153](E:/GoogleAI/ls/mediaflow/app/clients/clouddrive2.py:153)、[frontend/index.html:2351](E:/GoogleAI/ls/mediaflow/frontend/index.html:2351)。

CD2 通用 RPC 有 30 秒超时和重试，单季归档还会串行迁移多项公共资产；MP 请求单次可等 60 秒并串行处理多个下载/历史操作。单项仍可能超过网关时限。浏览器关闭后后续项不再调度，当前请求是否执行完成则可能不明。前端没有请求超时和可查询的服务端操作 ID；停止按钮只能停止下一项，不能保证当前项在短时间内结束。进度在单项开始时就前进，最后一项未完成也可显示 100%。

通用 RPC 会对异常重试，包括有外部副作用的操作；未见操作幂等键或“超时后先查询实际结果”的恢复机制。这会放大响应丢失时的结果不确定性。

**建议与验收：**保留现有逐项 UI；服务端为每项创建可查询的 operation ID，状态含 queued/running/succeeded/failed/unknown/cancelled；写操作按幂等键去重；超时先核实结果。前端按完成项计进度、区分“停止后续”与“当前项仍执行”。文档应删除“彻底免疫超时”的绝对表述。

### F14 · P2：补缺防重主要依赖即时队列与宽松字符串匹配

位置：[download_service.py:255](E:/GoogleAI/ls/mediaflow/app/services/download_service.py:255)、[download_service.py:376](E:/GoogleAI/ls/mediaflow/app/services/download_service.py:376)。

下载队列匹配采用剧名包含或 TMDB 数字串包含，季号缺失也接受匹配；若 episode 字段为空，会把包含季号、年份、分辨率的整个标题交给数字解析。OperationLog 虽记录提交，但未参与查询去重，也没有待处理覆盖集的持久化唯一约束。提交后 MP 队列尚未同步时重复点击/另一页面提交仍可能重复下载；队列读取失败时继续无防重提交。

**建议与验收：**按媒体 ID、季号、集号集合和资源唯一 ID 建立 pending/submitted 覆盖账本；队列只补充校验，数字解析只识别明确集号语法；并发两次提交与响应丢失重试应仅产生一次外部下载。

### F15 · P2：健康检查存在假阳性，且不适合直接承担容器存活探针

位置：[system.py:32](E:/GoogleAI/ls/mediaflow/app/api/system.py:32)、[clouddrive2.py:281](E:/GoogleAI/ls/mediaflow/app/clients/clouddrive2.py:281)、[system.py:66](E:/GoogleAI/ls/mediaflow/app/api/system.py:66)、[Dockerfile:24](E:/GoogleAI/ls/mediaflow/Dockerfile:24)。

CD2 的 get_file_info('/') 直接返回本地根目录对象，不发网络请求；只要配置了地址/令牌，健康检查就可能显示 CD2 online。TMDB 只要有 key 就显示 online，也没有真实探测。另一方面该接口又会串行请求 MP/豆瓣；容器探针超时仅 5 秒，外部慢请求可能使正常进程被标 unhealthy。各探测异常路径的客户端 close 不在 finally 中，存在连接泄漏窗口。

**建议与验收：**拆分不访问外部的 `/live` 和带缓存、并发/短超时的 `/ready` 或依赖状态接口；真实探测失败显示 degraded，配置存在只显示 configured。模拟错误地址和慢依赖分别验证状态与探针响应时间。

### F16 · P2：状态、计数和前端显示没有统一状态机

位置：[archive.py:140](E:/GoogleAI/ls/mediaflow/app/api/archive.py:140)、[archive.py:150](E:/GoogleAI/ls/mediaflow/app/api/archive.py:150)、[archive.py:46](E:/GoogleAI/ls/mediaflow/app/api/archive.py:46)、[frontend/index.html:1312](E:/GoogleAI/ls/mediaflow/frontend/index.html:1312)。

后端会生成 archived、conflict、cleaned_redundant，但汇总只统计五个原分类，前端未认识的状态默认放入 airing，导致已归档或冲突记录显示成连载、分类数字与列表不一致。清理后重新诊断更新 recognized/duplicate，却不更新 missing_episodes_json。批量退订日志取 `s.title`，预览返回的是 `name`，因而会丢失真实剧名。

**建议与验收：**后端 Enum 和输出模型定义全部状态、允许动作、分类与计数；未知状态明确显示错误，不落入业务正常分类。一次状态流转统一更新诊断字段和汇总。

### F17 · P2：测试通过并不能证明核心调用链正确

位置：[test_frontend_logic.js:23](E:/GoogleAI/ls/mediaflow/tests/test_frontend_logic.js:23)、[test_archiver.py:47](E:/GoogleAI/ls/mediaflow/tests/test_archiver.py:47)、[audit_frontend.js:14](E:/GoogleAI/ls/mediaflow/tests/audit_frontend.js:14)、[run_all_tests.py:30](E:/GoogleAI/ls/mediaflow/tests/run_all_tests.py:30)。

前端测试说明宣称动态提取逻辑，实际重新写了 classifySeason；归档若干测试也在测试里重写判断条件，没有执行真实路由。专项 cross_diff 测试主要验证集合运算，无法发现真实增量执行器拒绝的问题。现有主运行器未纳入该专项脚本，静态审计没有执行规范要求的 node --check。

full_system_test.py 固定指向 127.0.0.1:8080 和真实媒体目录，会创建任务、触发扫描及校准，依赖 requests，而 requirements 未声明 requests，也没有独立测试依赖配置。直接运行总套件可能碰到正在运行的真实服务。本次未执行它。

**建议与验收：**默认全部测试使用隔离 SQLite 与模拟外部 API；真实服务集成测试显式 opt-in、参数化服务/目录并负责清理。CI 固定运行全部内联 JS 语法检查和实际路由契约测试；增加上述失败场景，避免复制实现当作验证。

## 二、建议优化完善的部分

以下是基于当前项目规模的增量优化，无需立即更换技术栈或引入完整分布式队列。

| 优先顺序 | 建议 | 可验收结果 |
|---|---|---|
| 1 | 建立统一的文件操作安全层，提供路径校验、只读预览、计划摘要、执行前重新验证 | 所有删除入口复用同一门禁；失败/未知不删除；预览内容变化时拒绝旧计划 |
| 2 | 将源目录自动清空与用户主动整剧删除拆成明确操作 | 归档只迁移已计划文件；自动删除仅真正空目录；整剧删除显示范围和资产清单 |
| 3 | 将写操作变成持久 operation 记录 | 外部动作前有意图记录，动作后有结果；超时有 unknown 状态和核实入口；多次提交可去重 |
| 4 | 固定接口模型与状态模型 | 前端请求/响应字段与后端一致；成功/跳过/部分失败有明确契约；新状态不会误分类 |
| 5 | 单任务互斥与运行代次 | 暂停、继续、停止、再次启动都对应唯一 worker；重启能解释未完成状态 |
| 6 | 统一 EpisodeParser 与媒体类型白名单 | 初检、清理、跨库比对、补缺对范围集/合集/特典的识别一致 |
| 7 | 元数据保留来源和不确定性 | 每季可追踪媒体 ID、数据源、时间、集数和完结依据；模糊匹配不会直接驱动删除 |
| 8 | 逐步拆分 3,400 余行单文件前端 | 优先抽出 api、状态/分类、batch、目录浏览、MP 运维模块；保留现有视觉与交互 |
| 9 | 补足移动端和交互可访问性验证 | 360/390/768/桌面宽度验证弹窗滚动、键盘焦点、Esc/返回键、抽屉遮罩、长目录/长日志显示 |
| 10 | 离线提供编译后的 CSS 与字体图标资源 | 前端不依赖 Tailwind/字体 CDN 才能正常显示；制定可执行的内容安全策略 |
| 11 | 配置和依赖管理 | 不在源码保留默认外部 API 密钥；使用部署注入；生产依赖锁定、测试依赖独立；启动校验空根目录和根重合 |
| 12 | 数据库演进与查询性能 | 引入可审计迁移，给 task_id/status 等实际查询字段补索引；测试外键行为；建立备份恢复检查 |
| 13 | 服务资源与运行配置 | 客户端用异步上下文管理确保关闭；TLS 校验默认开启并允许自签 CA 配置；日志使用有限尾读，避免每次读完整文件 |
| 14 | 文档和功能契约同步 | 清除过时注释、无效 force_clean 参数语义和硬编码 nas.finish/私有默认路径；说明实际停止语义与超时恢复方式 |

客户端 CD2/MP 当前使用 `verify=False`；这是已有实现中的连接校验缺口，不代表本次已经观察到中间人攻击。源码存在默认 TMDB key，本报告不展示其值，也未读取任何真实用户凭据。是否需要轮换由凭据所有者判断；审查没有执行轮换或修改配置。

部署方面，目前宽范围版本约束不能保证不同日期构建相同；Docker 使用默认 root 用户，也没有资源限制。可在完成业务保护后，锁定依赖、增加非 root 运行与合理资源边界。是否存在已公开依赖漏洞需另做联网版本核查，本次未核验，不能据此宣称有或没有 CVE。

数据库目前依靠 create_all 创建表，没有迁移版本。未显式开启 SQLite 外键；若今后开启，OperationLog 指向会被重建/删除的 SeasonRecord 的关系需要先设计好，否则容易产生约束冲突。建议审计记录保留稳定对象标识与操作时快照，而不是依赖可删除诊断记录存活。

## 三、与指定加固规范逐项核对

| 规范要求 | 当前情况 | 结论 |
|---|---|---|
| 五大写操作统一逐项流水线 | 五类前端操作均接入 runBatchPipeline | 形式已完成；补缺契约错误、结果判定错误需要修复 |
| 实时日志和中止后续任务 | 有日志看板与停止后续按钮 | 已实现；日志来自每项结束后的响应，并非项内服务端实时流；不能保证当前项立即结束 |
| 已成功项持久化 | 前端正常逐项请求时每项由后端 commit | 正常路径成立；外部成功/数据库失败、页面关闭/响应丢失的恢复未完成 |
| N>5 不使用一个长请求 | 前端已拆项 | 后端仍接受任意长度 ID 列表；单项也可能超时，不能称彻底免疫 |
| init 使用 Promise.allSettled | 当前代码已采用 | 符合；“提速60%”“毫秒级”没有测量依据 |
| 中性首屏占位 | 主标题/路径等已改造 | 改善明显；仍有私有默认路径等硬编码，应继续清理 |
| JS 交付前 node --check | 本次手动检查通过 | 主测试流程尚未固化这道防线 |
| 移动抽屉/横向分类/动态视口 | 源码中有对应布局 | 本次只静态检查，未做真实浏览器/移动设备视觉验收 |

规范本身建议调整：把“客户端驱动单项请求”定义成当前交互方案，补充服务端幂等、操作持久化、超时核实；把删除安全前提写成可自动断言的条件。Promise.allSettled 是容错调度工具，不应以固定语法替代首屏时间与失败可见性的性能目标。

## 四、实际验证结果与交付文件

| 检查 | 实际结果 |
|---|---|
| Python unittest discover | 36 项通过 |
| 前端 DOM/事件/弹窗契约审计 | 通过，但不验证浏览器行为和 API 字段 |
| 前端分类测试 | 4 个场景通过；测试为镜像实现，证据强度有限 |
| cross_diff 专项脚本 | 4 个函数检查通过；未被 unittest 自动收集 |
| 全部内联 JS node --check | 2 段非空内联脚本通过 |
| Python AST 语法检查 | app 下 31 个 Python 文件通过 |
| 新增隔离复现 | 15 个探针记录了上述异常行为 |
| 真实服务端到端测试 / Docker / 移动端视觉 | 未执行，不能宣称通过 |

复现脚本：[reproduce.py](E:/GoogleAI/ls/outputs/mediaflow-audit-20261109/reproduce.py)。机器可读证据：[reproduction-results.json](E:/GoogleAI/ls/outputs/mediaflow-audit-20261109/reproduction-results.json)。脚本只使用占位配置和模拟操作，不连接真实网盘；可从工作区根目录运行 `python outputs/mediaflow-audit-20261109/reproduce.py`。

建议实施顺序：先修 F01/F02 的删除保护，再修 F03/F04/F05 的真实结果和接口契约，随后处理 F06–F12 的模型/生命周期/鉴权问题，最后补完持久操作与前端模块化。先把本报告复现场景变成执行真实实现的回归测试，再用其验收修改。
