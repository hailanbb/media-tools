# 🪟 MediaFlow (Windows 原生免环境运行版)

> **面向 Windows 本地 PC 与工作站的原生运行发行版。无需 Docker 引擎，即下即用。**

[![Windows](https://img.shields.io/badge/Windows-Native-0078d4.svg?style=flat-square&logo=windows)](#)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776ab.svg?style=flat-square&logo=python)](#)
[![Version](https://img.shields.io/badge/Release-v2.5.40-6366f1.svg?style=flat-square)](#)

v2.5.40：落实同 TMDB 多目录源端实际合并与前缀统一引擎（覆盖 M01–M14 验收场景）：区分本次源端目标（留源）与未来归档目标、有效 S 标记精准边界拆分与连集保护、伴生外挂字幕语言标签继承、同名资产保全至保留目录、错季与重名冲突写入前拦截、双端后验空壳清理与 CloudDrive2 云盘目录缓存强制刷新、合并后根据实存与已播状态自动回流连载留源；弹窗升级为「选择主目录并合并」，主操作一键原子合并，保留 0 移动语义辅助选项。模块方案见 [异常剧集模块需求与交互方案](MediaFlow异常剧集模块需求与交互方案-v2.5.39.md)。
v2.5.39：全面完善「异常剧集」模块核心治理：新增人工确认独立持久化与逐项生效引擎（作品归属、总集数、播出状态独立保存，不污染 TMDB 官方缓存，支持实时分流预览、跨轮继承与实质矛盾熔断）、异常唯一三分类计数（「需你决定」「证据待决 · 安全保留」「归档已齐，可清源」）、任务级「本次保留，不再提示」清单与恢复处理、跨轮指纹差分智能（无变化静默折叠、有变化置顶唤醒），移除普通卡常驻「重新核查」与异常卡整剧删除入口，按问题提供「人工确认」「确认目录」「核对归属」「重试失败项」与「刷新修改结果」。模块方案见 [异常剧集模块需求与交互方案](MediaFlow异常剧集模块需求与交互方案-v2.5.39.md)。
v2.5.38：全面完善「真缺集待补」模块（覆盖 D01–D09 验收场景）：支持综艺连期范围选种与含混候选跳过、双来源总集数冲突时前段缺号独立补缺与尾集依据单列、逐集低做种精确候选数/做种数诊断、单季推送前自选集号、单次队列对账未命中防重保护、等待页「提交待确认／等待入库」语义区分与按钮去重，以及三端实物覆盖详情展示。模块方案见 [真缺集待补模块需求与交互方案](MediaFlow真缺集待补模块需求与交互方案-v2.5.38.md)。
v2.5.37：修正候选作品集数被当作文件归属证据的误报；异常列表中的连载季改用「确认目录」「重新核查」与详情，隐藏不可用的手动处理按钮。模块方案见 [异常剧集需求与交互方案](MediaFlow异常剧集模块需求与交互方案-v2.5.39.md)，实现记录见 [异常误报与连载交互修正](MediaFlow异常误报与连载条目操作修正-v2.5.37.md)。

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
