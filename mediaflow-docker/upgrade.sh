#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"

PORT="8080"
if [ -f "$SCRIPT_DIR/.env" ]; then
    ENV_PORT="$(grep -E '^MF_PORT=' "$SCRIPT_DIR/.env" | tail -n 1 | cut -d '=' -f2 | tr -d '\r ' || true)"
    [ -n "$ENV_PORT" ] && PORT="$ENV_PORT"
fi

echo "=== [1/5] 备份 SQLite 数据库 ==="
mkdir -p "$BACKUP_DIR"
if [ -f "$DATA_DIR/mediaflow.db" ]; then
    STAMP="$(date +%Y%m%d_%H%M%S)"
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" stop mediaflow >/dev/null 2>&1 || true
    cp "$DATA_DIR/mediaflow.db" "$BACKUP_DIR/mediaflow_${STAMP}.db"
    ls -1t "$BACKUP_DIR"/mediaflow_*.db 2>/dev/null | tail -n +8 | xargs -r rm -f
    echo "已备份至: $BACKUP_DIR/mediaflow_${STAMP}.db"
else
    echo "未发现既有数据库文件，跳过备份。"
fi

echo "=== [2/5] 从 GitHub 拉取最新代码 ==="
cd "$REPO_ROOT"
OLD_REV="$(git rev-parse --short HEAD)"
git fetch origin main
git pull --ff-only origin main
NEW_REV="$(git rev-parse --short HEAD)"
echo "代码版本: ${OLD_REV} -> ${NEW_REV}"

echo "=== [3/5] 重建镜像并启动容器 ==="
cd "$SCRIPT_DIR"
mkdir -p "$DATA_DIR"
docker compose up -d --build

echo "=== [4/5] 清理悬空旧镜像 ==="
docker image prune -f >/dev/null 2>&1 || true

echo "=== [5/5] 升级后健康与版本验收 ==="
for i in $(seq 1 15); do
    if curl -sf "http://127.0.0.1:${PORT}/api/system/health" >/dev/null; then
        echo "升级成功！当前运行版本：$(curl -s http://127.0.0.1:${PORT}/api/system/version)"
        exit 0
    fi
    sleep 2
done

echo "⚠️ 服务在 30 秒内未就绪，请检查日志：docker compose logs --tail=100"
exit 1
