#!/bin/bash
# ==============================================================================
# 🎬 剧集整理工作流 (Python 版) — Ubuntu/Linux 一键初始化安装脚本
# ==============================================================================

set -e

# ANSI 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "\n${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  🎬 剧集整理工作流 (Python 版) — Ubuntu 一键极速部署初始化${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}\n"

# 1. 检查 root 权限（用于安装系统包及注册全局快捷命令）
IS_ROOT=false
if [ "$EUID" -eq 0 ]; then
    IS_ROOT=true
fi

# 2. 检查并安装系统级 Python3 及 pip
echo -e "${CYAN}[1/4] 检查系统 Python 环境...${NC}"
if ! command -v python3 &>/dev/null || ! command -v pip3 &>/dev/null; then
    echo -e "${YELLOW}未检测到完整 Python3/pip 环境，正在通过 apt 安装...${NC}"
    if [ "$IS_ROOT" = true ]; then
        apt-get update -y && apt-get install -y python3 python3-pip python3-venv
    elif command -v sudo &>/dev/null; then
        sudo apt-get update -y && sudo apt-get install -y python3 python3-pip python3-venv
    else
        echo -e "${RED}❌ 缺少 root 权限，请手动执行: sudo apt update && sudo apt install -y python3 python3-pip python3-venv${NC}"
        exit 1
    fi
fi
PYTHON_BIN=$(command -v python3)
echo -e "${GREEN}✅ 系统 Python: $($PYTHON_BIN --version) (${PYTHON_BIN})${NC}"

# 3. 免配置虚拟环境直接安装依赖（自动兼容 Ubuntu 20.04/22.04/24.04 与 PEP 668）
echo -e "\n${CYAN}[2/4] 安装 Python 依赖库...${NC}"
PIP_INSTALLED=false

# 尝试 1: 直接全局安装 (Ubuntu 24.04+ 需 --break-system-packages)
if pip3 install -r requirements.txt --break-system-packages &>/dev/null; then
    PIP_INSTALLED=true
    USE_SYS_PYTHON=true
# 尝试 2: 经典全局安装 (Ubuntu 20.04/22.04)
elif pip3 install -r requirements.txt &>/dev/null; then
    PIP_INSTALLED=true
    USE_SYS_PYTHON=true
# 尝试 3: 当前用户环境安装 (--user)
elif pip3 install --user -r requirements.txt &>/dev/null; then
    PIP_INSTALLED=true
    USE_SYS_PYTHON=true
fi

# 如果上述全局方式受阻，自动建立无感内置 .venv (用户无需感知，启动器自动调用)
if [ "$PIP_INSTALLED" = false ]; then
    echo -e "${YELLOW}系统启用了外部托管保护，正在配置自动静默环境 (.venv)...${NC}"
    if [ ! -d ".venv" ]; then
        python3 -m venv .venv
    fi
    .venv/bin/pip install --upgrade pip &>/dev/null || true
    .venv/bin/pip install -r requirements.txt
    USE_SYS_PYTHON=false
fi
echo -e "${GREEN}✅ 核心依赖安装完成 (openpyxl, requests, croniter 等)${NC}"

# 4. 授予脚本与入口可执行权限
echo -e "\n${CYAN}[3/4] 授予程序执行权限...${NC}"
chmod +x tv-workflow.sh install.sh bin/tv_workflow.py bin/daemon.py
echo -e "${GREEN}✅ 执行权限设置完成${NC}"

# 5. 注册全局命令 `tv` (无论在哪个目录，直接输入 tv 即可使用)
echo -e "\n${CYAN}[4/4] 注册全局快捷命令 'tv'...${NC}"
TARGET_LINK="/usr/local/bin/tv"

REGISTERED_TV=false
if [ "$IS_ROOT" = true ]; then
    ln -sf "${SCRIPT_DIR}/tv-workflow.sh" "$TARGET_LINK"
    REGISTERED_TV=true
elif command -v sudo &>/dev/null; then
    sudo ln -sf "${SCRIPT_DIR}/tv-workflow.sh" "$TARGET_LINK"
    REGISTERED_TV=true
else
    # 尝试当前用户的 ~/.local/bin
    USER_BIN="$HOME/.local/bin"
    mkdir -p "$USER_BIN"
    ln -sf "${SCRIPT_DIR}/tv-workflow.sh" "${USER_BIN}/tv"
    export PATH="${USER_BIN}:$PATH"
    echo -e "${YELLOW}已将快捷指令注册至当前用户目录: ${USER_BIN}/tv${NC}"
    REGISTERED_TV=true
fi

if [ "$REGISTERED_TV" = true ]; then
    echo -e "${GREEN}✅ 全局命令 'tv' 注册成功！可在终端任意目录下直接使用。${NC}"
fi

# 检查当前 config.json
echo -e "\n${CYAN}══════════════════════════════════════════════════════════════${NC}"
if [ -f "config.json" ]; then
    echo -e "${GREEN}🎉 部署完成！检测到本地 config.json 已就绪，无需任何手动配置！${NC}"
else
    echo -e "${YELLOW}⚠️ 未发现 config.json，已为您准备模板 config.example.json${NC}"
fi
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}\n"

echo -e "🚀 ${GREEN}常用命令指南 (可在任意目录直接运行):${NC}"
echo -e "  • 启动交互控制台 (菜单挂机):   ${CYAN}tv${NC} 或 ${CYAN}./tv-workflow.sh${NC}"
echo -e "  • 交互式分类选剧初检:          ${CYAN}tv inspect -i${NC}"
echo -e "  • 定向初检 (指定分类/剧名):    ${CYAN}tv inspect --category 日韩动漫${NC}"
echo -e "  • 补缺下载 (含自动整理修复):   ${CYAN}tv download${NC}"
echo -e "  • 复核清理冗余版本:            ${CYAN}tv review --yes${NC}"
echo -e "  • 执行完结归档与对账复核:      ${CYAN}tv archive --yes${NC}"
echo -e "  • 独立归档核验与清理空别名:    ${CYAN}tv verify${NC}"
echo -e "  • 全自动全流程运行:            ${CYAN}tv auto${NC}\n"
