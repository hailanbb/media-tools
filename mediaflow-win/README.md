# MediaFlow (Windows 原生版)

> **极简、强大、高可靠的媒体库智能整理与全链路归档工作台**  
> 专为 Windows 10 / 11 / Windows Server 本地运行深度优化，无需安装 Docker，双击即可一键启动。

---

## 📖 需求与适用场景

- **追剧与归档断点**：115 网盘、CloudDrive2 与 MoviePilot / Emby 媒体库在长期追更过程中，经常出现因断流导致的缺集、重复集、版本混存或废弃源端冗余堆积。
- **为何选择 Windows 原生版？**
  - **轻量即用**：无需开启 WSL2 或安装体积庞大的 Docker Desktop，零虚拟化开销。
  - **无缝对接 CloudDrive2 挂载盘**：无论是通过 CD2 挂载的本地盘符（如 `X:\`），还是通过本地端口 `127.0.0.1:19798` 远程调用，原生性能卓越。
  - **极简运维**：自带一键启动脚本、虚拟环境自管理、依赖自检与智能端口恢复。

---

## 🛠️ 环境准备

- **操作系统**：Windows 10 / Windows 11 / Windows Server 2019 及以上版本。
- **Python 环境**：Python 3.10 ~ 3.12。
  - [点击前往 Python 官方下载页面](https://www.python.org/downloads/)
  - **安装要点**：安装第一步务必勾选 **`Add python.exe to PATH`**（将 Python 添加到系统环境变量）。

---

## 🚀 极速安装与启动

### 第一步：解压文件
将本项目完整解压至任意磁盘目录（例如 `D:\MediaFlow`，建议路径不含空格与特殊符号）。

### 第二步：首次双击 `start.bat`
1. 双击运行当前目录下的 **`start.bat`**。
2. 脚本将自动检测 Python 环境，并在当前目录下自动创建独立的隔离虚拟环境（`.venv`）。
3. 自动连接国内清华高速镜像源安装所需核心依赖。
4. 首次启动时，若检测到未配置 `.env`，脚本将自动从模板创建 `.env` 并弹窗打开记事本提示编辑。

### 第三步：配置 `.env` 关键参数
在打开的 `.env` 文件中填写你的真实服务参数：

```ini
# ── CloudDrive2 连接（必填）────────────────────────────
# 本地或内网运行的 CD2 地址及访问 Token
MF_CD2_URL=http://127.0.0.1:19798
MF_CD2_TOKEN=your-cd2-token-here

# ── MoviePilot 连接（必填）────────────────────────────
# MoviePilot Web 地址及 X-API-KEY / JWT Token
MF_MP_URL=https://mp.yourdomain.com:8920
MF_MP_TOKEN=your-moviepilot-api-key

# ── 路径映射配置（必填）────────────────────────────────
# 对应 CloudDrive2 网盘内的绝对路径（支持正斜杠）
# source_root:  待整理的追更源目录根路径
# archive_root: 完结归档目标库根路径
# library_root: Emby/Jellyfin 扫描的媒体库根路径
MF_SOURCE_ROOT=/115/Library_incs/nas
MF_ARCHIVE_ROOT=/115/Library_incs/nas.finish
MF_LIBRARY_ROOT=/115/Library_incs/nas.finish
```

### 第四步：启动工作台
保存并关闭 `.env` 后，再次双击 **`start.bat`**：
- 控制台将自动启动 FastAPI 异步后端引擎。
- 自动调起系统默认浏览器访问可视化工作台：`http://127.0.0.1:8080`。

---

## 🖥️ 停止与日常运维

- **停止服务**：
  - 直接关闭控制台黑窗口，或者双击运行目录下的 **`stop.bat`** 即可安全终止进程。
- **PowerShell 启动**：
  - 如果习惯使用 PowerShell，右键以 PowerShell 运行 `start.ps1` 亦可无缝启动。

---

## ⏰ Windows 开机静默自启配置指南

如果你希望 MediaFlow 在电脑开机或服务器重启后在后台自动静默运行：

### 推荐方式：Windows 任务计划程序（最稳定、无黑框）
1. 按键盘 `Win + R` 打开运行窗口，输入 `taskschd.msc` 回车打开“任务计划程序”。
2. 点击右侧“创建任务”：
   - **常规**：名称填写 `MediaFlow-Autostart`，勾选“不管用户是否登录都要运行”（或仅登录时运行）。
   - **触发器**：新建 ➔ 选择“在系统启动时”或“登录时”。
   - **操作**：新建 ➔ 启动程序：
     - 程序或脚本：填写虚拟环境中的 Python 路径，例如：`D:\MediaFlow\.venv\Scripts\python.exe`
     - 添加参数：`-m uvicorn app.main:app --host 0.0.0.0 --port 8080`
     - 起始于：填写程序根目录，例如：`D:\MediaFlow`
3. 保存即可。下次开机时服务将自动在后台极速启动。

---

## 🧭 核心业务操作说明

### 1. 扫描与初检诊断
- 在 Web 控制台左侧点击“新建任务”，通过内置的 CloudDrive2 目录浏览器直观选择待整理文件夹。
- 点击“开始初检”，系统将毫秒级全盘扫描视频与 `tvshow.nfo`，智能对齐 TMDB 官方季集标准。

### 2. 智能推送补缺
- 在“真缺集待补”分类下，点击剧集卡片上的“推送补缺”。
- **独家防跳过整理机制**：系统在向 MoviePilot 推送下载前，会自动匹配并**「仅删除旧转移记录」**（严格锁定 `deletesrc=false` 不误伤任何物理文件），彻底解除 MoviePilot 因历史记录而跳过入库整理的顽疾！

### 3. 复核清理与版本去重
- 当某一季存在混版（如同一集存在 1080p 与 2160p 两个版本）时，进入“待复核清理”。
- 点击“清理冗余”，系统遵循**孤本保全第一定律**：仅删除重复集中的低码率冗余版本，任何未在更高规格中出现的孤本视频将 100% 绝对保护留存！

### 4. 单季/全剧零残留归档
- 对于已齐备的完结剧集，点击“单季归档”或全剧归档。
- 系统自动增量将对应季视频、字幕及海报搬移至目标库，当源端全剧已无其余有效季度时，自动深度清理残留的空文件夹和元数据，实现源端完全零残留。

---

## ❓ 常见问题排查 (FAQ)

### Q1: 双击 `start.bat` 闪退怎么办？
- 请按 `Win + R` 输入 `cmd`，在命令行中通过 `cd /d D:\MediaFlow` 切换到程序目录，手动运行 `start.bat` 查看具体报错提示。
- 最常见原因是电脑未安装 Python 或安装时未勾选 `Add python.exe to PATH`。

### Q2: 提示 `8080 端口已被占用`？
- 双击运行当前目录下的 `stop.bat` 清理旧进程；
- 或者在 `.env` 中修改 `MF_PORT=8090`，重新启动即可。

### Q3: 运行测试套件？
- 在程序目录下打开终端，执行：
  ```cmd
  .venv\Scripts\activate
  python tests/run_all_tests.py
  ```
- 即可自动执行 8 大全矩阵自动化回归测试流水线。
