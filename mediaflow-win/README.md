# 🪟 MediaFlow (Windows 原生免环境运行版)

> **面向 Windows 本地 PC 与工作站的原生运行发行版。无需 Docker 引擎，即下即用。**

[![Windows](https://img.shields.io/badge/Windows-Native-0078d4.svg?style=flat-square&logo=windows)](#)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776ab.svg?style=flat-square&logo=python)](#)
[![Version](https://img.shields.io/badge/Release-v2.5.16-6366f1.svg?style=flat-square)](#)

---

## 🚀 本地极速启动指南

### 1. 准备运行环境
推荐使用 Python 3.11 或 3.12 虚拟环境：
```powershell
cd media-tools\mediaflow-win

# 创建并激活虚拟环境 (可选但推荐)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量
复制 `.env.example` 为 `.env`：
```powershell
copy .env.example .env
```
用 VS Code 或记事本打开 `.env` 填入配置：
* `MF_CD2_URL`: CloudDrive2 本地服务地址，如 `http://localhost:19798`
* `MF_CD2_TOKEN`: CD2 认证 Token
* `MF_MP_URL`: MoviePilot 服务地址，如 `http://localhost:3001`
* `MF_MP_TOKEN`: MoviePilot API Key
* `MF_SOURCE_ROOT`: 本地挂载盘路径（或映射的网络驱动器路径）
* `MF_ARCHIVE_ROOT`: 归档目标路径

### 3. 一键启动服务
```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```
打开浏览器访问：`http://localhost:8080`

---

## 🧪 自动化回归测试流水线

Windows 版包含完整的本地全矩阵自动化测试套件：
```powershell
python tests/run_all_tests.py
```
一键验证包括 P0 物理安全探针、多集识别、双轨拟合、分段治理在内的全套测试流水线。

---

## 📚 详细功能与架构

关于智能初检、双轨拟合、分段解析与安全门禁的详细设计，请参阅 [项目主 README](../README.md)。
