#!/usr/bin/env bash
# 启动服务。默认读项目根目录的 .env。
#
#   bash scripts/run.sh              # 前台启动
#   bash scripts/run.sh --reload     # 开发模式，改代码自动重启
#
# 优先用项目自己的 .venv；没有就用 PY 指定的解释器。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -n "${PY:-}" ]; then
  :
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PY="$PROJECT_DIR/.venv/bin/python"
elif [ -x "$HOME/.venvs/warehouse/bin/python" ]; then
  PY="$HOME/.venvs/warehouse/bin/python"
else
  echo "找不到虚拟环境解释器。先跑 bash scripts/deploy.sh 装依赖，或设 PY=... 指过去。" >&2
  exit 1
fi

cd "$PROJECT_DIR"
# 默认只本机；要对外就设 HOST=0.0.0.0（.env 里的 HOST 也会被 uvicorn 读到）
exec "$PY" -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"
