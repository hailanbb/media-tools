#!/usr/bin/env bash
set -uo pipefail

# ==============================================================================
#  MediaFlow Ubuntu Docker 交互式一键管理面板
#  安装根目录: /root/docker/mediaflow
# ==============================================================================

BASE_DIR="/root/docker/mediaflow"
SRC_DIR="$BASE_DIR/src"
DATA_DIR="$BASE_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"
ENV_FILE="$BASE_DIR/.env"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
SELF_TARGET="$BASE_DIR/mf.sh"
REPO_URL="https://github.com/hailanbb/media-tools.git"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[信息]${NC} $*"; }
ok()    { echo -e "${GREEN}[成功]${NC} $*"; }
warn()  { echo -e "${YELLOW}[提示]${NC} $*"; }
err()   { echo -e "${RED}[错误]${NC} $*"; }

pause_key() {
    echo ""
    read -r -p "按回车键返回主菜单..." _
}

# 自动安装自身为全局命令 mf
install_shortcut() {
    mkdir -p "$BASE_DIR"
    local current_script
    current_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    if [ "$current_script" != "$SELF_TARGET" ] && [ -f "$current_script" ]; then
        cp -f "$current_script" "$SELF_TARGET"
        chmod +x "$SELF_TARGET"
    fi
    if [ -f "$SELF_TARGET" ] && [ "$(id -u)" -eq 0 ]; then
        ln -sf "$SELF_TARGET" /usr/local/bin/mf
    fi
}

# 安全读取 .env 中的键值（避免 source 解析特殊字符出错）
get_env_val() {
    local key="$1"
    local def="${2:-}"
    if [ ! -f "$ENV_FILE" ]; then
        echo "$def"
        return
    fi
    local line
    line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
    if [ -z "$line" ]; then
        echo "$def"
        return
    fi
    local val="${line#*=}"
    val="${val%$'\r'}"
    # 去除首尾引号
    val="${val#\"}"
    val="${val%\"}"
    val="${val#\'}"
    val="${val%\'}"
    if [ -z "$val" ]; then
        echo "$def"
    else
        echo "$val"
    fi
}

get_port() {
    get_env_val "MF_PORT" "8080"
}

get_lan_ip() {
    hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1"
}

# 检查并引导安装基础依赖
ensure_deps() {
    local missing=()
    command -v git  >/dev/null 2>&1 || missing+=("git")
    command -v curl >/dev/null 2>&1 || missing+=("curl")

    if [ ${#missing[@]} -gt 0 ]; then
        warn "检测到缺少基础工具: ${missing[*]}"
        read -r -p "是否自动安装 ${missing[*]}？[Y/n]: " ans
        if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then
            apt-get update && apt-get install -y "${missing[@]}" || { err "安装失败，请检查网络或 apt 源"; return 1; }
        else
            err "缺少必要工具，无法继续。"
            return 1
        fi
    fi

    if ! command -v docker >/dev/null 2>&1; then
        warn "未检测到 Docker 引擎。"
        read -r -p "是否使用官方脚本一键安装 Docker？[Y/n]: " ans
        if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then
            curl -fsSL https://get.docker.com | sh || { err "Docker 安装失败"; return 1; }
            systemctl enable --now docker >/dev/null 2>&1 || true
            ok "Docker 安装完成！"
        else
            err "请先安装 Docker 后再继续。"
            return 1
        fi
    fi

    if ! docker compose version >/dev/null 2>&1; then
        warn "未检测到 docker compose v2 插件，尝试安装 docker-compose-plugin..."
        apt-get update && apt-get install -y docker-compose-plugin || { err "docker compose 插件安装失败"; return 1; }
    fi
    return 0
}

# 生成顶层 docker-compose.yml
write_compose_file() {
    cat > "$COMPOSE_FILE" << 'EOF'
services:
  mediaflow:
    build:
      context: ./src/mediaflow-docker
      dockerfile: Dockerfile
    container_name: mediaflow
    restart: unless-stopped
    ports:
      - "${MF_PORT:-8080}:8080"
    volumes:
      - ./data:/app/data
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
    extra_hosts:
      - "host.docker.internal:host-gateway"
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8080/api/system/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
EOF
}

# 校验容器内 URL 是否误填 localhost/127.0.0.1
check_localhost_url() {
    local label="$1"
    local url="$2"
    if [[ "$url" =~ ^https?://(127\.0\.0\.1|localhost)(:|/|$) ]]; then
        local fixed="${url/127.0.0.1/host.docker.internal}"
        fixed="${fixed/localhost/host.docker.internal}"
        warn "${label} 填写了本机回环地址 (${url})，在 Docker 容器内将无法访问宿主机服务！" >&2
        read -r -p "是否自动替换为 ${fixed}？[Y/n]: " fix_ans
        if [[ "${fix_ans:-Y}" =~ ^[Yy]$ ]]; then
            echo "$fixed"
            return
        fi
    fi
    echo "$url"
}

# 交互式向导配置 .env
configure_env_wizard() {
    local lan_ip
    lan_ip="$(get_lan_ip)"

    echo ""
    echo -e "${BOLD}========== 交互式参数配置向导 ==========${NC}"
    echo -e "${YELLOW}说明：直接按【回车】将保留括号内的默认值/现有值${NC}"
    echo ""

    local cur_port cur_cd2_url cur_cd2_token cur_mp_url cur_mp_token
    local cur_src cur_arc cur_lib cur_debug
    cur_port="$(get_env_val MF_PORT "8080")"
    cur_cd2_url="$(get_env_val MF_CD2_URL "http://${lan_ip}:19798")"
    cur_cd2_token="$(get_env_val MF_CD2_TOKEN "")"
    cur_mp_url="$(get_env_val MF_MP_URL "http://${lan_ip}:3001")"
    cur_mp_token="$(get_env_val MF_MP_TOKEN "")"
    cur_src="$(get_env_val MF_SOURCE_ROOT "/115/Library_incs/nas")"
    cur_arc="$(get_env_val MF_ARCHIVE_ROOT "/115/Library_incs/nas.finish")"
    cur_lib="$(get_env_val MF_LIBRARY_ROOT "$cur_arc")"
    cur_debug="$(get_env_val MF_DEBUG "false")"

    local v_port v_cd2_url v_cd2_token v_mp_url v_mp_token v_src v_arc v_lib

    read -r -p "1. Web 服务端口 [默认: ${cur_port}]: " v_port
    v_port="${v_port:-$cur_port}"

    read -r -p "2. CloudDrive2 地址 (勿填127.0.0.1) [当前: ${cur_cd2_url}]: " v_cd2_url
    v_cd2_url="${v_cd2_url:-$cur_cd2_url}"
    v_cd2_url="$(check_localhost_url "CloudDrive2 地址" "$v_cd2_url")"

    local mask_cd2="${cur_cd2_token:+已设置(回车保留)}"
    read -r -p "3. CloudDrive2 Token [${mask_cd2:-必填}]: " v_cd2_token
    v_cd2_token="${v_cd2_token:-$cur_cd2_token}"

    read -r -p "4. MoviePilot 地址 (勿填127.0.0.1) [当前: ${cur_mp_url}]: " v_mp_url
    v_mp_url="${v_mp_url:-$cur_mp_url}"
    v_mp_url="$(check_localhost_url "MoviePilot 地址" "$v_mp_url")"

    local mask_mp="${cur_mp_token:+已设置(回车保留)}"
    read -r -p "5. MoviePilot API Key [${mask_mp:-必填}]: " v_mp_token
    v_mp_token="${v_mp_token:-$cur_mp_token}"

    read -r -p "6. 待整理源目录路径 MF_SOURCE_ROOT [当前: ${cur_src}]: " v_src
    v_src="${v_src:-$cur_src}"

    read -r -p "7. 归档目标根路径 MF_ARCHIVE_ROOT [当前: ${cur_arc}]: " v_arc
    v_arc="${v_arc:-$cur_arc}"

    read -r -p "8. Emby成品媒体库根路径 MF_LIBRARY_ROOT (一般直接回车同归档路径) [默认: ${v_arc}]: " v_lib
    v_lib="${v_lib:-$v_arc}"

    # 保留用户可能手动添加的其他可选配置（如 MF_TMDB_API_KEY / MF_GAP_FILL_*）
    local extra_lines=""
    if [ -f "$ENV_FILE" ]; then
        extra_lines="$(grep -E '^MF_(TMDB_|GAP_FILL_)' "$ENV_FILE" || true)"
    fi

    cat > "$ENV_FILE" << EOF
# MediaFlow 环境配置（由交互式向导生成）
MF_DEBUG=${cur_debug}
MF_PORT=${v_port}
MF_DATA_DIR=/app/data
# MF_AUTH_TOKEN=  # 注意：网页端无登录框，浏览器直接访问请保持注释留空；公网建议在 Nginx/Lucky 反代层加密码

# CloudDrive2 连接
MF_CD2_URL=${v_cd2_url}
MF_CD2_TOKEN=${v_cd2_token}

# MoviePilot 连接
MF_MP_URL=${v_mp_url}
MF_MP_TOKEN=${v_mp_token}

# 路径映射
MF_SOURCE_ROOT=${v_src}
MF_ARCHIVE_ROOT=${v_arc}
MF_LIBRARY_ROOT=${v_lib}
EOF

    if [ -n "$extra_lines" ]; then
        printf "\n# 其他扩展配置\n%s\n" "$extra_lines" >> "$ENV_FILE"
    fi

    chmod 600 "$ENV_FILE"
    ok "配置已保存至 ${ENV_FILE}"
}

# 等待并验证服务健康状态
wait_and_verify() {
    local port
    port="$(get_port)"
    local lan_ip
    lan_ip="$(get_lan_ip)"

    info "正在等待服务就绪并验证版本..."
    for _ in $(seq 1 20); do
        if curl -sf "http://127.0.0.1:${port}/api/system/health" >/dev/null 2>&1; then
            local ver
            ver="$(curl -s "http://127.0.0.1:${port}/api/system/version" 2>/dev/null || echo '未知')"
            ok "MediaFlow 启动成功！"
            echo -e "  ${GREEN}访问地址:${NC} http://${lan_ip}:${port}"
            echo -e "  ${GREEN}当前版本:${NC} ${ver}"
            return 0
        fi
        sleep 2
    done

    err "服务在 40 秒内未通过健康检查，建议在主菜单选择 [6] 查看实时日志排查。"
    return 1
}

# 备份 SQLite 数据库（调用前需确保容器已停止或允许冷备）
backup_db() {
    local tag="${1:-manual}"
    mkdir -p "$BACKUP_DIR"
    if [ ! -f "$DATA_DIR/mediaflow.db" ]; then
        warn "未发现既有数据库文件 (${DATA_DIR}/mediaflow.db)，跳过备份。"
        return 0
    fi
    local stamp
    stamp="$(date +%Y%m%d_%H%M%S)"
    local dst="$BACKUP_DIR/mediaflow_${stamp}_${tag}.db"
    cp "$DATA_DIR/mediaflow.db" "$dst"
    ls -1t "$BACKUP_DIR"/mediaflow_*.db 2>/dev/null | tail -n +8 | xargs -r rm -f
    ok "数据库已备份至: ${dst}（自动保留最近 7 份）"
}

# ==================== 菜单动作 ====================

action_install() {
    ensure_deps || return 1
    mkdir -p "$DATA_DIR" "$BACKUP_DIR"

    if [ -d "$SRC_DIR/.git" ]; then
        warn "检测到源码目录已存在 ($SRC_DIR)，将跳过克隆并同步最新代码..."
        git -C "$SRC_DIR" fetch origin main && git -C "$SRC_DIR" checkout main && git -C "$SRC_DIR" pull --ff-only origin main || {
            err "拉取代码失败，请检查网络连接 GitHub 是否畅通。"
            return 1
        }
    else
        info "正在从 GitHub 稀疏拉取 mediaflow-docker 源码（体积极小）..."
        if ! git clone --filter=blob:none --sparse "$REPO_URL" "$SRC_DIR" 2>/dev/null; then
            warn "稀疏克隆失败（可能 Git 版本较旧），自动切换标准克隆..."
            rm -rf "$SRC_DIR"
            git clone "$REPO_URL" "$SRC_DIR" || { err "克隆仓库失败，请检查网络或仓库权限。"; return 1; }
        else
            git -C "$SRC_DIR" sparse-checkout set mediaflow-docker
        fi
    fi

    write_compose_file
    ok "已生成编排文件: ${COMPOSE_FILE}"

    if [ -f "$ENV_FILE" ]; then
        read -r -p "检测到已有 .env 配置文件，是否重新配置？[y/N]: " reconf
        if [[ "${reconf:-N}" =~ ^[Yy]$ ]]; then
            configure_env_wizard
        fi
    else
        configure_env_wizard
    fi

    info "开始构建 Docker 镜像并启动 MediaFlow（首次构建约需 1~2 分钟）..."
    cd "$BASE_DIR" || return 1
    if ! docker compose up -d --build; then
        err "构建或启动失败，请检查上方报错信息。"
        return 1
    fi

    install_shortcut
    wait_and_verify
    echo ""
    ok "已创建快捷命令：以后随时在终端输入 ${BOLD}mf${NC} 即可打开本管理面板！"
}

action_upgrade() {
    if [ ! -d "$SRC_DIR/.git" ] || [ ! -f "$COMPOSE_FILE" ]; then
        err "尚未完成首次安装，请先选择 [1] 执行首次安装。"
        return 1
    fi

    cd "$SRC_DIR" || return 1
    # 若之前做过版本回滚处于 detached HEAD，先切回 main
    local cur_branch
    cur_branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo 'DETACHED')"
    if [ "$cur_branch" != "main" ]; then
        warn "当前源码处于历史回滚状态 ($cur_branch)，将切回 main 分支检查最新更新..."
        git checkout main || { err "切回 main 分支失败"; return 1; }
    fi

    local old_rev remote_rev
    old_rev="$(git rev-parse --short HEAD)"
    info "正在连接 GitHub 检查更新..."
    git fetch origin main || { err "连接 GitHub 失败，已中止升级（原服务不受影响）。"; return 1; }
    remote_rev="$(git rev-parse --short origin/main)"

    if [ "$old_rev" = "$remote_rev" ]; then
        ok "当前已是最新版本 (${old_rev})！"
        read -r -p "是否仍要强制重新构建镜像？[y/N]: " force_ans
        if [[ ! "${force_ans:-N}" =~ ^[Yy]$ ]]; then
            return 0
        fi
    else
        echo ""
        echo -e "${BOLD}发现新版本更新 (${old_rev} -> ${remote_rev})：${NC}"
        git log --oneline "${old_rev}..origin/main"
        echo ""
        warn "升级前请确认网页端没有正在运行的「初检扫描」或「批量清理/归档」任务。"
        read -r -p "确认开始平滑升级？[Y/n]: " go_ans
        if [[ ! "${go_ans:-Y}" =~ ^[Yy]$ ]]; then
            info "已取消升级。"
            return 0
        fi
        git pull --ff-only origin main || { err "git pull 失败，请检查 $SRC_DIR 是否有本地冲突。"; return 1; }
    fi

    local new_rev
    new_rev="$(git rev-parse --short HEAD)"

    # 若仓库中同步更新了 mf.sh，一并刷新本地管理脚本
    if [ -f "$SRC_DIR/mediaflow-docker/mf.sh" ]; then
        cp -f "$SRC_DIR/mediaflow-docker/mf.sh" "$SELF_TARGET"
        chmod +x "$SELF_TARGET"
    fi

    info "阶段 1/3：预构建新镜像（旧容器持续运行，若失败不中断服务）..."
    cd "$BASE_DIR" || return 1
    if ! docker compose build; then
        err "新镜像构建失败！旧容器仍在正常运行，未受影响。"
        return 1
    fi

    info "阶段 2/3：停服备份数据库并切换至新镜像（中断约 2 秒）..."
    docker compose stop mediaflow >/dev/null 2>&1 || true
    backup_db "$old_rev"
    if ! docker compose up -d; then
        err "新容器启动失败！"
        return 1
    fi

    info "阶段 3/3：清理悬空旧镜像并验证健康状态..."
    docker image prune -f >/dev/null 2>&1 || true
    wait_and_verify
    ok "版本切换完成: ${old_rev} -> ${new_rev}"
}

action_edit_env() {
    configure_env_wizard
    if [ -f "$COMPOSE_FILE" ]; then
        read -r -p "是否立即应用新配置并重建容器使其生效？[Y/n]: " ans
        if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then
            cd "$BASE_DIR" || return 1
            docker compose up -d && wait_and_verify
        fi
    fi
}

action_restart() {
    [ -f "$COMPOSE_FILE" ] || { err "尚未安装，请先执行 [1]。"; return 1; }
    cd "$BASE_DIR" || return 1
    info "正在重启 MediaFlow 容器..."
    docker compose restart && wait_and_verify
}

action_stop() {
    [ -f "$COMPOSE_FILE" ] || { err "尚未安装。"; return 1; }
    cd "$BASE_DIR" || return 1
    docker compose stop && ok "MediaFlow 已停止。"
}

action_logs() {
    [ -f "$COMPOSE_FILE" ] || { err "尚未安装。"; return 1; }
    cd "$BASE_DIR" || return 1
    info "正在显示实时日志（按 Ctrl+C 退出并返回菜单）..."
    echo "--------------------------------------------------"
    ( trap 'exit 0' INT; docker compose logs -f --tail=100 )
}

action_manual_backup() {
    [ -f "$COMPOSE_FILE" ] || { err "尚未安装。"; return 1; }
    cd "$BASE_DIR" || return 1
    local was_running=0
    if docker compose ps --status running 2>/dev/null | grep -q mediaflow; then
        was_running=1
        info "为确保 SQLite WAL 数据完全落盘，临时暂停服务 2 秒进行冷备..."
        docker compose stop mediaflow >/dev/null 2>&1 || true
    fi
    backup_db "manual"
    if [ "$was_running" -eq 1 ]; then
        docker compose start mediaflow >/dev/null 2>&1 || true
        ok "服务已恢复运行。"
    fi
}

action_rollback() {
    [ -f "$COMPOSE_FILE" ] || { err "尚未安装。"; return 1; }
    mapfile -t backups < <(ls -1t "$BACKUP_DIR"/mediaflow_*.db 2>/dev/null || true)
    if [ ${#backups[@]} -eq 0 ]; then
        warn "暂无可用的历史备份文件 ($BACKUP_DIR)。"
        return 0
    fi

    echo ""
    echo -e "${BOLD}可用的历史数据库备份（按时间倒序）：${NC}"
    local idx=1
    for f in "${backups[@]}"; do
        echo "  ${idx}. $(basename "$f") ($(du -h "$f" | awk '{print $1}'))"
        idx=$((idx + 1))
    done
    echo "  0. 取消"
    echo ""
    read -r -p "请输入要恢复的备份序号: " pick
    if [[ ! "$pick" =~ ^[0-9]+$ ]] || [ "$pick" -eq 0 ] || [ "$pick" -gt ${#backups[@]} ]; then
        info "已取消回滚。"
        return 0
    fi

    local chosen="${backups[$((pick - 1))]}"
    local bname
    bname="$(basename "$chosen" .db)"
    local rev_tag="${bname##*_}"

    warn "即将恢复数据库为: $(basename "$chosen")"
    read -r -p "确认继续？[y/N]: " confirm
    [[ "${confirm:-N}" =~ ^[Yy]$ ]] || { info "已取消。"; return 0; }

    cd "$BASE_DIR" || return 1
    docker compose stop mediaflow >/dev/null 2>&1 || true
    cp "$DATA_DIR/mediaflow.db" "$BACKUP_DIR/mediaflow_$(date +%Y%m%d_%H%M%S)_before_rollback.db" 2>/dev/null || true
    cp -f "$chosen" "$DATA_DIR/mediaflow.db"
    rm -f "$DATA_DIR/mediaflow.db-wal" "$DATA_DIR/mediaflow.db-shm"
    ok "数据库已恢复。"

    if [ "$rev_tag" != "manual" ] && [ "$rev_tag" != "rollback" ] && git -C "$SRC_DIR" cat-file -e "${rev_tag}^{commit}" 2>/dev/null; then
        read -r -p "检测到该备份对应代码提交 ${rev_tag}，是否同时将代码回退至 ${rev_tag} 并重建镜像？[Y/n]: " code_rb
        if [[ "${code_rb:-Y}" =~ ^[Yy]$ ]]; then
            git -C "$SRC_DIR" checkout "$rev_tag"
            docker compose up -d --build && wait_and_verify
            return 0
        fi
    fi

    docker compose up -d && wait_and_verify
}

action_status() {
    local port lan_ip
    port="$(get_port)"
    lan_ip="$(get_lan_ip)"
    echo ""
    echo -e "${BOLD}========== MediaFlow 运行状态 ==========${NC}"
    echo "  安装目录: $BASE_DIR"
    if [ -d "$SRC_DIR/.git" ]; then
        echo "  源码提交: $(git -C "$SRC_DIR" log -1 --oneline 2>/dev/null || echo '未知')"
    else
        echo "  源码状态: 未克隆"
    fi
    if [ -f "$COMPOSE_FILE" ]; then
        cd "$BASE_DIR" && docker compose ps
    fi
    if curl -sf "http://127.0.0.1:${port}/api/system/health" >/dev/null 2>&1; then
        echo -e "  接口状态: ${GREEN}🟢 正常${NC}"
        echo -e "  运行版本: ${GREEN}$(curl -s "http://127.0.0.1:${port}/api/system/version")${NC}"
        echo -e "  访问地址: ${CYAN}http://${lan_ip}:${port}${NC}"
    else
        echo -e "  接口状态: ${RED}🔴 未运行或异常${NC}"
    fi
    if [ -d "$DATA_DIR" ]; then
        echo "  数据占用: $(du -sh "$DATA_DIR" 2>/dev/null | awk '{print $1}')"
    fi
}

# 获取顶部状态栏摘要
render_banner() {
    clear 2>/dev/null || true
    local port status_str ver_str
    port="$(get_port)"
    if curl -sf "http://127.0.0.1:${port}/api/system/health" >/dev/null 2>&1; then
        status_str="${GREEN}🟢 运行中${NC}"
        ver_str="$(curl -s "http://127.0.0.1:${port}/api/system/version" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/v\1/p')"
        ver_str="${ver_str:-运行中}"
    elif [ -f "$COMPOSE_FILE" ]; then
        status_str="${RED}🔴 已停止${NC}"
        ver_str="未启动"
    else
        status_str="${YELLOW}⚪ 未安装${NC}"
        ver_str="-"
    fi

    echo -e "${CYAN}========================================================${NC}"
    echo -e "  ${BOLD}🎬 MediaFlow Ubuntu 一键管理面板${NC}"
    echo -e "  路径: ${BASE_DIR}"
    echo -e "  状态: ${status_str} | 版本: ${BOLD}${ver_str}${NC} | 端口: ${port}"
    echo -e "${CYAN}========================================================${NC}"
    echo "   1. 🚀 首次安装 / 一键部署（拉取代码 + 引导配置 + 启动）"
    echo "   2. ⬆️  一键安全升级（预构建 + 自动备库 + 2秒平滑切换）"
    echo "   3. ⚙️  修改配置参数（交互式修改 .env 并自动生效）"
    echo "   4. 🔄 重启服务"
    echo "   5. ⏹️  停止服务"
    echo "   6. 📋 查看实时日志（按 Ctrl+C 返回菜单）"
    echo "   7. 💾 手动备份数据库"
    echo "   8. ⏪ 回滚到历史备份版本"
    echo "   9. 🔍 查看详细状态与访问地址"
    echo "   0. 🚪 退出"
    echo -e "${CYAN}========================================================${NC}"
}

main() {
    install_shortcut
    while true; do
        render_banner
        read -r -p "请输入选项数字 [0-9]: " choice
        case "$choice" in
            1) action_install; pause_key ;;
            2) action_upgrade; pause_key ;;
            3) action_edit_env; pause_key ;;
            4) action_restart; pause_key ;;
            5) action_stop; pause_key ;;
            6) action_logs ;;
            7) action_manual_backup; pause_key ;;
            8) action_rollback; pause_key ;;
            9) action_status; pause_key ;;
            0) echo "再见！随时输入 mf 可再次打开面板。"; exit 0 ;;
            *) warn "无效选项，请输入 0~9 之间的数字。"; sleep 1 ;;
        esac
    done
}

main "$@"
