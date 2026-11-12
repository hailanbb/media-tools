# 🐳 MediaFlow (Docker 容器化发行版)

> **面向家庭影院与云原生 NAS 的一键容器化部署版本。支持群晖 Synology、QNAP、TrueNAS、Unraid 及各类 Linux 主机。**

[![Docker](https://img.shields.io/badge/Container-Docker%20Compose-2496ed.svg?style=flat-square&logo=docker)](#)
[![Version](https://img.shields.io/badge/Release-v2.5.20-6366f1.svg?style=flat-square)](#)

---

## 🚀 极速部署指南

### 1. 克隆代码与配置
```bash
git clone https://github.com/hailanbb/media-tools.git
cd media-tools/mediaflow-docker

cp .env.example .env
vim .env
```

### 2. 核心环境变量说明
| 环境变量 | 必填 | 默认值 | 说明 |
|---|:---:|:---:|---|
| `MF_CD2_URL` | ✅ | `http://cd2:19798` | CloudDrive2 服务地址（容器间互通请使用服务名或局域网 IP） |
| `MF_CD2_TOKEN` | ✅ | - | CD2 认证 JWT Token |
| `MF_MP_URL` | ✅ | `http://moviepilot:3001` | MoviePilot API 服务地址 |
| `MF_MP_TOKEN` | ✅ | - | MoviePilot API 密钥 (`X-API-KEY`) |
| `MF_SOURCE_ROOT` | ✅ | `/115/Library_incs/nas` | 挂载在容器内的源待整理目录路径 |
| `MF_ARCHIVE_ROOT` | ✅ | `/115/Library_incs/nas.finish` | 挂载在容器内的归档目标库路径 |
| `MF_PORT` | 否 | `8080` | Web 端口映射 |

### 3. 一键启动
```bash
docker compose up -d --build
```
启动后在浏览器中访问：`http://<宿主机IP>:8080`

---

## 📂 容器卷与持久化挂载说明

在 `docker-compose.yml` 中：
```yaml
services:
  mediaflow:
    build: .
    container_name: mediaflow
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data                   # SQLite 数据库与任务状态持久化
      - /your/cd2/mount:/115/Library_incs  # 云盘挂载点穿透映射
    env_file:
      - .env
```

---

## 📚 详细功能与架构

关于智能初检、双轨拟合、分段解析与安全门禁的详细设计，请参阅 [项目主 README](../README.md)。
