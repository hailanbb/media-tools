# MediaFlow (Docker 容器版)

> **工业级云原生媒体库整理与归档微服务**  
> 专为群晖 (Synology)、威联通 (QNAP)、Unraid、极空间、绿联等 NAS 系统及自建 Linux 云服务器深度定制。

---

## 📖 核心特性与架构优势

- **轻量高能**：基于 `python:3.12-slim` 构建，内置清华高速源、上海时区 (`Asia/Shanghai`) 与标准 UTF-8 语言环境，镜像体积精简。
- **开箱即用**：标准 Docker Compose 编排，支持容器健康自检 (`HEALTHCHECK`)、异常自动拉起 (`restart: unless-stopped`)。
- **全生态兼容**：无缝对接 CloudDrive2 与 MoviePilot 容器集群，支持容器网络互通 (`host` 模式或自定义 `bridge` 共享网络）。
- **数据完全持久化**：SQLite 数据库与任务操作流水持久化映射至宿主机本地目录，容器销毁升级数据零丢失。

---

## 🛠️ 环境要求

- **宿主机系统**：群晖 DSM 7.2+、威联通 QTS/QuTS hero、Unraid 6.11+、Ubuntu 22.04+、Debian 11+ 等支持 Docker 的系统。
- **Docker 引擎**：Docker Engine 20.10 及以上，支持 Docker Compose v2。

---

## 🚀 极速安装与部署

### 方法一：命令行 Docker Compose 部署（推荐）

1. **进入部署目录**：
   ```bash
   cd mediaflow-docker
   ```

2. **配置环境变量**：
   复制配置模板并编辑：
   ```bash
   cp .env.example .env
   nano .env   # 或使用 vim / 宝塔文本编辑器
   ```

   **关键参数配置示例**：
   ```ini
   # ── 基础服务配置 ──────────────────────────────────────────
   MF_DEBUG=false
   MF_PORT=8080
   MF_DATA_DIR=/app/data

   # ── CloudDrive2 连接（必填）────────────────────────────
   # 宿主机或局域网内 CD2 容器地址（注意：在桥接网络中不要写 localhost，应写 NAS 局域网真实 IP）
   MF_CD2_URL=http://192.168.1.100:19798
   MF_CD2_TOKEN=your-cd2-token-here

   # ── MoviePilot 连接（必填）────────────────────────────
   # MoviePilot 地址及 API 认证 Key
   MF_MP_URL=https://mp.yourdomain.com:8920
   MF_MP_TOKEN=your-moviepilot-api-key

   # ── 路径映射配置（必填）────────────────────────────────
   # 对应 CloudDrive2 网盘内的绝对路径
   MF_SOURCE_ROOT=/115/Library_incs/nas
   MF_ARCHIVE_ROOT=/115/Library_incs/nas.finish
   MF_LIBRARY_ROOT=/115/Library_incs/nas.finish
   ```

3. **构建并后台启动容器**：
   ```bash
   chmod +x deploy.sh && ./deploy.sh
   # 或者直接执行：
   docker compose up -d --build
   ```

4. **访问 Web 控制台**：
   打开浏览器，访问 `http://<NAS_IP>:8080` 即可开始使用！

---

### 方法二：群晖 (Synology) Container Manager 图形界面部署

1. 打开群晖 DSM 的 **Container Manager**。
2. 在左侧菜单点击 **项目 (Project)** ➔ 点击 **新增 (Create)**：
   - **项目名称**：`mediaflow`
   - **路径**：选择一个存储卷目录（例如 `/docker/mediaflow`），并将本项目的所有文件上传到该目录下。
   - **来源**：选择“创建 docker-compose.yml”并自动识别上传的 `docker-compose.yml`。
3. 确保目录下已正确创建并配置好 `.env` 文件。
4. 点击“下一步”直接构建并启动项目。

---

### 方法三：Unraid 部署指引

1. 在 Unraid 的 `appdata` 目录下创建 `mediaflow` 文件夹（如 `/mnt/user/appdata/mediaflow`）。
2. 将项目文件放入该目录，并配置好 `.env`。
3. 在 Unraid 终端中执行：
   ```bash
   cd /mnt/user/appdata/mediaflow
   docker compose up -d --build
   ```
4. 容器将自动在 Docker 列表中呈现，并可通过 WebUI 直接跳转。

---

## 🔧 容器运维与升级指南

### 1. 查看容器实时日志
```bash
docker compose logs -f mediaflow
```

### 2. 检查健康检查状态
```bash
docker ps -f name=mediaflow
# 正常运行时 STATUS 显示 (healthy)
```

### 3. 一键平滑更新
由于业务数据、扫描记录均存储在挂载卷 `./data` 中，更新镜像绝不影响已有配置：
```bash
git pull
docker compose down
docker compose up -d --build
```

### 4. 容器内执行自动化测试
如需验证容器内的运行完整性，可进入容器执行内置的 8 大全矩阵测试套件：
```bash
docker compose exec mediaflow python tests/run_all_tests.py
```

---

## 🛡️ 网络通信进阶技巧 (避坑指南)

> [!TIP]
> **容器间通信注意事项**：
> 1. 如果你的 CloudDrive2、MoviePilot 和 MediaFlow 均运行在同一台 NAS 上，且使用 Docker 默认的 bridge 模式，**切忌将 `MF_CD2_URL` 填写为 `http://127.0.0.1:19798` 或 `http://localhost:19798`**，因为容器内部的 `localhost` 指向自身！
> 2. **正确方案**：
>    - 填写 NAS 的局域网物理 IP（例如 `http://192.168.1.100:19798`）；
>    - 或者让 MediaFlow 使用 `network_mode: "host"` 直接共享宿主机网络栈；
>    - 或者将所有容器加入同一个 Docker 用户自定义网络，直接使用容器名互联（例如 `http://clouddrive:19798`）。
