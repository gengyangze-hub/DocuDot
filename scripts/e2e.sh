#!/usr/bin/env bash
# 端到端验收：用真实 uvicorn 进程 + 真实 HTTP 请求走一遍主要接口。
#
#   bash scripts/e2e.sh
#
# 用的是独立的临时数据库和端口，不影响日常数据。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 解释器优先级：显式 PY > 项目自带 .venv > 家目录下的 venv
if [ -n "${PY:-}" ]; then
  :
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PY="$PROJECT_DIR/.venv/bin/python"
elif [ -x "$HOME/.venvs/warehouse/bin/python" ]; then
  PY="$HOME/.venvs/warehouse/bin/python"
else
  echo "找不到 Python 解释器。先跑 bash scripts/deploy.sh，或设 PY=... 指过去。" >&2
  exit 1
fi
PORT="${PORT:-8123}"
KEY="e2e-admin-key"
DB="$(mktemp -u /tmp/warehouse-e2e-XXXXXX.db)"
LOG="$(mktemp /tmp/warehouse-e2e-XXXXXX.log)"
FAILED=0
PASSED=0

cd "$PROJECT_DIR"

cleanup() {
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$DB" "$DB-wal" "$DB-shm"
}
trap cleanup EXIT

check() { # 名称 实际值 期望子串
  if printf '%s' "$2" | grep -qF -- "$3"; then
    PASSED=$((PASSED + 1)); echo "  ✓ $1"
  else
    FAILED=$((FAILED + 1)); echo "  ✗ $1"
    echo "      期望包含: $3"
    echo "      实际: $(printf '%s' "$2" | head -c 400)"
  fi
}

api() { # api <METHOD> <PATH> [JSON]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -X "$method" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
      -d "$body" "http://127.0.0.1:$PORT$path"
  else
    curl -s -X "$method" -H "X-API-Key: $KEY" "http://127.0.0.1:$PORT$path"
  fi
}

search() { # search <关键词> —— 中文参数必须 URL 编码，否则 h11 直接判非法请求
  curl -s -G -H "X-API-Key: $KEY" --data-urlencode "q=$1" "http://127.0.0.1:$PORT/api/v1/search"
}

echo "==> 启动服务（端口 $PORT，库 $DB）"
DATABASE_PATH="$DB" DATA_DIR="$(dirname "$DB")" BOOTSTRAP_API_KEY="$KEY" \
  QQ_BOT_ENABLED=false LLM_ENABLED=false FUZZY_THRESHOLD=0.55 \
  "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning >"$LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
  if curl -s "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务启动失败，日志："; cat "$LOG"; exit 1
  fi
  sleep 0.5
done

echo "==> 1. 健康检查与鉴权"
check "GET /health" "$(curl -s "http://127.0.0.1:$PORT/health")" '"status":"ok"'
check "无 Key 访问被拒" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/items")" "401"
check "OpenAPI 文档可用" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/docs")" "200"

echo "==> 2. 建条目"
check "新建 STM32" "$(api POST /api/v1/items '{"name":"STM32F103C8T6","category":"stm_component","quantity":25,"location":"A柜-1层-盒3","spec":"LQFP48","aliases":["F103C8","STM32F103"]}')" '"quantity":25'
check "新建电容" "$(api POST /api/v1/items '{"name":"0.1uF 50V MLCC","category":"stm_component","quantity":500,"location":"A柜-2层-盒1","spec":"0805","aliases":["104","100nF"]}')" '"name":"0.1uF 50V MLCC"'
check "新建 Steam 卡" "$(api POST /api/v1/items '{"name":"Steam 50元充值卡","category":"steam_card","quantity":10,"location":"B柜-抽屉1","spec":"50元"}')" 'Steam游戏卡'

echo "==> 3. 模糊匹配（参考仓库语义）"
check "100nF 命中 0.1uF（物理量等价）" "$(api GET '/api/v1/search?q=100nF')" '"name":"0.1uF 50V MLCC"'
check "304 不命中任何东西" "$(api GET '/api/v1/search?q=304zzz')" '"total":0'
check "「电容」按类型归约命中" "$(search 电容)" '0.1uF'
check "「游戏卡」命中 Steam 卡" "$(search 游戏卡)" 'Steam 50元充值卡'
check "0.1uF 全等命中" "$(api GET '/api/v1/search?q=0.1uF')" '0.1uF'
check "100Ω 不误命中电容" "$(search 100Ω)" '"total":0'

echo "==> 4. 出入库"
check "按别名出库" "$(api POST /api/v1/stock/out '{"name":"F103C8","quantity":5,"operator":"qq:10001"}')" '"quantity_after":20'
check "库存不足被拦截" "$(api POST /api/v1/stock/out '{"name":"STM32F103C8T6","quantity":9999}')" '库存不足'
check "入库自动建档" "$(api POST /api/v1/stock/change '{"action":"in","name":"NE555","quantity":30,"location":"C柜-1层","auto_create":true}')" '"created":true'
check "流水已记录" "$(api GET '/api/v1/stock/movements?limit=3')" '"action"'

echo "==> 5. 机器人命令（同一个 CommandRouter，QQ 适配层直接复用）"
check "帮助" "$(api POST /api/v1/bot/command '{"text":"帮助"}')" '入库'
check "自然语言问数量" "$(api POST /api/v1/bot/command '{"text":"STM32 还有多少"}')" '20'
check "自然语言问位置" "$(api POST /api/v1/bot/command '{"text":"NE555 放在哪"}')" 'C柜-1层'
check "入库指令" "$(api POST /api/v1/bot/command '{"text":"入库 1N4148 100 @C柜-2层 #4148"}')" '入库成功'
check "位置查询" "$(api POST /api/v1/bot/command '{"text":"位置 A柜-2层-盒1"}')" '0.1uF'
check "加别名" "$(api POST /api/v1/bot/command '{"text":"别名 NE555 +定时器"}')" '定时器'

echo "==> 5b. 清单化展示 + 多轮上下文 + 整批确认"
check "总览带具体清单" "$(api POST /api/v1/bot/command '{"text":"库存","conversation":"e2e-ctx"}')" '物品清单'
check "分类清单" "$(api POST /api/v1/bot/command '{"text":"STM元器件","conversation":"e2e-ctx"}')" 'STM32F103C8T6'
check "回序号看详情" "$(api POST /api/v1/bot/command '{"text":"2","conversation":"e2e-ctx"}')" '位置：'
check "整批出库先要确认" "$(api POST /api/v1/bot/command '{"text":"出库全部","conversation":"e2e-ctx"}')" '回复「确认」执行'
check "确认前数据不动" "$(api GET '/api/v1/search?q=STM32F103C8T6')" '20'
check "取消整批" "$(api POST /api/v1/bot/command '{"text":"取消","conversation":"e2e-ctx"}')" '已取消'

# 同名多位置 → 列候选 → 回序号完成出库
api POST /api/v1/items '{"name":"AMS1117-3.3","quantity":10,"location":"D柜-1层"}' >/dev/null
api POST /api/v1/items '{"name":"AMS1117-3.3","quantity":20,"location":"D柜-2层"}' >/dev/null
check "同名多位置列候选" "$(api POST /api/v1/bot/command '{"text":"出库 AMS1117-3.3 5","conversation":"e2e-pick"}')" '回复序号'
check "回序号继续出库" "$(api POST /api/v1/bot/command '{"text":"2","conversation":"e2e-pick"}')" '出库成功'
check "选中的那条被扣减" "$(api GET '/api/v1/search?q=AMS1117-3.3')" '15'

echo "==> 6. 导入导出"
MD='# 库存清单

## STM元器件

| 名称 | 数量 | 位置 | 规格 | 别名 |
| --- | --- | --- | --- | --- |
| AMS1117-3.3 | 50 | D柜-1层 | SOT-223 | 1117 |

## Steam游戏卡

| 名称 | 数量 | 位置 | 规格 | 别名 |
| --- | --- | --- | --- | --- |
| Steam 100元充值卡 | 5 | B柜-抽屉2 | 100元 | 100元卡 |
'
check "导入 Markdown 表格" "$(api POST /api/v1/import/commit "$(printf '{"filename":"e2e.md","mode":"merge","content":%s}' "$(printf '%s' "$MD" | "$PY" -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")")" '"created":2'
check "导入后能搜到" "$(api GET '/api/v1/search?q=1117')" 'AMS1117-3.3'
check "导入分类正确（标题即分类）" "$(api GET '/api/v1/items?category=Steam%E6%B8%B8%E6%88%8F%E5%8D%A1')" 'Steam 100元充值卡'
check "导出 Markdown" "$(api GET /api/v1/export/markdown)" '| 名称 | 数量 | 位置 |'
check "导出 Excel" "$(curl -s -H "X-API-Key: $KEY" -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/export/xlsx")" "200"
check "导入模板接口" "$(api GET /api/v1/import/template)" 'ai_prompt'

echo "==> 7. 统计"
check "总览" "$(api GET /api/v1/analysis/overview)" '"item_count"'
check "分类统计" "$(api GET /api/v1/analysis/categories)" 'STM元器件'
check "位置统计" "$(api GET /api/v1/analysis/locations)" '"location"'
check "低库存" "$(api GET '/api/v1/analysis/low-stock?threshold=10')" 'Steam 100元充值卡'
check "NLQ 总览" "$(api POST /api/v1/analysis/nlq '{"question":"总共多少种物品"}')" '种物品'

echo "==> 8. 管理"
check "新建受限 Key" "$(api POST /api/v1/admin/keys '{"label":"readonly","scopes":["read"]}')" '"key":"whk_'
check "审计日志" "$(api GET /api/v1/admin/audit)" 'item.create'
check "运行状态" "$(api GET /api/v1/admin/status)" '"version"'

echo "==> 9. 追问细节 / 省略位置 / 欧姆写法等价 / 已清零不占版面"
check "粘贴清单走文件解析" "$(api POST /api/v1/bot/command '{"text":"| 名称 | 数量 | 位置 |\n| --- | --- | --- |\n| W25Q64 | 5 | D柜 |","conversation":"e2e-paste"}')" '解析出'
check "粘贴导入确认前不落库" "$(api GET '/api/v1/items?keyword=W25Q64')" '"total":0'
check "粘贴导入确认" "$(api POST /api/v1/bot/command '{"text":"确认","conversation":"e2e-paste"}')" '导入完成'
check "粘贴导入生效" "$(api GET '/api/v1/items?keyword=W25Q64')" '"total":1'
check "位置可省略" "$(api POST /api/v1/bot/command '{"text":"入库 杜邦线 100 无位置","conversation":"e2e-ask"}')" '入库成功'
check "入库后追问封装" "$(api POST /api/v1/bot/command '{"text":"入库 AT24C02 20 @C柜","conversation":"e2e-ask2"}')" '要补充规格/封装吗'
check "追问回答被记录" "$(api POST /api/v1/bot/command '{"text":"SOP-8","conversation":"e2e-ask2"}')" '已记录规格：SOP-8'
check "跳过别名追问" "$(api POST /api/v1/bot/command '{"text":"跳过","conversation":"e2e-ask2"}')" '信息补全完成'
check "追问期间下指令不被吞" "$(api POST /api/v1/bot/command '{"text":"入库 LM317 20 @C柜","conversation":"e2e-ask3"}')" '要补充规格/封装吗'
check "下指令正常执行" "$(api POST /api/v1/bot/command '{"text":"库存","conversation":"e2e-ask3"}')" '库存总览'
check "100欧姆电阻 ≡ 100Ω电阻" "$(api POST /api/v1/bot/command '{"text":"入库 100欧姆电阻 100 @C库","conversation":"e2e-ohm"}')" '入库成功'
check "另一种写法能搜到" "$(search 100Ω电阻)" '100欧姆电阻'

echo "==> 9b. 采购清单比对 + 位置冲突处理"
check "采购比对（表格）" "$(api POST /api/v1/bot/command '{"text":"采购\n| 名称 | 数量 |\n| --- | --- |\n| STM32F103C8T6 | 20 |\n| ZZTEST-NOPE | 5 |","conversation":"e2e-procure"}')" '需要额外购买'
check "已拥有的给出位置数量" "$(api POST /api/v1/bot/command '{"text":"采购 STM32F103C8T6 20","conversation":"e2e-procure2"}')" '@ A柜-1层-盒3'
check "采购用量不足要算差额" "$(api POST /api/v1/bot/command '{"text":"采购 STM32F103C8T6 999","conversation":"e2e-procure3"}')" '还差'
check "已有物品不填位置无需追问" "$(api POST /api/v1/bot/command '{"text":"入库 STM32F103C8T6 5","conversation":"e2e-loc0"}')" '入库成功'
check "已有物品填新位置会询问" "$(api POST /api/v1/bot/command '{"text":"入库 STM32F103C8T6 5 @Z柜","conversation":"e2e-loc"}')" '① 合并'
check "询问期间不落库" "$(api GET '/api/v1/items?keyword=STM32F103C8T6')" 'A柜-1层-盒3'
check "选择合并" "$(api POST /api/v1/bot/command '{"text":"合并","conversation":"e2e-loc"}')" '已合并位置'
check "合并后位置已更新" "$(api GET '/api/v1/items?keyword=STM32F103C8T6')" 'Z柜'

# 整批清零放在最后，避免影响前面的统计断言
check "整批出库要确认" "$(api POST /api/v1/bot/command '{"text":"出库全部","conversation":"e2e-zero"}')" '回复「确认」执行'
check "确认后清零" "$(api POST /api/v1/bot/command '{"text":"确认","conversation":"e2e-zero"}')" '已执行'
check "清零后清单不再列条目" "$(api POST /api/v1/bot/command '{"text":"库存","conversation":"e2e-zero"}')" '当前没有在库物品'
check "零库存能看到" "$(api POST /api/v1/bot/command '{"text":"零库存","conversation":"e2e-zero"}')" '已清零的物品'
check "清理零库存要确认" "$(api POST /api/v1/bot/command '{"text":"清理零库存","conversation":"e2e-zero"}')" '将删除'
check "确认清理" "$(api POST /api/v1/bot/command '{"text":"确认","conversation":"e2e-zero"}')" '已删除'
check "清理后库里没记录" "$(api GET /api/v1/items)" '"total":0'

echo
echo "=========================================="
echo "端到端结果：$PASSED 项通过，$FAILED 项失败"
echo "=========================================="
if [ "$FAILED" -gt 0 ]; then
  echo "服务日志尾部："; tail -30 "$LOG"
  exit 1
fi
exit 0
