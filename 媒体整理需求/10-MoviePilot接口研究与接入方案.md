# MoviePilot 接口研究与接入方案

文档基线：1.4（2026-10-29）。Python 当前核查见 13；历史研究/其他实现核查保留原时点。

原专项研究日期：2026-10-27 · 研究基线：1.2；当前需求修订为 1.4

**用户已确认部署版本为 v2.15.6；豆瓣数据必须通过 MoviePilot 获取。** 本文件以该版本固定源码为当前适配依据，将公开文档的 v3.1.9 作为升级差异参考，不要求升级 MoviePilot。本次未连接用户实例、未读取凭据、未测试真实业务请求、未修改项目源码或配置。

## 1. 研究结论

1. 豆瓣接入路径可以明确为“工作流 → 用户 MoviePilot 实例 → MoviePilot 的豆瓣模块”，无需在本工具中另建直接访问豆瓣的默认路径。
2. [公开 API 站](https://api.movie-pilot.org/)通过 swagger-initializer.js 加载 [openapi.json](https://api.movie-pilot.org/openapi.json)。本次读取的 info.version 是 v3.1.9，包含 393 个路径，未提供 servers 字段；它不能证明用户 v2.15.6 实例具备全部同名能力。
3. v2.15.6 已有按来源搜索媒体、豆瓣详情、TMDB 季集目、剧集组等接口。不同端点的响应有裸数组、裸对象和业务包装，必须逐端点适配。
4. MoviePilot 把豆瓣详情转换成统一媒体对象，某些季号/集号由标题与总数推导；返回了 seasons 不等于获得逐集播期证据。
5. HTTP 方法不能单独决定是否只读；例如 GET /transfer/now 是立即整理。探测接口应采用业务白名单。

公开 OpenAPI 本次内容 SHA-256：

    2bd4be5504b707f7e41a31996bde07a5cb2a3ec2ac81aa2ebff83a9fa3c96dce

只下载和解析了公开说明，未在公开文档中填入用户凭据，也未使用 Try it out 执行业务。

## 2. 版本与证据层级

| 对象 | 固定版本/证据 | 用途与限制 |
| --- | --- | --- |
| 用户部署 | 用户明确回复 v2.15.6 | 当前设计基准；尚未用实例只读请求核验响应 |
| v2.15.6 官方源码 | commit 684e76e5184525db6df1a627d327d3356064783c | 当前接口参数、响应类型、豆瓣转换逻辑 |
| 公开 Swagger | 本次 info.version=v3.1.9 | 研究当前公开契约；不能套用到 v2 |
| v3.1.9 官方源码 | commit 811503b709f6207ee12ce17fd3a1a113effee723 | 解释 v3 统一来源身份、包装响应和预览能力 |
| 本地客户端 | lib/clients/moviepilot.js 及修复模块 | 判定已有基础和适配缺口；注释不是服务契约 |

发生差异时，按“实际部署只读验证 + 对应版本源码/契约”定案。不要因某个字段在 v3 存在就假设 v2 能接受，也不要因 v2 某端点裸返回就假设全部端点都裸返回。

## 3. v2.15.6 当前接入候选

以下路径均相对于**用户 MoviePilot 实例的 API 基址**，不是 api.movie-pilot.org。中文参数应由 URL 编码器构造。

| 用途 | 方法与路径 | 关键参数/语义 | 当前设计要求 |
| --- | --- | --- | --- |
| 搜索豆瓣条目 | GET /api/v1/media/search | title、type=media、source=douban、page、count | 裸列表；逐页取得候选，不能只拿第一条 |
| 已知豆瓣 ID 的详情 | GET /api/v1/douban/{doubanid} | 豆瓣原生 ID | 裸 MediaInfo；空对象不等于有效详情 |
| 来源明确的媒体详情 | GET /api/v1/media/{mediaid} | 带来源前缀 ID，例如 tmdb:编号；type_name=电视剧 | 校验实际返回 ID/类型；不要假设前缀名和内部来源枚举相同 |
| TMDB 全部季 | GET /api/v1/tmdb/seasons/{tmdbid} | TMDB 剧 ID | 列表；不能把全剧总数当本季 |
| TMDB 本季集目 | GET /api/v1/tmdb/{tmdbid}/{season} | season；可选 episode_group | 列表；逐项检查季号、集号、播期和组归属 |
| 查询可用剧集组 | GET /api/v1/media/groups/{tmdbid} | TMDB 剧 ID | 仅发现候选，不自动选择“最大集数”的组 |
| 查询指定组季信息 | GET /api/v1/media/group/seasons/{episode_group} | 明确组 ID | 需结合既定编号政策，不能覆盖原始编号 |
| 资源站资源搜索 | GET /api/v1/search/media/{mediaid} 或 /search/title | 与媒体条目搜索不同 | 返回资源上下文；不用于查豆瓣登记集数 |
| 活跃下载与整理 | GET /api/v1/download/、GET /api/v1/transfer/queue | 任务及状态 | 只读核对，不以方法已定义视为调用链已使用 |
| 整理预览 | POST /api/v1/transfer/manual | 对应版本 ManualTransferItem，preview=true | 业务预览；需验证返回每项，不能只看汇总 |

依据：[v2 媒体端点](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/api/endpoints/media.py)、[豆瓣端点](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/api/endpoints/douban.py)、[TMDB 端点](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/api/endpoints/tmdb.py)、[资源搜索](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/api/endpoints/search.py)。

“媒体搜索”查询剧/电影条目，“资源站搜索”查询种子/资源，不能混用。无需修改 MoviePilot 的全局 SEARCH_SOURCE、识别来源、刮削来源来实现一次豆瓣复核；使用已核实的请求级 source=douban。

## 4. 豆瓣复核的完整数据流

### 4.1 触发前先统一编号

既有用户规则保持：本地集号超出 TMDB 本季范围时用豆瓣复核。新增动漫后，先证明本地和 TMDB 使用同一季集/剧集组体系；绝对集号 25 与 TMDB 第一季 12 集不能直接比较。

规范化后确实出现本季 E17、TMDB 仅登记到 E16，进入豆瓣复核。文件数多、同集多版本、样片、OP/ED 或跨季文件不直接触发。

### 4.2 查找与绑定

1. 优先使用已经核验的“TMDB 剧+季/编号体系 ↔ 豆瓣条目”绑定。
2. 没有绑定时，向 MP 的媒体搜索发送 title 和 source=douban；保留全部候选与页状态。
3. 核对完整名称/别名、类型、年份、季度/分部含义；不能只匹配汉字主标题。
4. 对唯一可信候选请求豆瓣详情，再核对豆瓣 ID、具体条目范围及季映射。
5. 候选多义或字段不足时请求用户绑定**豆瓣条目 ID/链接及对应季**，随后仍经 MP 取详情；这不是绕过 MP 直接抓网页。

用户当前“经 MP 获取豆瓣”的选择不等于要求所有 TMDB 请求也改走 MP。可以把 MP 的 TMDB 只读接口作为适配候选，是否替代当前直连 TMDB 另行决定，不能悄悄切换来源通道。

### 4.3 从返回值取得数量时的限制

v2.15.6 的 set_douban_info 会从豆瓣标题解析季号；无明确季号时可用 1，并根据 episodes_count 构造 1..N。to_dict 又会清空原始 douban_info。因此：

- 返回 seasons 中的数组可能是**由总数合成的序列**，不是逐集查得的目录。
- 返回季号 1 可能是兜底，不能据此确认“这个豆瓣条目就是 TMDB 第一季”。
- 不把可选 number_of_episodes 缺失/0 当作零集；应按已验证响应提取对应季列表/数量，并检查一致性。
- 有数量，没有播出日期/结束证据时，数量核实与播完状态分开保存。
- 若接口没有原始字段，不在报告中伪造“豆瓣原始证据”；记录转换后事实、转换限制、身份绑定和提取规则。

依据：[v2.15.6 MediaInfo 转换与序列化](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/core/context.py)。

建议输出：

    provider: douban
    accessedVia: moviepilot
    adapterVersion: mp-v2.15.6
    subjectId: 豆瓣原生 ID
    bindingId: 明确的剧季/分部对应关系
    count: 正整数或 null
    episodeEvidenceKind: count-derived 或 explicit-list
    seasonEvidenceKind: verified-binding / title-derived / unknown
    observedAt: 本次 MP 查询时间
    upstreamFetchedAt: 若 MP 未提供则 null
    responseShape: v2-media-info
    completionEvidence: 独立证据或 unknown

MP 可能使用缓存；本次查询时间不等于豆瓣实时抓取时间。不自动清空 MP 缓存来取得“更新”。

### 4.4 结果处置

| 结果 | 工作流行为 |
| --- | --- |
| 同剧同季/分部绑定明确，豆瓣支持更大有效数量 | 保留两来源，按核实范围重算缺集与版本；不自动覆盖播期 |
| 豆瓣仍不能解释超范围 | 人工核实编号、条目与文件，不能删除超出集 |
| MP 未认证、不可达或豆瓣上游失败 | 豆瓣待复核；相关清理/归档阻挡 |
| MP HTTP 200 但空对象、空列表、类型不符 | 无有效证据，不当作 0 集或不存在 |
| 只有数量，没有新增集目的播期 | 保留数量，新增集播期未知；不扩大已播可下载集合 |
| MP 不支持或返回形状未知 | 报兼容性问题；不回退为工具直连豆瓣 |

## 5. 公共 v3 文档带来的整体优化

### 5.1 来源身份与响应适配

v3 公共契约使用 media_source + media_id；媒体搜索参数由 v2 的 source 变为 media_source。通用媒体详情和精确资源搜索的路径 ID 不再应直接照搬 v2 的带前缀格式；v3 文档要求另带 media_source。豆瓣专用 /douban/{doubanid} 仍使用豆瓣原生 ID。[v3 媒体源码](https://github.com/jxxghp/MoviePilot/blob/811503b709f6207ee12ce17fd3a1a113effee723/app/api/endpoints/media.py)

当前方案应定义固定版本适配器：

    统一领域接口
       → mp-v2.15.6：source 参数、带来源 ID、逐端点裸/包装响应
       → 未来 mp-v3：media_source + media_id、统一业务包装

不要用“看到 data 就拆，没有就原样”作为唯一校验；还要验证 success、字段类型、分页、身份和空值。返回中所有辅助 ID 只是映射证据，不能相互替代主身份。

### 5.2 操作类型按语义分类

建议维护读能力/预览/写能力目录，包含 HTTP 方法但不以方法决定权限。已确认 v2 GET /transfer/now 会触发整理；公共 v3 还把下载开始/暂停暴露为 GET。这类端点不能加入“所有 GET 均可安全探测”的扫描器。[v2 整理端点](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/api/endpoints/transfer.py)

新增数据读取不得顺带调用 subscribe、scrape、缓存删除、历史删除、download/start、transfer/now 等改变外部状态的能力。

### 5.3 整理修复请求需要重新对照契约

官方 v2.15.6 ManualTransferItem 声明 fileitem、media_source/media_id 或兼容媒体 ID、type_name、season、episode_format/detail/offset、episode_group、preview、reorganize 等；本地 buildTransferRequest 发送的嵌套 media_info/meta_info 和 overwrite_mode 并非该模型声明的字段。

因此，“本地请求体写了 overwrite_mode=never”**不能证明服务落实了不覆盖**。必须按部署版本重新构造请求、核对服务有效覆盖策略和逐项预览；在无法证明不覆盖时阻挡。不要为了通过请求而修改 MoviePilot 全局安全/整理设置。

依据：[v2.15.6 整理请求模型](https://github.com/jxxghp/MoviePilot/blob/684e76e5184525db6df1a627d327d3356064783c/app/schemas/transfer.py)。该结论是源码契约对照，尚未实测用户部署的字段忽略/拒绝行为。

### 5.4 只读诊断先于自动修复

公开 v3 有更细的整理提交/人工核查状态，这说明操作回执值得结构化保存；这些端点不直接加入 v2 当前实现。当前仍以已验证 v2 能力核查队列、历史和文件；绝不因文档中出现更强的重整能力而扩大授权。

## 6. 兼容验证计划

建议将来仅在获准使用现有连接时执行：

1. 以非敏感版本/能力信息确认实例；不导出 system/env 全量。
2. 使用已知测试条目读取媒体搜索、豆瓣详情、TMDB 季集目；不提交业务写操作。
3. 验证裸响应、空对象、401/403、分页结束、MP 缓存时间缺失等情况。
4. 通过脱敏响应固定契约样例，并锁定接口版本。
5. 整理预览另用隔离样例，核验目标与覆盖语义；真实整理仍需独立计划。

鉴权方式按对应端点和现有配置适配，不从 token 的外观长度猜测全部接口授权。不要把用户凭据发给公开 API 文档站，也不要为调研读取密钥。

## 7. 本次应同步到总体文档的改变

- 把“豆瓣接入方式待选”改为“通过 MP 已确定，v2.15.6 实例响应待验证”。
- 新增来源转换证据、编号映射、业务语义读写分类、版本适配需求。
- 把当前修复参数与官方模型不一致列入 P0 接口缺口。
- 将自定义根与动漫数据归属贯穿初检、报告、恢复和归档，详见独立动漫文档。
- 保留后台写权限待讨论状态，不把新增 API 能力当作已允许自动执行。

对应验收：08 的 T-P01～T-P08。

## 8. 1.3 补充业务接入要求（不新增接口核查）

- 初检使用可信作品身份取得中文名/原名/别名，保存来源与语言；原目录可能为日文、韩文、繁体中文，不能以翻译后同名替代 ID/季映射。
- 资源搜索可补充已核实别名，结果去重并核实作品、季和版本；下载/整理历史标题变化不产生新的任务身份。
- 补缺前汇总同收藏同季的多目录、已下载待整理及在途覆盖；现成文件进入关联/治理，不因新中文目录为空重新下载。
- 后续整理路径应与已确认主目录对应，并核对实际落点；不能宣称一个目标路径参数必然控制完整最终路径，也不为统一中文名修改全局识别配置。
- S01 归档而 S02 留源时，明确各自所在位置及共享资料，核对整理任务是否会影响 S01 或剧根；不能把工作流内部锁当成控制外部所有任务。
- 公开文档与用户实例能力仍分开；本次只定义需要得到的业务结果，不新增 API 参数承诺、版本兼容结论或代码适配方案。

完整规则见 [12 专项](12-同剧多目录与分季归档专项需求.md)。

## 9. 1.4 当前 Python 对接状态与约束

本轮核查固定 Python 915e26e：已调用活跃下载端点，但空集数字段、未知响应、取数失败及提交前复查仍需按 Y-02 处理；不能把“方法存在/调用一次”称为完整防重。下载接收必须有可信稳定任务标识，超时保留未知，跨旧标题关联用作品/季/收藏身份。

本轮未核验用户 MP 实例响应，也未把公开 API 新版本套入 v2.15.6。初检与复核的 MP 豆瓣链路仍未见接入；此前已确认的豆瓣通道和触发规则继续作为目标要求。字段名、分页、来源与季编号要逐端点验证，具体固定源码差异见 [13 专项](13-Python版更新核查与需求补充.md)。
