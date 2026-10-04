#!/usr/bin/env bash
# ============================================================================
#  DocuDot 仓储助手 · 一键部署（Linux 服务器 / 云主机）
#
#    bash scripts/deploy.sh
#
# 一条命令走完：检查环境 → 建虚拟环境 → 装依赖 → 问你要凭证 → 写 .env
#              → 自检 → 装成开机自启服务并启动。
#
# 它只问你几件事：API Key（可直接回车自动生成）、QQ AppID、QQ AppSecret，
# 以及「要不要让公网/局域网访问接口」。
#
#  常用参数：
#    --host <地址>      绑定的地址；默认 127.0.0.1（只本机），0.0.0.0 = 对外开放
#    --port <端口>      默认 8000
#    --service          直接装成 systemd 服务（不问了）
#    --no-service       不装服务，用 nohup 后台跑
#    --foreground       前台跑（调试用，Ctrl-C 退出）
#    --force            覆盖已有 .env（默认会先备份）
#    --non-interactive  全部从环境变量读，不交互（脚本化部署）
#    --no-start         只装依赖和写配置，不启动
#    -h | --help        看帮助
#
#  非交互模式从这些环境变量读：
#    BOOTSTRAP_API_KEY  QQ_APP_ID  QQ_APP_SECRET  LLM_API_KEY  HOST  PORT  DATA_DIR
# ============================================================================
set -euo pipefail

readonly GREEN=$'\033[32m'; readonly YELLOW=$'\033[33m'
readonly RED=$'\033[31m';   readonly BOLD=$'\033[1m'; readonly OFF=$'\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PY="$VENV_DIR/bin/python"
ENV_FILE="$ROOT_DIR/.env"
SERVICE_NAME="docupoint"

INTERACTIVE=1
START=1
FOREGROUND=0
SERVICE_MODE="ask"          # ask | yes | no
FORCE=0
HOST_CLI=""
PORT_CLI=""

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }
hr()   { printf '%s\n' "────────────────────────────────────────────────────────────"; }
ask()  { # ask "提示" 变量名 [默认值]
  local prompt="$1" var="$2" default="${3:-}" answer=""
  if [ -n "$default" ]; then printf '  %s [%s]: ' "$prompt" "$default"
  else printf '  %s: ' "$prompt"; fi
  read -r answer
  printf -v "$var" '%s' "${answer:-$default}"
}

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

# ------------------------------------------------------------------ 参数
while [ $# -gt 0 ]; do
  case "$1" in
    --host)             shift; HOST_CLI="${1:-}" ;;
    --port)             shift; PORT_CLI="${1:-}" ;;
    --service)          SERVICE_MODE="yes" ;;
    --no-service)       SERVICE_MODE="no" ;;
    --foreground)       FOREGROUND=1; SERVICE_MODE="no" ;;
    --force)            FORCE=1 ;;
    --non-interactive)  INTERACTIVE=0 ;;
    --no-start)         START=0 ;;
    -h|--help)          usage ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
  shift
done

printf '\n%sDocuDot 仓储助手 · 一键部署%s\n' "$BOLD" "$OFF"
hr

# ------------------------------------------------------------------ ① 环境
say "① 检查运行环境"
if [ "$(uname -s)" != "Linux" ]; then
  warn "当前不是 Linux（$(uname -s)），脚本仍会尝试继续"
fi

SUDO=""
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    SUDO="sudo"                      # 免密 sudo，随便用
  elif [ "$INTERACTIVE" -eq 1 ]; then
    SUDO="sudo"                      # 交互模式：让 sudo 自己提示输密码
  else
    # ⚠️ 非交互 + sudo 需要密码：绝不能用 sudo —— 它会卡在密码提示上把部署挂死
    SUDO=""
    warn "当前用户没有免密 sudo，非交互模式下不会尝试提权（装不了系统服务，改用后台进程）"
  fi
fi

need_pkg() {  # need_pkg 包名 用途
  local pkg="$1" why="$2"
  if [ -z "$SUDO" ]; then
    die "缺少 $pkg（$why），且当前没有 sudo 权限。请让管理员执行：apt install -y $pkg"
  fi
  warn "缺少 $pkg（$why）"
  if [ "$INTERACTIVE" -eq 1 ]; then
    local answer=""
    ask "用 apt 装上可以吗？(y/n)" answer "y"
    [ "$answer" = "y" ] || die "缺 $pkg 装不下去，请手动安装后重跑"
  fi
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$pkg" || die "安装 $pkg 失败"
  ok "已安装 $pkg"
}

if ! command -v python3 >/dev/null 2>&1; then
  need_pkg python3 "运行本服务需要"
fi
PY_BOOT="$(command -v python3)"
PY_VERSION="$("$PY_BOOT" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VERSION%%.*}"; PY_MINOR="${PY_VERSION##*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  die "需要 Python 3.10+，当前 $PY_VERSION"
fi
ok "Python $PY_VERSION（$PY_BOOT）"

if ! "$PY_BOOT" -c 'import venv' >/dev/null 2>&1; then
  need_pkg "python3-venv" "建虚拟环境需要"
fi
ok "venv 模块可用"

[ -f "$ROOT_DIR/requirements.txt" ] || die "找不到 requirements.txt"
[ -f "$ROOT_DIR/app/main.py" ] || die "找不到 app/main.py"
ok "项目文件齐全（$ROOT_DIR）"

command -v curl >/dev/null 2>&1 || warn "没有 curl，稍后无法自动做健康检查（不影响运行）"

# ------------------------------------------------------------------ ② 依赖
say ""
say "② 准备虚拟环境与依赖"
if [ ! -x "$PY" ]; then
  "$PY_BOOT" -m venv "$VENV_DIR"
  ok "已创建虚拟环境 $VENV_DIR"
else
  ok "复用已有虚拟环境 $VENV_DIR"
fi
"$PY" -m pip install --quiet --upgrade pip
say "   安装依赖中（第一次约 1-2 分钟）…"
"$PY" -m pip install --quiet -r "$ROOT_DIR/requirements.txt" \
  || die "依赖安装失败：检查网络，或先装 build-essential python3-dev"
ok "依赖安装完成"

# ------------------------------------------------------------------ ③ 凭证
say ""
say "③ 配置凭证"

if [ "$INTERACTIVE" -eq 0 ]; then
  BOOTSTRAP_API_KEY="${BOOTSTRAP_API_KEY:-}"
  QQ_APP_ID="${QQ_APP_ID:-}"
  QQ_APP_SECRET="${QQ_APP_SECRET:-}"
  LLM_API_KEY="${LLM_API_KEY:-}"
  HOST="${HOST_CLI:-${HOST:-127.0.0.1}}"
  PORT="${PORT_CLI:-${PORT:-8000}}"
  DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
else
  gen_key() { head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32; }

  say "  ① API Key —— 调接口用。直接回车，我生成一个 32 位强随机 Key"
  ask "API Key（回车自动生成）" BOOTSTRAP_API_KEY ""
  if [ -z "$BOOTSTRAP_API_KEY" ]; then
    BOOTSTRAP_API_KEY="$(gen_key)"
    ok "已生成：$BOOTSTRAP_API_KEY"
  fi

  say ""
  say "  ② QQ 机器人 AppID —— QQ 开放平台 → 你的机器人 → 开发设置"
  say "     不接 QQ 就直接回车（接口照样能用）"
  ask "QQ_APP_ID" QQ_APP_ID ""

  say ""
  say "  ③ QQ 机器人 AppSecret —— 和 AppID 配套，在同一个页面"
  ask "QQ_APP_SECRET" QQ_APP_SECRET ""

  say ""
  say "  ④（可选）DeepSeek API Key —— 不填就没有 AI 归类/分析，回车跳过"
  ask "LLM_API_KEY" LLM_API_KEY ""

  HOST="${HOST_CLI:-}"
  PORT="${PORT_CLI:-}"
  DATA_DIR="$ROOT_DIR/data"
fi

PORT="${PORT_CLI:-${PORT:-8000}}"
DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"

# 绑定地址：云服务器上默认只本机 —— QQ 机器人是**主动外连**，不需要开任何入站端口
if [ -z "${HOST:-}" ]; then
  if [ "$INTERACTIVE" -eq 0 ]; then
    HOST="127.0.0.1"
  else
    say ""
    say "  ⑤ 接口访问范围"
    say "     ① 只本机（推荐）—— QQ 机器人照常工作，公网碰不到这个端口"
    say "     ② 对外开放 —— 你需要在手机/别的机器上调接口时选它"
    printf '     选 [1/2，默认 1]: '
    read -r scope
    if [ "$scope" = "2" ]; then HOST="0.0.0.0"; else HOST="127.0.0.1"; fi
  fi
fi

# 校验
if [ -n "${QQ_APP_ID:-}" ] && [ -z "${QQ_APP_SECRET:-}" ]; then
  die "填了 QQ_APP_ID 却没有 QQ_APP_SECRET —— 两个要一起给，或者都留空"
fi
if [ -z "${QQ_APP_ID:-}" ] && [ -n "${QQ_APP_SECRET:-}" ]; then
  die "填了 QQ_APP_SECRET 却没有 QQ_APP_ID —— 两个要一起给，或者都留空"
fi
if [ -n "${QQ_APP_ID:-}" ] && ! printf '%s' "$QQ_APP_ID" | grep -qE '^[0-9]+$'; then
  die "QQ_APP_ID 应该是纯数字，你填的是：$QQ_APP_ID"
fi
[ -n "${BOOTSTRAP_API_KEY:-}" ] || die "API Key 不能为空（它决定谁能调你的接口）"
[ "${#BOOTSTRAP_API_KEY}" -ge 16 ] || warn "API Key 只有 ${#BOOTSTRAP_API_KEY} 位，建议至少 16 位"

QQ_ENABLED=false; [ -n "${QQ_APP_ID:-}" ] && QQ_ENABLED=true
LLM_ENABLED=false; [ -n "${LLM_API_KEY:-}" ] && LLM_ENABLED=true

if [ "$HOST" = "0.0.0.0" ]; then
  say ""
  warn "你选了对外开放。请务必做两件事："
  say  "     1. 云厂商控制台的**安全组**里放行 $PORT 端口（否则外面还是连不上）"
  say  "     2. 只用 API Key 调用，别把 Key 贴到公开地方"
  say  "     想更稳妥就用 Nginx 反代 + HTTPS，然后把这个端口只对 Nginx 开放。"
fi

# ------------------------------------------------------------------ ④ 写配置
say ""
say "④ 写入配置"
if [ -f "$ENV_FILE" ] && [ "$FORCE" -eq 0 ]; then
  BACKUP="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$ENV_FILE" "$BACKUP"
  warn "已存在 .env，旧文件备份到：$BACKUP"
fi
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true

cat > "$ENV_FILE" <<EOF
# ---- 由 scripts/deploy.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S') ----
# 这个文件含密钥，已在 .gitignore 里，**不要提交到 Git**
HOST=$HOST
PORT=$PORT
LOG_LEVEL=info
DATABASE_PATH=$DATA_DIR/inventory.db
DATA_DIR=$DATA_DIR
BOOTSTRAP_API_KEY=$BOOTSTRAP_API_KEY
FUZZY_THRESHOLD=0.55

# ---- 大模型（AI 归类 / 分析，可选）----
LLM_ENABLED=$LLM_ENABLED
LLM_BASE_URL=${LLM_BASE_URL:-https://api.deepseek.com/v1}
LLM_API_KEY=${LLM_API_KEY:-}
LLM_MODEL=${LLM_MODEL:-deepseek-chat}

# ---- QQ 官方机器人 ----
QQ_BOT_ENABLED=$QQ_ENABLED
QQ_APP_ID=${QQ_APP_ID:-}
QQ_APP_SECRET=${QQ_APP_SECRET:-}
QQ_SANDBOX=false
EOF
chmod 600 "$ENV_FILE"
ok "已写入 $ENV_FILE（权限 600）"
ok "数据目录 $DATA_DIR（数据库就放这儿，备份直接拷这个文件）"

# ------------------------------------------------------------------ ⑤ 自检
say ""
say "⑤ 启动前自检"
( cd "$ROOT_DIR" && "$PY" -m compileall -q app scripts tests ) || die "代码编译失败"
ok "语法编译通过"
( cd "$ROOT_DIR" && "$PY" scripts/selftest.py >/dev/null ) && ok "核心引擎自检通过" \
  || die "核心自检未通过，先别启动"

# ------------------------------------------------------------------ ⑥ 启动
URL="http://127.0.0.1:$PORT"
SERVICE_INSTALLED=0

run_foreground() {
  say "   前台运行，Ctrl-C 退出。文档：$URL/docs"
  hr
  cd "$ROOT_DIR" && exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
}

run_nohup() {
  local log="$DATA_DIR/server.log" pidfile="$DATA_DIR/server.pid"
  cd "$ROOT_DIR"
  setsid nohup "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
    > "$log" 2>&1 < /dev/null &
  echo $! > "$pidfile"
  for _ in $(seq 1 30); do
    command -v curl >/dev/null 2>&1 && curl -sf "$URL/health" >/dev/null 2>&1 && break
    sleep 1
  done
  if command -v curl >/dev/null 2>&1 && curl -sf "$URL/health" >/dev/null 2>&1; then
    ok "服务已就绪（PID $(cat "$pidfile")）"
  else
    warn "还没就绪，看日志：tail -f $log"
  fi
  say "   日志：$log"
  say "   停止：kill \$(cat $pidfile)"
}

install_system_service() {
  local unit="/etc/systemd/system/${SERVICE_NAME}.service"
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
[Unit]
Description=DocuDot 仓储助手（QQ 机器人库存后端）
Documentation=file://$ROOT_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$ROOT_DIR
ExecStart=$PY -m uvicorn app.main:app --host $HOST --port $PORT
Restart=always
RestartSec=5
# 基本加固：不给提权、独立临时目录
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  $SUDO install -m 644 "$tmp" "$unit" || { rm -f "$tmp"; return 1; }
  rm -f "$tmp"
  $SUDO systemctl daemon-reload || return 1
  $SUDO systemctl enable --now "${SERVICE_NAME}.service" || return 1
  for _ in $(seq 1 30); do
    command -v curl >/dev/null 2>&1 && curl -sf "$URL/health" >/dev/null 2>&1 && break
    sleep 1
  done
  ok "已装成系统服务并启动（开机自启）"
  say "   状态：systemctl status $SERVICE_NAME"
  say "   日志：journalctl -u $SERVICE_NAME -f"
  say "   重启：systemctl restart $SERVICE_NAME"
  return 0
}

if [ "$START" -eq 1 ]; then
  say ""
  say "⑥ 启动服务"

  if [ "$FOREGROUND" -eq 1 ]; then
    run_foreground
  fi

  DO_SERVICE=0
  case "$SERVICE_MODE" in
    yes) DO_SERVICE=1 ;;
    no)  DO_SERVICE=0 ;;
    ask)
      if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        if [ "$INTERACTIVE" -eq 1 ]; then
          answer=""
          say "   要不要装成**开机自启的系统服务**？（服务器上推荐）"
          ask "装服务 (y/n)" answer "y"
          [ "$answer" = "y" ] && DO_SERVICE=1
        fi
      else
        warn "这台机器没有可用的 systemd，改用 nohup 后台运行"
      fi
      ;;
  esac

  if [ "$DO_SERVICE" -eq 1 ]; then
    if [ -z "$SUDO" ] && [ "$(id -u)" -ne 0 ]; then
      warn "没有 sudo 权限，装不了系统服务；改用 nohup 后台运行"
      DO_SERVICE=0
    fi
  fi

  if [ "$DO_SERVICE" -eq 1 ]; then
    if install_system_service; then SERVICE_INSTALLED=1; else
      warn "系统服务安装失败，回退到 nohup 后台运行"
      run_nohup
    fi
  else
    run_nohup
  fi
fi

# ------------------------------------------------------------------ 收尾
hr
printf '%s部署完成%s\n\n' "$BOLD$GREEN" "$OFF"
cat <<EOF
  接口地址    $URL
  接口文档    $URL/docs
  健康检查    $URL/health
  $([ "$HOST" = "0.0.0.0" ] && echo "对外地址    http://<这台服务器的公网IP>:$PORT（记得放行安全组）" || echo "访问范围    仅本机（QQ 机器人是主动外连，不需要开入站）")

  ${BOLD}你的 API Key（调接口时放在 X-API-Key 头里）${OFF}
      $BOOTSTRAP_API_KEY
  ${BOLD}请存好 —— .env 里也有这份，别提交到 Git。${OFF}

  QQ 机器人    $([ "$QQ_ENABLED" = true ] && echo "已启用（AppID $QQ_APP_ID）" || echo "未启用（只走 HTTP 接口）")
  AI 能力      $([ "$LLM_ENABLED" = true ] && echo "已启用" || echo "未启用（未填 LLM_API_KEY）")
  服务方式     $([ "$SERVICE_INSTALLED" -eq 1 ] && echo "systemd 系统服务（开机自启）" || echo "nohup 后台进程")
  数据文件     $DATA_DIR/inventory.db

  自检一下：
      curl -H "X-API-Key: $BOOTSTRAP_API_KEY" $URL/api/v1/items
  备份就是拷这一个文件：
      cp $DATA_DIR/inventory.db ~/inventory-backup-\$(date +%F).db
EOF
hr

unset BOOTSTRAP_API_KEY QQ_APP_SECRET LLM_API_KEY
