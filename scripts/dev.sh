#!/usr/bin/env bash
# 开发循环：把源码目录同步到 WSL/Linux 运行目录，然后跑编译检查 + 自检 +（可选）测试。
#
#   bash scripts/dev.sh                 # 同步 + compileall + selftest
#   bash scripts/dev.sh -m pytest -q    # 再加跑 pytest
#
# 源码目录默认取本脚本所在仓库的根目录（所以仓库换位置也不用改）。
# 可用环境变量覆盖：SRC / DST / PY / DATA
set -euo pipefail

# 本脚本在 <仓库根>/scripts/ 下，向上一级就是源码目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# 同步目标只是本机的临时运行目录，换名字不影响仓库（.env 不会被覆盖）
DST="${DST:-$HOME/warehouse-bot}"
PY="${PY:-$HOME/.venvs/warehouse/bin/python}"
DATA="${DATA:-$HOME/warehouse-data}"

if [ ! -x "$PY" ]; then
  echo "找不到虚拟环境解释器：$PY" >&2
  echo "先跑 bash scripts/deploy.sh 装依赖，或设 PY=... 指向别的解释器" >&2
  exit 1
fi

if [ ! -f "$SRC/app/main.py" ]; then
  echo "源码目录看起来不对（$SRC 下没有 app/main.py）" >&2
  exit 1
fi

mkdir -p "$DST" "$DATA"
echo "==> 同步 $SRC → $DST"
# 排除运行期产物，别把本地库/密钥同步过去
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.env' --exclude 'data/' --exclude '*.db' --exclude '*.db-wal' \
    --exclude '*.db-shm' --exclude '.venv/' --exclude '__pycache__/' \
    --exclude '.pytest_cache/' --exclude '.git/' \
    "$SRC/" "$DST/"
else
  cp -r "$SRC/." "$DST/"
fi

if [ ! -f "$DST/.env" ]; then
  echo "==> 生成开发用 .env（数据库放在 $DATA）"
  # 随机生成引导 Key，别再给一个能猜到的默认值
  BOOTSTRAP="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  cat > "$DST/.env" <<EOF
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=info
DATABASE_PATH=$DATA/inventory.db
DATA_DIR=$DATA
BOOTSTRAP_API_KEY=$BOOTSTRAP
FUZZY_THRESHOLD=0.55

# 需要 AI 分析 / AI 规范化导入时填这里
LLM_ENABLED=false
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=
LLM_MODEL=deepseek-chat

# QQ 官方机器人
QQ_BOT_ENABLED=false
QQ_APP_ID=
QQ_APP_SECRET=
QQ_SANDBOX=false
EOF
  chmod 600 "$DST/.env"
  echo "    引导 Key（把它填进 X-API-Key）：$BOOTSTRAP"
fi

cd "$DST"
echo "==> 语法编译"
"$PY" -m compileall -q app scripts tests
echo "    OK"

echo "==> 核心自检"
"$PY" scripts/selftest.py

if [ "$#" -gt 0 ]; then
  echo "==> 额外命令：$*"
  "$PY" "$@"
fi
