# 🎬 剧集整理工作流 — Ubuntu/Linux 命令行后端程序 (Python 版)

基于 CloudDrive2 / MoviePilot / TMDB API 的剧集整理、补缺与完结归档工具（纯原生 Python 3 实现）。
针对 **Ubuntu 20.04 / 22.04 / 24.04 LTS** 及各类 Linux 服务器环境深度优化。

---

## 极简使用：两步完成部署

### 第一步：将代码上传到服务器并配置
将本目录（`tv-workflow-Python`）上传至服务器任意位置（例如 `/opt/tv-workflow-Python` 或 `~/tv-workflow-Python`）。
- **若为全新克隆/下载**：
  ```bash
  cd tv-workflow-Python
  cp config.example.json config.json
  # 使用 nano / vim 编辑 config.json 填入您的 CloudDrive2、MoviePilot 与 TMDB 凭据
  ```
- **若直接上传本地已配好的环境**：
  若本地已在 `config.json` 中配置好参数，直接打包上传即可，服务器端无需再次编辑！

### 第二步：一键初始化环境
在服务器的项目目录中，执行一键初始化脚本：
```bash
sudo bash install.sh
```
该脚本会自动完成：
1. 安装基础依赖包（`python3`、`pip3` 等）
2. 安装所需 Python 库（`openpyxl`、`requests`、`croniter` 等，**免手动配置虚拟环境**）
3. 授予所有程序执行权限
4. 注册全局命令 `tv` 到系统路径 `/usr/local/bin/tv`

---

## 在服务器上直接执行命令

初始化完成后，**无论在服务器的哪一个目录下**，均可直接使用快捷指令 `tv` 执行全部功能：

| 想要执行的操作 | 直接运行的命令 | 说明 |
| :--- | :--- | :--- |
| **打开交互式控制台** | `tv` | 显示中文彩色菜单，支持按数字挂机或手动触发 |
| **交互式分类选剧初检** | `tv inspect -i` 或 `tv inspect` | 浏览媒体库 (动漫/电视剧) -> 子分类 -> 剧集列表，支持数字选剧、多选、区间 `1-3` 或剧名过滤 |
| **指定分类初检** | `tv inspect --category 日韩动漫` | 仅初检指定分类下的剧集（如 日韩动漫、国产剧） |
| **指定剧名初检** | `tv inspect --show 凡人修仙传` | 仅初检名称包含关键字的剧集 |
| **指定精确目录初检** | `tv inspect --path "/115/.../葬送的芙莉莲"` | 精确扫描指定网盘剧集或分类目录 |
| **补缺下载** | `tv download` | 自动调取最新报表，优先执行 MoviePilot 受阻修复，再提交下载（含专属站点） |
| **MoviePilot整理修复** | `tv repair` | 专项解除 MoviePilot 转移阻塞历史并重新触发整理（兼容电视剧与动漫） |
| **复核清理 (预览)** | `tv review` | 预览待淘汰删除的冗余旧版本与对应刮削文件 |
| **复核清理 (执行)** | `tv review --yes` | 实际删除冗余文件并自动刷新网盘季目录缓存 |
| **完结归档 (预览)** | `tv archive` | 预览拟执行整剧移动或增量季合入的剧集 |
| **完结归档 (执行并对账)** | `tv archive --yes` | 执行归档移动，并自动触发目标完整性核验与源存量对账 |
| **归档后对账与空别名清理** | `tv verify` | 独立对账核验目标视频、源存量，并自动安全清理源端残留的空别名目录 |
| **全自动流程** | `tv auto` | 依次自动执行：定向初检/指定待处理 → 补缺下载 → 复核清理 → 归档对账 |

> 提示：如果您在项目目录内，也可以通过 `./tv-workflow.sh <命令>` 运行，效果完全一致。

---

## 交互式控制台菜单 (`tv`)

在终端直接输入 `tv` 即可进入控制台：
```text
══════════════════════════════════════════════════
  🎬 剧集整理工作流 (Python 版) — 后台运行中
══════════════════════════════════════════════════
  定时配置: [0 2 * * *] (每日自动执行)

  [1] 选剧/分类初检（浏览选择媒体库、分类与剧集，支持多选）
  [2] 立即补缺下载（含自动整理修复）
  [r] MoviePilot 整理历史专项修复
  [3] 立即复核清理（预览模式）
  [4] 立即执行归档（预览模式）
  [5] 复核清理（执行删除）
  [6] 执行归档（实际移动并自动对账复核）
  [v] 归档后对账复核（验证目标完整性与清理残留空别名）
  [7] 全自动流程 (初检→下载→复核→归档)
  [8] 查看最新报表路径
  [9] 查看最新运行日志
  [0] 退出
══════════════════════════════════════════════════
```

---

## 已经安装旧版的服务器如何更新升级？

如果服务器上已安装了旧版 `tv-workflow-Python`，更新只需极简的 3 步：

### 1. 本地打包/上传新版代码
将本地更新后的 `tv-workflow-Python` 目录文件上传并覆盖服务器上的项目目录（例如 `/opt/tv-workflow-Python` 或 `~/tv-workflow-Python`）。
> **注意**：本地的 `config.json` 已经同步更新了新的网盘路径与站点配置，可直接一并覆盖。

### 2. 重新执行一键权限与依赖刷新
通过 SSH 进入服务器上的项目目录，执行：
```bash
cd /opt/tv-workflow-Python   # 替换为您服务器上的实际安装路径
sudo bash install.sh
```
该脚本会：
- 自动更新全部依赖；
- 重新赋予执行权限；
- 刷新全局 `tv` 命令指向。

### 3. 重启后台服务（如果开启了 systemd 常驻）
若您先前启用了 `tv-workflow.service` 系统服务，重启使其生效：
```bash
sudo systemctl restart tv-workflow.service
sudo systemctl status tv-workflow.service
```
若使用的是前台控制台交互，直接运行 `tv` 即可立即体验新版！

---

## 后台常驻与开机自启 (可选)

如果希望在 Ubuntu 服务器上开机自启并在每日 02:00 定时执行整理：
```bash
# 复制 systemd 服务配置
sudo cp systemd/tv-workflow.service /etc/systemd/system/

# 启动并设置开机自启
sudo systemctl daemon-reload
sudo systemctl enable --now tv-workflow.service

# 查看运行状态与实时日志
sudo systemctl status tv-workflow.service
sudo journalctl -u tv-workflow.service -f
```

---

## 核心流程架构

```text
① 初检 → ② 补缺下载 (含整理历史修复) → ③ 复核清理 → ④ 完结归档 (含对账复核)
   │                    │                           │                 │
   │  扫描网盘CD2       │ 检查MP转移阻塞优先修复    │ 闭环核验修复集  │ 整剧移动 / 季增量合入
   │  核对TMDB集数      │ 搜索站点(追加观众/天空等) │ 保留唯一完整版  │ 目标完整性物理核查
   │  识别缺失/连载     │ 优选做种并提交下载        │ 清理旧版与刮削  │ 别名残留排查与空别名清理
   │                    │                           │                 │
   └──→ .xlsx ──────────┴─────→ .xlsx ──────────────┴───→ .xlsx ──────┴───→ 最终对账报表
```
