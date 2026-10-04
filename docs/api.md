# HTTP 接口文档

- 基地址：`http://<host>:<port>`
- 交互式文档：`/docs`（Swagger UI）、`/redoc`
- 鉴权：`X-API-Key: whk_xxx` 或 `Authorization: Bearer whk_xxx`

## 权限范围

| scope | 能做什么 |
|---|---|
| `read` | 查询物品、检索、统计、导出、NLQ、AI 解析（不落库） |
| `write` | 上面全部 + 新建/修改物品、别名、出入库、导入、**机器人命令**、AI 导入 |
| `analyze` | 上面之外 + `POST /analysis/ai`、`POST /analysis/tidy`（只看建议） |
| `admin` | 全部，另含 Key 管理、审计日志、删除物品、`mode=replace` 导入。**隐式包含其它所有 scope** |

> ⚠️ 两条容易踩的：
> * **`POST /bot/command` 要 `write`** —— 它后面的命令路由器能改库存、能删记录、也能清空全库
>   （`删除全部` → `确认`）。只读 Key 不能走这条路。
> * **`POST /analysis/tidy` 挂 `analyze`，但 `apply=true` 时会在处理器里再要求 `write`** ——
>   `analyze` 的语义是「只读 + 调大模型」，不该顺带写数据。
> * **`POST /import/ai` 默认 `dry_run=true` 只预览**；要真落库得显式传 `"dry_run": false`。
>   `mode="replace"`（先清空涉及分类的旧记录）额外需要 `admin`。

错误响应统一形如：

```json
{"error": "ambiguous", "message": "「AMS1117-3.3」可能指多个物品，请确认是哪一个",
 "detail": {"candidates": [...]}}
```

| HTTP | error | 含义 |
|---|---|---|
| 401 | `unauthorized` | 缺 Key / Key 无效 / 已吊销 / 已过期 |
| 403 | `forbidden` | Key 权限不足 |
| 404 | `not_found` | 物品不存在 |
| 409 | `ambiguous` | 模糊匹配命中多个候选，需消歧 |
| 409 | `location_conflict` | 同名物品在别的位置已存在，需选择「合并 / 分开」 |
| 422 | `validation_failed` | 参数不合法（含库存不足、文件超限） |
| 502 | `upstream_error` | 大模型等外部依赖出错 |

---

## 端点清单

> 由 `python3 scripts/list_routes.py` 生成，与代码保持一致。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 服务信息（无鉴权） |
| GET | `/health` | 健康检查（无鉴权） |
| GET | `/api/v1/meta` | 分类 / 位置 / 规模概览 |
| GET | `/api/v1/items` | 列出库存（过滤 + 分页） |
| POST | `/api/v1/items` | 新建库存条目 |
| GET | `/api/v1/items/{item_id}` | 查看单个条目 |
| PATCH | `/api/v1/items/{item_id}` | 修改条目 |
| DELETE | `/api/v1/items/{item_id}` | 删除条目（需 admin） |
| GET | `/api/v1/items/{item_id}/aliases` | 查看别名 |
| POST | `/api/v1/items/{item_id}/aliases` | 添加别名 |
| DELETE | `/api/v1/items/{item_id}/aliases/{alias}` | 删除别名 |
| GET | `/api/v1/search` | 模糊检索 |
| POST | `/api/v1/search/match` | 批量解析名称 → 唯一物品 |
| POST | `/api/v1/stock/change` | 统一库存变更入口 |
| POST | `/api/v1/stock/in` | 入库 |
| POST | `/api/v1/stock/out` | 出库 |
| POST | `/api/v1/stock/set` | 盘点（直接设定数量） |
| GET | `/api/v1/stock/movements` | 出入库流水 |
| GET | `/api/v1/analysis/overview` | 库存总览 |
| GET | `/api/v1/analysis/categories` | 按分类统计 |
| GET | `/api/v1/analysis/locations` | 按存储位置统计 |
| GET | `/api/v1/analysis/low-stock` | 低库存清单 |
| POST | `/api/v1/analysis/nlq` | 自然语言问答（规则优先） |
| POST | `/api/v1/analysis/ai` | 大模型分析（需 analyze + LLM Key） |
| POST | `/api/v1/analysis/ai/parse-stock` | 用大模型解析一句库存变更描述（只解析） |
| POST | `/api/v1/analysis/tidy` | AI 归类整理：先给建议，可选直接应用 |
| POST | `/api/v1/import/preview` | 上传文件预览解析结果（不落库；`use_ai=true` 时先让 AI 归类） |
| POST | `/api/v1/import/preview-text` | 文本预览（`use_ai=true` 同上） |
| POST | `/api/v1/import/commit` | 提交导入（文本；`use_ai=true` 同上） |
| POST | `/api/v1/import/commit-file` | 提交导入（上传文件；`use_ai=true` 同上） |
| POST | `/api/v1/import/ai` | AI 规范化导入 |
| GET | `/api/v1/import/template` | 规范化 Markdown 模板 + AI 提示词 |
| GET | `/api/v1/import/batches` | 导入批次历史 |
| GET | `/api/v1/export/markdown` | 导出规范化 Markdown |
| GET | `/api/v1/export/xlsx` | 导出 Excel |
| POST | `/api/v1/bot/command` | 机器人命令入口 |
| POST | `/api/v1/admin/keys` | 新建 API Key |
| GET | `/api/v1/admin/keys` | 列出 Key（不含明文） |
| DELETE | `/api/v1/admin/keys/{key_id}` | 吊销 Key |
| GET | `/api/v1/admin/audit` | 审计日志 |
| GET | `/api/v1/admin/status` | 服务运行状态 |

---

## 典型调用

### 新建条目

```bash
curl -X POST http://127.0.0.1:8000/api/v1/items \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{
    "name": "STM32F103C8T6",
    "category": "STM元器件",
    "quantity": 25,
    "location": "A柜-1层-盒3",
    "spec": "LQFP48",
    "aliases": ["F103C8", "STM32F103"],
    "operator": "geng"
  }'
```

`category` 是**自由文本分类名**，没有内置分类：

- 省略 / 空 → `未分类`（之后可由 AI 归类）
- 想建新分类就直接写名字（`"category": "耗材"`）
- 旧代码仍兼容：`stm_component` → `STM元器件`、`steam_card` → `Steam游戏卡`、`other` → `未分类`
- 现有的分类列表见 `GET /analysis/categories`（完全由数据决定）

### 模糊检索

```bash
curl -G http://127.0.0.1:8000/api/v1/search \
  -H "X-API-Key: $KEY" --data-urlencode "q=100nF"
```

```json
{
  "query": "100nF",
  "tokens": ["100nF"],
  "hits": [
    {"id": 2, "name": "0.1uF 50V MLCC", "score": 1.0,
     "quantity": 500, "location": "A柜-2层-盒1", "spec": "0805",
     "reasons": ["100nF: 100nF ≈ 0.1uF"]}
  ],
  "total": 1,
  "threshold": 0.55
}
```

查询词支持空格分隔（默认 AND，`any_mode=true` 切 OR）、
中文逗号分隔；位置类过滤请用 `/items?location=`。

### 入库 / 出库

```bash
# 按别名出库
curl -X POST http://127.0.0.1:8000/api/v1/stock/out \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "F103C8", "quantity": 5, "operator": "qq:10001"}'

# 入库并自动建档（找不到就建）
curl -X POST http://127.0.0.1:8000/api/v1/stock/change \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"action":"in","name":"1N4148","quantity":100,"location":"C柜-2层","auto_create":true}'
```

- `action`：`in` / `out` / `set`
- `item_id` 与 `name` 二选一；给 `name` 走模糊匹配
- 命中多个候选时返回 **409**，候选列表在 `detail.candidates`；
  加 `allow_ambiguous: true` 可强制取最高分，或带 `location` 精确化
- 出库超量返回 **422**（不会把库存扣成负数）

### 自然语言问答

```bash
curl -X POST http://127.0.0.1:8000/api/v1/analysis/nlq \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"question": "STM32 还有多少"}'
```

返回 `intent` 便于程序判断：

| intent | 触发示例 | 说明 |
|---|---|---|
| `total` | 总共多少种物品 | 总量统计 |
| `overview` | 库存总览 | 分类汇总 |
| `count` | STM32 还有多少 | 按关键词查数量 |
| `where` | F103C8 放在哪 | 按关键词查位置 |
| `location_list` | A柜-2层-盒1里有什么 | 按位置列清单 |
| `spec` / `alias` | 电容的规格 / 电容的别名 | 属性查询 |
| `category_stats` | 分类统计 | 分类分布 |
| `low_stock` | 库存不足 | 低库存清单（`low_stock_threshold` 可调） |
| `list` / `search` | 清单 / 任意关键词 | 列表与兜底检索 |

`use_llm: true` 时，规则答不出来会兜底问大模型（需要配 LLM）。

### 导入

```bash
# 上传文件（.md / .txt / .csv / .xlsx）
curl -X POST http://127.0.0.1:8000/api/v1/import/commit-file \
  -H "X-API-Key: $KEY" \
  -F "file=@samples/sample_inventory.xlsx" -F "mode=merge" -F "dry_run=false"

# 先预览不落库
curl -X POST http://127.0.0.1:8000/api/v1/import/preview \
  -H "X-API-Key: $KEY" -F "file=@samples/sample_components.txt"
```

`mode`：`merge`（按「名称+位置+规格」合并，已存在则更新数量）/ `add`（数量并入）/
`replace`（先清空出现的分类再重建）。

### 机器人命令

```bash
curl -X POST http://127.0.0.1:8000/api/v1/bot/command \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"入库 STM32F103C8T6 25 @A柜-1层 #F103C8","operator":"qq:10001","scene":"qq-group","conversation":"group-123"}'
```

`conversation` 是**会话键**：同一个键的多次调用共享上下文
（序号选择、`这些` 指代、待确认操作）。群聊传 `group_openid`，单聊传 `user_openid`；
不传则退化为按 `operator` 隔离。典型多轮交互：

```bash
# 1) 列出清单
curl ... -d '{"text":"库存","conversation":"group-123"}'
# 2) 回序号看详情
curl ... -d '{"text":"2","conversation":"group-123"}'
# 3) 整批出库 → 拿到确认提示（此时不会真的执行）
curl ... -d '{"text":"这些全部出库","conversation":"group-123"}'
# 4) 确认执行
curl ... -d '{"text":"确认","conversation":"group-123"}'
```

### 管理 Key

```bash
# 建一个只读 Key（明文只在这一次响应里返回）
curl -X POST http://127.0.0.1:8000/api/v1/admin/keys \
  -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"label":"只读脚本","scopes":["read"]}'
```
