#!/usr/bin/env bash
# ios-video-dl 一键安装脚本（Debian / Ubuntu，需要 root）
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/bensonYang68/ios-video-dl/main/install.sh)
#
# 可选参数（不传就交互询问）：
#   --domain example.com   用来访问服务的域名（必须已解析到这台服务器）
#   --mode caddy|nginx     caddy：自带 HTTPS（需要 80/443 空闲）；nginx：已有 nginx，生成配置片段自己 include
#   --with-douyin          同时部署抖音解析（Douyin_TikTok_Download_API v5，约多占 4.5G 硬盘、300M 内存）
#   --port 8090            服务在本机监听的端口（nginx 模式用）
#   --yes                  不再确认，直接安装
# 重复运行即升级：已有的密钥、路径、抖音 API Key 都会保留。
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/bensonYang68/ios-video-dl.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/ios-video-dl}"
DTK_DIR="${DTK_DIR:-/opt/dtk}"
DTK_REPO="https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git"
DTK_TAG="${DTK_TAG:-5.1.1}"   # 测试过的版本；新版改了签名可用 DTK_TAG=x.y.z 覆盖

MODE="" DOMAIN="" WITH_DOUYIN=0 ASSUME_YES=0 VDL_PORT=""

c_green=$'\033[32m' c_yellow=$'\033[33m' c_red=$'\033[31m' c_bold=$'\033[1m' c_off=$'\033[0m'
info() { echo "${c_green}==>${c_off} $*"; }
warn() { echo "${c_yellow}[注意]${c_off} $*"; }
die()  { echo "${c_red}[错误]${c_off} $*" >&2; exit 1; }

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --with-douyin) WITH_DOUYIN=1; shift ;;
    --port) VDL_PORT="${2:-}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

# 通过 curl | bash 运行时 stdin 不是终端，交互统一从 /dev/tty 读
ask() {  # ask <提示> <默认值> -> 输出答案
  local reply=""
  if [ "$ASSUME_YES" = 1 ] || [ ! -r /dev/tty ]; then echo "$2"; return; fi
  read -r -p "$1 [$2]: " reply < /dev/tty || true
  echo "${reply:-$2}"
}
confirm() { [ "$ASSUME_YES" = 1 ] && return 0; [[ "$(ask "$1 (y/n)" y)" =~ ^[Yy] ]]; }
env_get() { [ -f "$1" ] && grep -E "^$2=" "$1" | tail -1 | cut -d= -f2- || true; }
port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# ---------- 0. 环境检查 ----------
[ "$(id -u)" = 0 ] || die "请用 root 运行（sudo -i 后再执行）"
if [ -r /etc/os-release ]; then . /etc/os-release; fi
case "${ID:-}" in debian|ubuntu) ;; *) warn "只在 Debian / Ubuntu 上测试过，当前系统：${PRETTY_NAME:-未知}";; esac

info "安装依赖（curl git openssl）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git openssl ca-certificates iproute2 >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  info "安装 Docker（官方脚本 get.docker.com）"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件，请先安装 docker-compose-plugin"
systemctl enable --now docker >/dev/null 2>&1 || true

# ---------- 1. 获取代码 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/docker-compose.yml" ] && [ -d "$SCRIPT_DIR/app" ]; then
  INSTALL_DIR="$SCRIPT_DIR"   # 在克隆好的仓库里直接运行
  info "使用当前目录：$INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
  info "更新代码：$INSTALL_DIR"
  git -C "$INSTALL_DIR" pull -q --ff-only
else
  info "下载代码到 $INSTALL_DIR"
  git clone -q --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
ENV_FILE="$INSTALL_DIR/.env"

# ---------- 2. 域名与模式 ----------
DOMAIN="${DOMAIN:-$(env_get "$ENV_FILE" DOMAIN)}"
while [ -z "$DOMAIN" ]; do
  [ "$ASSUME_YES" = 1 ] && die "非交互模式请用 --domain 指定域名"
  DOMAIN="$(ask "访问服务用的域名（需已解析到本机，例如 dl.example.com）" "")"
done
DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"

if [ -z "$MODE" ]; then
  MODE="$(env_get "$ENV_FILE" VDL_MODE)"
fi
if [ -z "$MODE" ]; then
  if ! port_busy 80 && ! port_busy 443; then MODE=caddy
  elif command -v nginx >/dev/null 2>&1; then MODE=nginx
  else die "80/443 端口已被占用，又没有检测到 nginx。请先释放端口，或用 --mode nginx 配合现有 nginx"
  fi
  MODE="$(ask "HTTPS 方式：caddy（自带证书）/ nginx（已有 nginx）" "$MODE")"
fi
case "$MODE" in caddy|nginx) ;; *) die "--mode 只能是 caddy 或 nginx";; esac
if [ "$MODE" = nginx ] && [[ "$INSTALL_DIR" == /root/* ]]; then
  warn "代码在 $INSTALL_DIR，nginx（www-data 用户）读不到 /root 下的下载文件。建议放到 /opt：INSTALL_DIR=/opt/ios-video-dl"
  confirm "仍然继续？" || exit 1
fi

if [ "$MODE" = caddy ]; then
  if docker ps --format '{{.Names}}' | grep -q caddy; then :; else
    for p in 80 443; do port_busy $p && die "caddy 模式需要 $p 端口空闲（现在被占用），可改用 --mode nginx"; done
  fi
  MYIP="$(curl -fsS4 -m 8 https://api.ipify.org 2>/dev/null || true)"
  DNSIP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')"
  if [ -n "$MYIP" ] && [ "$DNSIP" != "$MYIP" ]; then
    warn "$DOMAIN 解析到 ${DNSIP:-（无）}，本机公网 IP 是 $MYIP。证书申请可能失败（用了 Cloudflare 橙色云朵的话先改成仅 DNS）"
    confirm "仍然继续？" || exit 1
  fi
fi

VDL_PORT="${VDL_PORT:-$(env_get "$ENV_FILE" VDL_PORT)}"
VDL_PORT="${VDL_PORT:-8090}"
[ "$(env_get "$ENV_FILE" DTK_API_KEY)" ] && [ -d "$DTK_DIR/repo" ] && WITH_DOUYIN=1   # 升级时保留抖音模块

echo
info "即将安装：域名 ${c_bold}$DOMAIN${c_off}，模式 ${c_bold}$MODE${c_off}，抖音模块 ${c_bold}$([ $WITH_DOUYIN = 1 ] && echo 开启 || echo 关闭)${c_off}"
confirm "确认开始？" || exit 1

# ---------- 3. 生成 .env（保留已有密钥） ----------
VDL_TOKEN="$(env_get "$ENV_FILE" VDL_TOKEN)"; VDL_TOKEN="${VDL_TOKEN:-$(openssl rand -hex 24)}"
VDL_PATH="$(env_get "$ENV_FILE" VDL_PATH)";   VDL_PATH="${VDL_PATH:-$(openssl rand -hex 6)}"
DTK_API_KEY="$(env_get "$ENV_FILE" DTK_API_KEY)"
COMPOSE_FILE="docker-compose.yml"; [ $WITH_DOUYIN = 1 ] && COMPOSE_FILE="docker-compose.yml:compose.douyin.yml"
umask 077
cat > "$ENV_FILE" <<EOF
# 由 install.sh 生成。VDL_TOKEN 是快捷指令里填的密钥，不要泄露
DOMAIN=$DOMAIN
VDL_MODE=$MODE
VDL_PORT=$VDL_PORT
VDL_TOKEN=$VDL_TOKEN
VDL_PATH=$VDL_PATH
VDL_PUBLIC_BASE=https://$DOMAIN/$VDL_PATH/f
DTK_API_KEY=$DTK_API_KEY
COMPOSE_FILE=$COMPOSE_FILE
COMPOSE_PROFILES=$([ "$MODE" = caddy ] && echo caddy)
EOF
umask 022
mkdir -p data/files data/cookies && chmod 755 data data/files && chmod 700 data/cookies

# ---------- 4. 抖音模块（可选） ----------
setup_douyin() {
  local mem_mb swap_mb
  mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
  swap_mb=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
  if [ "$mem_mb" -lt 3500 ] && [ "$swap_mb" -lt 2000 ] && [ ! -f /swapfile-vdl ]; then
    info "内存较小（${mem_mb}M），增加 swap 到约 2G，防止数据库被 OOM"
    fallocate -l "$((2048 - swap_mb))M" /swapfile-vdl && chmod 600 /swapfile-vdl
    mkswap /swapfile-vdl >/dev/null && swapon /swapfile-vdl
    grep -q swapfile-vdl /etc/fstab || echo "/swapfile-vdl none swap sw 0 0" >> /etc/fstab
    echo "vm.swappiness=10" > /etc/sysctl.d/99-vdl-swappiness.conf && sysctl -q -p /etc/sysctl.d/99-vdl-swappiness.conf
  fi
  local avail_gb; avail_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
  [ "$avail_gb" -ge 6 ] || warn "硬盘只剩 ${avail_gb}G，抖音模块的镜像约 4.5G，可能不够"

  mkdir -p "$DTK_DIR"
  if [ -d "$DTK_DIR/repo/.git" ]; then git -C "$DTK_DIR/repo" pull -q --ff-only || true
  else info "下载 Douyin_TikTok_Download_API"; git clone -q --depth 1 "$DTK_REPO" "$DTK_DIR/repo"; fi
  cd "$DTK_DIR/repo"
  if [ ! -f .env ]; then
    local pp rp; pp=$(openssl rand -hex 24); rp=$(openssl rand -hex 24)
    umask 077
    cat > .env <<EOF
DTK_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')
POSTGRES_PASSWORD=$pp
REDIS_PASSWORD=$rp
DTK_DATABASE_URL=postgresql+asyncpg://dtk:$pp@postgres:5432/dtk
DTK_REDIS_URL=redis://:$rp@redis:6379/0
DTK_BIND_HOST=127.0.0.1
DTK_BIND_PORT=8000
DTK_REDIS_MAXMEMORY=128mb
DTK_IMAGE=evil0ctal/douyin_tiktok_download_api
DTK_IMAGE_TAG=$DTK_TAG
EOF
    umask 022
  fi
  # 小机器资源上限（不装浏览器容器，签名是纯 Python 实现）
  cat > compose.host.yml <<'EOF'
services:
  postgres: { mem_limit: 512m, memswap_limit: 1g }
  redis:    { mem_limit: 160m, memswap_limit: 320m }
  migrate:  { mem_limit: 384m, memswap_limit: 768m, cpus: 1.0 }
  api:      { mem_limit: 384m, memswap_limit: 768m, cpus: 1.0 }
  worker:   { mem_limit: 384m, memswap_limit: 768m, cpus: 1.0 }
EOF
  info "启动抖音解析服务（首次要下载约 4.5G 镜像，请耐心等待）"
  COMPOSE_ENV_FILES=.env docker compose -p dtk -f docker/compose.yml -f compose.host.yml \
    up -d --quiet-pull postgres redis migrate api worker
  for _ in $(seq 1 60); do
    curl -fs -m 3 -o /dev/null http://127.0.0.1:8000/readyz && break; sleep 3
  done
  curl -fs -m 3 -o /dev/null http://127.0.0.1:8000/readyz || warn "抖音解析服务还没就绪，稍后用 vdlctl status 查看"
  cd "$INSTALL_DIR"
}
[ $WITH_DOUYIN = 1 ] && setup_douyin

# ---------- 5. 启动下载服务 ----------
info "构建并启动下载服务"
docker compose up -d --build --remove-orphans >/dev/null
ln -sf "$INSTALL_DIR/scripts/vdlctl" /usr/local/bin/vdlctl
chmod +x "$INSTALL_DIR/scripts/vdlctl"

for _ in $(seq 1 30); do
  curl -fs -m 3 -H "X-Token: $VDL_TOKEN" "http://127.0.0.1:$VDL_PORT/api?id=ping" | grep -q '"code"' && break; sleep 2
done

# ---------- 6. nginx 模式：生成配置片段 ----------
if [ "$MODE" = nginx ]; then
  mkdir -p /etc/nginx/snippets
  cat > /etc/nginx/snippets/ios-video-dl.conf <<EOF
# ios-video-dl（install.sh 生成）：在 $DOMAIN 的 HTTPS server { } 里加一行 include snippets/ios-video-dl.conf;
location = /$VDL_PATH/api {
    proxy_pass http://127.0.0.1:$VDL_PORT;
    proxy_set_header Host \$host;
    proxy_read_timeout 60s;
}
location /$VDL_PATH/f/ {
    alias $INSTALL_DIR/data/files/;
    autoindex off;
    default_type application/octet-stream;
}
EOF
fi

# ---------- 7. 完成 ----------
echo
echo "${c_bold}================ 安装完成 ================${c_off}"
if [ "$MODE" = nginx ]; then
  warn "还差一步：在 nginx 里 $DOMAIN 的 HTTPS server { } 块中加入："
  echo "      include snippets/ios-video-dl.conf;"
  echo "    然后执行：nginx -t && systemctl reload nginx"
  echo
fi
echo "导入快捷指令时填写（以后可用 ${c_bold}vdlctl info${c_off} 再次查看）："
echo "  服务器地址：${c_bold}https://$DOMAIN/$VDL_PATH${c_off}"
echo "  密钥：      ${c_bold}$VDL_TOKEN${c_off}"
echo
echo "测试：vdlctl test \"https://www.youtube.com/watch?v=jNQXAC9IVRw\""
if [ $WITH_DOUYIN = 1 ] && [ -z "$DTK_API_KEY" ]; then
  echo
  warn "抖音模块还需要你配置一次（导入抖音 cookie、创建 API Key），步骤见："
  echo "    vdlctl douyin-guide"
fi
