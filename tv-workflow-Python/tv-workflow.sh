#!/bin/bash
# ==============================================================================
# 🎬 剧集整理工作流 — Ubuntu/Linux 命令行快捷启动器
# 支持任意目录下通过全局快捷方式 'tv' 或本地 './tv-workflow.sh' 调用
# ==============================================================================

set -e

# 解析真实脚本所在路径（支持符号链接）
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

# 自动选取最佳 Python 运行环境（完全无需人工手动 activate 虚拟环境）
PYTHON_EXEC=""
if [ -f "${SCRIPT_DIR}/.venv/bin/python3" ]; then
    PYTHON_EXEC="${SCRIPT_DIR}/.venv/bin/python3"
elif [ -f "${SCRIPT_DIR}/venv/bin/python3" ]; then
    PYTHON_EXEC="${SCRIPT_DIR}/venv/bin/python3"
else
    PYTHON_EXEC=$(command -v python3 || command -v python)
fi

if [ -z "$PYTHON_EXEC" ]; then
    echo "❌ 未检测到 Python 运行环境，请先执行: sudo bash install.sh"
    exit 1
fi

# 分发运行
if [ $# -eq 0 ]; then
    # 无参数时：进入交互式后台控制台
    exec "$PYTHON_EXEC" "${SCRIPT_DIR}/bin/daemon.py"
else
    # 带参数时：直接传递给 CLI 处理（如 tv inspect -i, tv archive --yes 等）
    exec "$PYTHON_EXEC" "${SCRIPT_DIR}/bin/tv_workflow.py" "$@"
fi
