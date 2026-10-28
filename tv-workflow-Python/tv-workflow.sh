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
if [ "$1" = "update" ] || [ "$1" = "upgrade" ]; then
    echo "🔄 正在从 GitHub 同步最新版本..."
    TMP_DIR=$(mktemp -d /tmp/tv-wf-update-XXXXXX)

    CLONED=false
    if git clone --depth=1 https://github.com/hailanbb/media-tools.git "$TMP_DIR" 2>/dev/null; then
        CLONED=true
    elif git clone --depth=1 https://ghfast.top/https://github.com/hailanbb/media-tools.git "$TMP_DIR" 2>/dev/null; then
        CLONED=true
    fi

    if [ "$CLONED" = false ]; then
        echo "❌ 无法连接至 GitHub 或镜像加速节点，请检查服务器网络"
        rm -rf "$TMP_DIR"
        exit 1
    fi

    SRC="$TMP_DIR/tv-workflow-Python"
    if [ -d "$SRC" ]; then
        cp -r "$SRC/lib" "$SCRIPT_DIR/"
        cp -r "$SRC/bin" "$SCRIPT_DIR/"
        cp "$SRC/install.sh" "$SCRIPT_DIR/"
        cp "$SRC/tv-workflow.sh" "$SCRIPT_DIR/"
        [ -f "$SRC/update.sh" ] && cp "$SRC/update.sh" "$SCRIPT_DIR/"
        chmod +x "$SCRIPT_DIR/tv-workflow.sh" "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/bin/"*
        echo "🎉 升级完成！已成功更新至最新版代码。"
        echo "   config.json 与历史报表均已完整保留。"
    else
        echo "❌ 未在拉取的仓库中找到 tv-workflow-Python 目录"
    fi
    rm -rf "$TMP_DIR"
    exit 0
elif [ $# -eq 0 ]; then
    # 无参数时：进入交互式后台控制台
    exec "$PYTHON_EXEC" "${SCRIPT_DIR}/bin/daemon.py"
else
    # 带参数时：直接传递给 CLI 处理（如 tv inspect -i, tv archive --yes 等）
    exec "$PYTHON_EXEC" "${SCRIPT_DIR}/bin/tv_workflow.py" "$@"
fi
