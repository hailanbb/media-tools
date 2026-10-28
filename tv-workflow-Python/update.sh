#!/bin/bash
# ==============================================================================
# 🎬 剧集整理工作流 — 一键热更新脚本 (保持 config.json / 数据不动)
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔄 正在从 GitHub 同步最新代码..."
TMP_DIR=$(mktemp -d /tmp/tv-wf-update-XXXXXX)

CLONED=false
if git clone --depth=1 https://github.com/hailanbb/media-tools.git "$TMP_DIR" 2>/dev/null; then
    CLONED=true
elif git clone --depth=1 https://ghfast.top/https://github.com/hailanbb/media-tools.git "$TMP_DIR" 2>/dev/null; then
    CLONED=true
fi

if [ "$CLONED" = false ]; then
    echo "❌ 无法连接至 GitHub 或镜像加速节点，请检查网络"
    rm -rf "$TMP_DIR"
    exit 1
fi

SRC="$TMP_DIR/tv-workflow-Python"
if [ -d "$SRC" ]; then
    cp -r "$SRC/lib" "$SCRIPT_DIR/"
    cp -r "$SRC/bin" "$SCRIPT_DIR/"
    cp "$SRC/install.sh" "$SCRIPT_DIR/"
    cp "$SRC/tv-workflow.sh" "$SCRIPT_DIR/"
    cp "$SRC/update.sh" "$SCRIPT_DIR/" 2>/dev/null || true
    chmod +x "$SCRIPT_DIR/tv-workflow.sh" "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/bin/"* "$SCRIPT_DIR/update.sh" 2>/dev/null || true
    echo "🎉 升级完成！已成功更新至最新版代码。"
    echo "   config.json 与历史报表均已完整保留。"
else
    echo "❌ 未在拉取的仓库中找到 tv-workflow-Python 目录"
fi
rm -rf "$TMP_DIR"
