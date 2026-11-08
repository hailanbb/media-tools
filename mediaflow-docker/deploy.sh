#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  MediaFlow 媒体库智能整理与归档工作台 (Docker 容器版)   "
echo "=========================================================="

if [ ! -f .env ]; then
    echo "[提示] 未检测到 .env 配置文件，正在从 .env.example 复制..."
    cp .env.example .env
    echo "[重要] 请先编辑 .env 文件配置真实参数，然后重新运行本脚本。"
    exit 0
fi

echo "[启动] 正在构建并启动 MediaFlow 容器..."
docker compose up -d --build

echo ""
echo "[成功] MediaFlow 容器已成功启动！"
echo "[控制台] 访问地址: http://$(hostname -I | awk '{print $1}'):8080"
echo "[日志查看] docker compose logs -f"
