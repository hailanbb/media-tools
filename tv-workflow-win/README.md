# 🎬 剧集与动漫整理工作流 (tv-workflow-win) — Windows 桌面/后台版

基于 **CloudDrive2 / MoviePilot / TMDB API** 的无头剧集与动漫生命周期自动化管理工具。  
告别传统网页脚本的手工导入导出与浏览器挂机，支持 CLI 命令行、终端交互式菜单与 Win11 系统后台托盘常驻运行。

---

## 📖 解决的核心痛点

在基于 115网盘 / NAS + CloudDrive2 + MoviePilot 的媒体库管理体系中，用户长期面临以下四大顽疾：

1. **缺集漏追难排查**：人工逐剧逐季翻看网盘极其繁琐；网络下载容易出现断更、漏下某集的情况。
2. **MoviePilot 转移卡死与阻塞重转**：由于识别错误、源文件重命名冲突或目标同名，MoviePilot 容易在历史转移记录中留下“失败/阻塞”条目，导致后续新资源无法自动转移入库。
3. **云盘空间膨胀与版本冗余**：补全高码率/内封字幕版本后，原有的低质量先行版、外挂字幕残留未及时清理，白白浪费云盘存储空间。
4. **完结归档易发生破坏性冲突**：将已完结剧集从“待整理源库”迁移至“完结归档库”时，容易遇到同名剧集大小写冲突、目标季重复覆盖，或者目录被移动后原路径留下空别名目录导致刮削混淆。
5. **动漫整理与电视剧差异巨大**：动漫压制组常常附带 `NCOP`（无字片头）、`NCED`（无字片尾）、`PV`、`CM`、`Preview`、`Menu` 等特典视频，传统电视剧整理工具会误将其识别为主线第 1 集或造成集号错乱；同时日文 `話` 数、字幕组方括号 `[01]` 命名需要专门的容错引擎。

**本项目实现了：智能定向初检 $\to$ 整理历史修复与自动补缺下载 $\to$ 冗余版本与附属文件复核清理 $\to$ 安全归档移动与目标存量对账的全流程闭环。**

---

## 🚀 傻瓜式快速上手

### 第 1 步：安装 Node.js 运行环境
- 前往 [Node.js 官方网站](https://nodejs.org/) 下载并安装 **LTS 版本**（建议 Node.js 18 或 20+）。
- 安装时一路点击“Next”即可。安装完成后打开 Windows PowerShell，输入以下命令确认成功：
  ```powershell
  node -v
  npm -v
  ```

### 第 2 步：安装项目依赖
进入本项目根目录 `tv-workflow-win`，在终端执行：
```powershell
npm install
```
> 本项目仅依赖 `opencc-js`（简繁转换）、`xlsx-js-style`（彩色Excel报表读写）等极简基础库，安装可在数秒内完成。

### 第 3 步：填写配置文件
复制配置模板为实际配置文件：
```powershell
copy config.example.json config.json
```
使用记事本或 VS Code 打开 `config.json`，填写您的服务信息：
```json
{
  "clouddrive2": {
    "origin": "http://127.0.0.1:19798",       // 您的 CloudDrive2 Web 访问地址
    "token": "YOUR_CD2_API_TOKEN"              // CloudDrive2 API 令牌 (网页端开发者工具或系统设置中获取)
  },
  "moviepilot": {
    "origin": "http://127.0.0.1:3000",        // 您的 MoviePilot Web 地址
    "token": "YOUR_MOVIEPILOT_API_TOKEN",      // MoviePilot 个人中心 API 令牌
    "extraSearchSites": ["观众", "天空"]       // 补缺下载额外追加搜索的 PT/BT 专属站点
  },
  "tmdb": {
    "apiKey": "YOUR_TMDB_API_KEY",             // TMDB 开发者 API Key（免费申请，用于核对集数）
    "language": "zh-CN"
  },
  "paths": {
    "source": "/115/Library_incs/nas",         // 待整理源库根目录（包含 动漫/电视剧 及下级分类）
    "destination": "/115/Library_incs/nas.finish", // 完结归档库根目录
    "downloadPath": "/video/downloads"         // 刮削下载盘/整理来源路径
  },
  "transferRepair": {
    "enabled": true,
    "source": "/video/downloads",
    "target": "/115/Library_incs/nas"
  },
  "schedule": {
    "enabled": false,
    "cron": "0 2 * * *",                      // 每日凌晨 02:00 自动执行
    "stages": ["inspect", "download", "review", "archive"]
  },
  "notifications": {
    "enabled": true,
    "sound": true
  }
}
```

---

## 🎮 使用方式

### 方式 A：交互式控制台（新手推荐 ★★★★★）
直接运行托盘与交互控制台程序：
```powershell
node bin/tray.js
```
终端将呈现清晰直观的操作菜单：
```text
═══════════════════════════════════════════════
  🎬 剧集整理工作流 v1.0.0 — 后台运行中
═══════════════════════════════════════════════
  定时任务: 每日 02:00 自动执行

  [1] 选剧/分类初检（浏览选择媒体库、分类与剧集，支持多选）
  [2] 立即补缺下载（含自动整理修复）
  [r] MoviePilot 整理历史专项修复
  [3] 立即复核清理（预览模式）
  [4] 立即执行归档（预览模式）
  [5] 复核清理（执行删除）
  [6] 执行归档（实际移动并自动对账复核）
  [v] 归档后对账复核（验证目标完整性与清理残留空别名）
  [7] 全自动流程
  [8] 打开最新报表
  [9] 打开日志目录
  [0] 退出
═══════════════════════════════════════════════
  请选择 > 
```
- **输入 `1`**：即刻启动多级导航初检，先选媒体类型（`[1] 动漫`, `[2] 电视剧`），再选子分类（`日韩动漫`、`国产剧`等），再选具体剧集（支持序号单选 `1`、多选 `1,3,5`、区间 `1-5`、全选 `all` 或关键词过滤）。
- **输入 `2`**：读取初检报表，优先检测 MoviePilot 阻塞历史并自动修复，未修复项自动搜索高分做种资源并提交补缺下载。
- **输入 `3`**：以安全预览模式核算淘汰版本，只看不删；确认无误输入 `5` 实际清理旧版。
- **输入 `4`**：以安全预览模式核算符合全绿完结条件的剧集；确认无误输入 `6` 实际移动归档并自动对账。

---

### 方式 B：CLI 命令行执行（高级用户/脚本串联）

无论是单步调试还是特定剧集维护，都可以使用命令行参数直达执行：

```powershell
# 1. 交互式初检（弹出媒体库与剧集菜单导航）
node bin/tv-workflow.js inspect -i

# 2. 定向初检：仅检测特定分类（支持动漫与电视剧子分类）
node bin/tv-workflow.js inspect --category 日韩动漫
node bin/tv-workflow.js inspect --category 国产剧

# 3. 定向初检：仅初检包含关键字的剧集
node bin/tv-workflow.js inspect --show 凡人修仙传
node bin/tv-workflow.js inspect --show 葬送的芙莉莲

# 4. 定向初检：直接指定网盘任意目录路径
node bin/tv-workflow.js inspect --path "/115/Library_incs/nas/动漫/日韩动漫/间谍过家家"

# 5. 补缺下载（自动使用最新初检报表）
node bin/tv-workflow.js download

# 6. 整理历史专项修复（仅检查并解除 MoviePilot 阻塞历史重转）
node bin/tv-workflow.js repair

# 7. 复核清理（默认预览，加 --yes 实际执行删除）
node bin/tv-workflow.js review
node bin/tv-workflow.js review --yes

# 8. 完结归档（默认预览，加 --yes 实际执行移动并自动触发对账）
node bin/tv-workflow.js archive
node bin/tv-workflow.js archive --yes

# 9. 独立归档核验（核验归档目标完整性、源目录存量对账并清理残留空别名）
node bin/tv-workflow.js verify

# 10. 一键全自动流程
node bin/tv-workflow.js auto
```

---

## 🎨 Excel 彩色状态体系

工作流产出的报表保存在 `reports/` 目录下，采用直观的底色标识各季的处理状态：

| 底色 | 状态 | 说明 |
| :---: | :--- | :--- |
| 🟩 **绿色** | 完结无缺集 | 本地已抓取集数等于 TMDB 总集数，整剧所有季全绿后可安全归档 |
| 🟦 **蓝色** | 连载更新中 | 本地集数等于 TMDB 当前已播出集数，但整剧尚未完结，暂不归档 |
| 🟨 **黄色** | 疑似缺集 | 本地集数少于 TMDB 播出集数，或中间存在跳集，自动进入补缺下载 |
| 🟥 **红色** | 异常待查 | 资源未匹配到、TMDB 暂无本季数据或集数解析异常 |
| ⬜ **无底色** | 待确认/特殊 | 未刮削、单文件根目录或其他待人工确认情况 |

---

## 📁 核心目录结构

```text
tv-workflow-win/
├── bin/
│   ├── tv-workflow.js          # CLI 命令总入口
│   └── tray.js                 # 终端交互式控制台 + 定时调度入口
├── lib/
│   ├── clients/                # 服务客户端
│   │   ├── clouddrive2.js      # CloudDrive2 Protobuf RPC / REST 接口封装
│   │   ├── moviepilot.js       # MoviePilot v2 接口封装（含专属站点解析）
│   │   └── tmdb.js             # TheMovieDb 剧集元数据拉取
│   ├── core/                   # 核心计算模块
│   │   ├── episode-parser.js   # 动漫/电视剧高兼容集号解析器（含特典过滤）
│   │   ├── title-matcher.js    # 剧名与季号正则提取器
│   │   ├── transfer-repair.js  # MoviePilot 整理历史修复与手动转移引擎
│   │   └── report-adapter.js   # 彩色 Excel 报表生成与状态回写
│   ├── stages/                 # 四大流程阶段
│   │   ├── inspect.js          # ① 剧集/动漫初检
│   │   ├── download.js         # ② 补缺下载与整理修复
│   │   ├── review.js           # ③ 复核清理
│   │   ├── archive.js          # ④ 完结归档与对账
│   │   └── index.js            # 阶段分发调度器
│   └── platform/               # Windows 系统适配
│       ├── notify.js           # Win11 系统 Toast 消息弹窗
│       ├── scheduler.js        # 内置 Cron 定时执行调度器
│       └── data-store.js       # 本地下载与执行状态记录
├── config.example.json         # 配置文件模板
├── README.md                   # 说明文档
└── package.json
```
