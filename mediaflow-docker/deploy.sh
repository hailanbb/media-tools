#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo "  MediaFlow 媒体库智能整理与归档工作台 (Docker 容器版)   "
echo "=========================================================="

if [ ! -f .env ]; then
    echo "[提示] 未检测到 .env 配置文件，正在从 .env.example 复制..."
    cp .env.example .env
    echo "[重要] 请先编辑 .env 文件配置真实参数（注意容器内 CD2/MP 地址勿填 127.0.0.1，请填宿主机局域网 IP 或域名），然后重新运行本脚本。"
    exit 0
fi

mkdir -p ./data
echo "[启动] 正在构建并启动 MediaFlow 容器..."
docker compose up -d --build

PORT="$(grep -E '^MF_PORT=' .env | tail -n 1 | cut -d '=' -f2 | tr -d '\r ' || true)"
PORT="${PORT:-8080}"

echo ""
echo "[成功] MediaFlow 容器已成功启动！"
echo "[控制台] 访问地址: http://$(hostname -I | awk '{print $1}'):${PORT}"
echo "[版本检查] curl -s http://127.0.0.1:${PORT}/api/system/version"
echo "[日志查看] docker compose logs -f"
