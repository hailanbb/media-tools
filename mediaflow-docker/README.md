# MediaFlow — 媒体库智能整理系统

自动化管理网盘剧集/动漫媒体库的 Docker Web 应用。通过 CloudDrive2 扫描网盘目录，结合 TMDB/MoviePilot 智能诊断，实现一键归档、冗余清理、缺集补缺的完整工作流。

## 核心功能

| 功能 | 说明 |
|------|------|
| **智能初检** | 递归扫描 CD2 目录，自动识别 Season 结构，解析集号，匹配 TMDB 官方数据 |
| **精准诊断** | 🟢齐全可归档 🟡重复需复核 🔴缺集待补 🔵连载中 — 一目了然 |
| **安全归档** | 按季增量移动，tvshow.nfo 和共享海报受保护留源 |
| **冗余清理** | 自动识别最优版本（4K>1080p, HEVC>x264），字幕文件永不删除 |
| **MoviePilot 联动** | 订阅管理、转移队列监控、资源搜索、一键推送补缺 |

## 快速部署

```bash
# 1. 克隆
git clone https://github.com/hailanbb/media-tools.git
cd media-tools/mediaflow

# 2. 配置
cp .env.example .env
# 编辑 .env 填入真实的 CD2/MP 连接信息

# 3. 启动
docker compose up -d

# 4. 访问
# http://<服务器IP>:8080
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `MF_CD2_URL` | ✅ | CloudDrive2 gRPC-Web 地址 |
| `MF_CD2_TOKEN` | ✅ | CD2 认证 Token |
| `MF_MP_URL` | ✅ | MoviePilot API 地址 |
| `MF_MP_TOKEN` | ✅ | MoviePilot API Key |
| `MF_SOURCE_ROOT` | ✅ | 源目录根路径 |
| `MF_ARCHIVE_ROOT` | ✅ | 归档目录根路径 |
| `MF_LIBRARY_ROOT` | 否 | 媒体库根路径（默认同 archive_root） |
| `MF_PORT` | 否 | 服务端口（默认 8080） |
| `MF_AUTH_TOKEN` | 否 | API 鉴权 Token（留空则不启用） |
| `MF_DEBUG` | 否 | 调试模式（默认 false） |

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLAlchemy 2.0 (async) + aiosqlite
- **前端**: 单文件 HTML + Tailwind CSS + Font Awesome
- **通信**: CD2 via gRPC-Web (原生 Protobuf)，MP via REST API
- **部署**: Docker + docker-compose

## 目录结构

```
mediaflow/
├── app/
│   ├── api/          # FastAPI 路由
│   ├── clients/      # CD2/MP/TMDB 异步客户端
│   ├── core/         # 初检引擎、集号解析
│   ├── models/       # SQLAlchemy ORM
│   ├── schemas/      # Pydantic 数据模型
│   ├── services/     # 业务编排
│   ├── config.py     # 配置管理
│   ├── database.py   # 数据库初始化
│   └── main.py       # 应用入口
├── frontend/
│   └── index.html    # 前端界面
├── data/             # SQLite 数据库（运行时）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 版本历史

- **v2.5.7** (2026-11-09):
  - 核心突破：落实《MediaFlow剧季元数据交叉核对与纠错方案》，实现 TMDB 与豆瓣主动双轨交叉核验与自适应版型拟合（拆分版 vs 官方整期版）；
  - 花絮/切片治理：扩展中英文花絮切片正则拦截（深度支持 `Cut`, `Clip`, `Tape`, `母带`, `切片`, `看点`, `加更`, `特辑` 等 18+ 模式）；
  - 断崖集号智能剥离：引入 `separate_outliers` 算法，自动剥离孤立断崖花絮集号，杜绝虚假大面积缺集误报；
  - 正片全齐加更特辑容错：支持正片齐全附带少量收官特辑直接就绪为 `archive_ready`；
  - 前端异常打标纠偏：消除已自愈齐备剧集的误打标。
- **v2.5.6** (2026-11-09): 初检引擎双轨校准与 MoviePilot 运维面板深度联动。
