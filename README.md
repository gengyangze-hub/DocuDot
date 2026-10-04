# DocuDot · 仓储助手

**QQ 机器人仓储库存后端服务** —— Linux 上一条命令部署，FastAPI + SQLite，
零外部服务依赖（不需要 Redis / MySQL / Docker，一个 Python 进程 + 一个 `.db` 文件）。

定位是「用大白话管库存」：**分类不预设**（由 AI 按你的实际物品归纳，你说什么分类就建什么分类），
核心能力是**标签式模糊匹配**：`0.1uF` 能找到 `100nF`、`51R` 能找到 `51Ω`、
搜「电容」能找到 `C`、搜「插排」能找到「排母」。

```bash
git clone https://github.com/gengyangze-hub/DocuDot && cd DocuDot
bash scripts/deploy.sh          # 问你要三个凭证，然后自动装好并启动
```

---

## 一、需求对照

| 你的需求 | 实现情况 |
|---|---|
| 作为 QQ 机器人后台，接收仓储变化信息并储存 | ✅ QQ 官方机器人开放平台 **WebSocket** 适配层（`app/qq/`），消息 → 命令路由 → 落库；同时提供 `POST /api/v1/bot/command` 便于其它框架复用 |
| 存储字段：名称 / 数目 / 存储位置 | ✅ 另含**分类、别名标签、封装规格、单位、备注、入库/更新时间、操作人** |
| 支持 API Key 调用进行数据分析 | ✅ `X-API-Key` / `Bearer`，四档权限 `read / write / analyze / admin`，SHA-256 存哈希、支持吊销与过期 |
| 模糊匹配（参考 component-inventory） | ✅ 移植其**分层匹配 + 物理量等价 + 类型/介质/封装归约**，含防误匹配守卫（详见第五节） |
| 导入 Excel / TXT / Markdown / JSON | ✅ 四种格式直接导入（表头中英文别名自动识别）；兼容 **component-inventory 的导出结构**；**QQ 里粘贴清单或直接发文件也能导入** |
| 导入时 AI 自动归类 | ✅ 导入前让 AI 通读整批定分类：优先复用已有分类，**装不下就自动新建**（实测把混合清单归成 元器件/五金件/文具/游戏周边）；`AI_CLASSIFY_ON_IMPORT=false` 可关 |
| 导入时 AI 规范命名 | ✅ 与归类**合并成同一次 LLM 调用**（不额外增加等待）：`R 4k7` → `4.7kΩ`、`C 1uF 16V MLCC 0805` → 名称 `1uF 16V MLCC` + 规格 `0805`、`黄 led` → `发光二极管 黄色`。**封装不写进名称**（避免和规格重复），**原名自动保留为别名**，且有护栏防止 AI 把名字改丢信息；`AI_NORMALIZE_ON_IMPORT=false` 可关 |
| AI 预处理为可读 Markdown | ✅ `POST /api/v1/import/ai`（服务端调大模型）+ `GET /api/v1/import/template`（导出提示词） |
| 统计报表 / 自然语言问答 / 大模型分析 | ✅ 见第四、八节 |
| **AI 语义解析入库** | ✅ 规则解析不出来时自动交给大模型（`QQ_PARSE_MODE=auto/ai`），AI 还听得出「一盒」「一些」这类模糊说法，数量不明会**反问** |
| **AI 自动分类 / 归纳整理** | ✅ 分类**不封闭**，AI 可创建新分类（如「开发板」）；`整理` 命令让 AI 通读在库条目，提出补封装/补别名/归类建议 |
| **QQ 里直接发文件** | ✅ 聊天框直接发 `.xlsx` / `.csv` / `.txt` / `.md`，下载后走同一套导入器，确认后入库；图片/语音/视频会给出明确提示 |
| **采购清单比对** | ✅ `采购 <清单>`：拿采购清单对比库存，输出**已有物品的位置与数量**、还差多少、以及需要额外买什么 |
| **出库 / 盘点 也接清单** | ✅ `出库` / `盘点` 后面可以直接贴一整张清单（表格、或把机器人列出的清单复制回来），逐条执行、执行前确认；单条库存不足不影响其他条目 |
| **合并重复条目** | ✅ `整理` 会指出「这几组可能是同一种东西」并编号，回「合并 1」或「合并这些」即可真合并（数量相加、名称留成别名、规格不同会先警告） |
| **AI 模糊指令匹配** | ✅ 规则认不出的说法交给大模型判意图：「全部不要了」→ 整批出库、「手头还有多少东西」→ 总览。拿不准会明确回 `unknown` 不乱执行，改库存的意图还要求更高可信度（≥0.75）；`QQ_AI_INTENT=false` 可关 |
| **入库位置智能处理** | ✅ 入库已有物品**不必重复填位置**（直接累加、位置不变）；如果填了新位置，会问你是「合并」还是「分开」 |
| **不同封装自动分档** | ✅ 已有记录填了**不同封装**时按新品种单独建档（补全缺失封装则算补全，不新建） |
| **归类 / 删除** | ✅ `归类 <名称> <分类>`（也可说「杜邦线归入stm元器件」，分类不存在会自动新建）；`删除 <名称>` / `删除全部`（均需二次确认） |

**实测验收数据**（全部在 WSL Ubuntu 24.04 真实执行）：

| 检查 | 结果 |
|---|---|
| `scripts/selftest.py` 核心引擎断言 | **82 / 82 通过** |
| `pytest` 全量测试 | **530 passed** |
| `scripts/e2e.sh` 真实 HTTP 端到端 | **72 / 72 通过** |
| `scripts/check_qq.py` QQ 适配层离线自检 | **25 / 25 通过** |
| `scripts/check_commands.py` 指令面验收（51 条指令真跑） | **51 / 51 通过**（未配 LLM 时其中 5 条 AI 用例自动跳过，报 46/51 + 5 跳过） |
| `scripts/list_routes.py` 打印端点清单 | **39 个业务端点**（人工与 `docs/api.md` 核对） |
| 真实 DeepSeek 联调 | AI 意图匹配 / 库存描述解析 / 导入自动归类 / 规范命名 / `整理` / 判断是否追问 **全部跑通** |
| 真实 QQ 联调 | 机器人已上线，多轮对话（整理→合并、批量出库、归类、清理零库存、入库）**实测可用** |
| QQ 附件下载路径 | 用本地 HTTP 冒充 QQ CDN，**下载 → 解析 → 导入** 全链路跑通（真实 CDN URL 待你发一次文件确认） |

---

## 二、快速开始（Linux 云服务器 / VPS）

### 一条命令部署

```bash
# 服务器上先备好基础环境（多数云镜像自带 python3，缺 venv 才需要补）
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

git clone https://github.com/gengyangze-hub/DocuDot && cd DocuDot
bash scripts/deploy.sh
```

脚本一条龙走完：检查环境 → 建虚拟环境 → 装依赖 → **问你要凭证** → 写 `.env`（权限 600）
→ 自检 → **装成开机自启的 systemd 服务**并启动。

交互中会问你：

| 问什么 | 怎么答 |
|---|---|
| **API Key** | 直接回车 → 自动生成一个 32 位强随机 Key（推荐） |
| **QQ AppID** | QQ 开放平台 → 你的机器人 → 开发设置。**不接 QQ 就回车**，HTTP 接口照样能用 |
| **QQ AppSecret** | 同上，和 AppID 配套（两个要么都给、要么都不给） |
| *（可选）* DeepSeek API Key | `platform.deepseek.com`；不填就没有 AI 归类 / 分析 |
| **接口访问范围** | `1` 只本机（推荐）/ `2` 对外开放 |

### 关于「接口访问范围」——云服务器上请务必想清楚

**QQ 机器人是主动外连的**（服务端主动建立 WebSocket 连到 QQ 网关），
所以它**不需要任何入站端口**。默认绑定 `127.0.0.1` 时：

- ✅ 机器人在群里/私聊照常收发
- ✅ 公网扫不到你这个端口
- ⚠️ 只有服务器本机能调 HTTP 接口 —— 要远程调就开 SSH 隧道：
  `ssh -L 8000:127.0.0.1:8000 用户名@你的服务器IP`，然后本地访问 `http://127.0.0.1:8000/docs`

如果你确实要对外开放（选 `2`，绑 `0.0.0.0`），脚本会提醒你两件事：
**去云厂商控制台的「安全组」放行该端口**（否则外面照样连不上），并且只通过 API Key 调用。
更稳的做法是 Nginx 反代 + HTTPS，让 uvicorn 继续只监听 `127.0.0.1`。

### 部署完自检

```bash
KEY=<部署脚本给你的 API Key>
curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/api/v1/items
# 交互式文档：http://127.0.0.1:8000/docs
```

### 日常运维

```bash
systemctl status docupoint        # 看状态
systemctl restart docupoint       # 重启
journalctl -u docupoint -f        # 实时日志
cp ~/DocuDot/data/inventory.db ~/backup-$(date +%F).db   # 备份（全部数据就这一个文件）
```

### 常用开关

```bash
bash scripts/deploy.sh --host 0.0.0.0   # 直接指定绑定地址
bash scripts/deploy.sh --port 9000      # 换端口
bash scripts/deploy.sh --service        # 直接装服务，不再问
bash scripts/deploy.sh --no-service     # 不装服务，用 nohup 后台跑
bash scripts/deploy.sh --foreground     # 前台跑，Ctrl-C 退出（调试）
bash scripts/deploy.sh --force          # 覆盖已有 .env（默认会先备份）
bash scripts/deploy.sh --non-interactive   # 全部从环境变量读，适合自动化
bash scripts/deploy.sh --help           # 全部参数
```

### 手动部署（想自己控制每一步）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：BOOTSTRAP_API_KEY 可以留空 —— 首次启动会自动生成并打印在日志里
bash scripts/run.sh
```

### 灌一份示例数据

```bash
KEY=$BOOTSTRAP_API_KEY   # 或部署脚本给你的那个

# 从 Excel 导入（先生成示例文件）
python3 scripts/make_sample_xlsx.py
curl -s -X POST http://127.0.0.1:8000/api/v1/import/commit-file \
  -H "X-API-Key: $KEY" -F "file=@samples/sample_inventory.xlsx" -F "mode=merge"

# 或直接从 Markdown 导入
curl -s -X POST http://127.0.0.1:8000/api/v1/import/commit-file \
  -H "X-API-Key: $KEY" -F "file=@samples/sample_inventory.md" -F "mode=merge"

# 试试模糊匹配
curl -s -G http://127.0.0.1:8000/api/v1/search \
  -H "X-API-Key: $KEY" --data-urlencode "q=100nF"
```

---

## 三、目录结构

```
DocuDot/                       # 仓库根目录 = 运行目录，clone 下来就能跑
├── app/
│   ├── main.py              # FastAPI 装配入口、生命周期、错误处理
│   ├── config.py            # 全部配置（环境变量 / .env）
│   ├── db.py                # SQLite 连接、建表、迁移
│   ├── models.py            # Pydantic 契约（请求/响应）
│   ├── repository.py        # 所有 SQL 集中在这里
│   ├── errors.py            # 领域异常 → HTTP 状态码
│   ├── utils.py             # 时间、数值格式化
│   ├── core/                # ★ 纯逻辑，无 IO，最易测试
│   │   ├── normalize.py     #   文本/物理量归一化、类型与封装归约
│   │   ├── fuzzy.py         #   分层模糊匹配与打分
│   │   └── categories.py    #   分类体系（**不预设内置分类**，由 AI/用户创建）
│   ├── services/            # 业务编排
│   │   ├── inventory.py     #   检索、出入库、消歧、合并
│   │   ├── analysis.py      #   统计报表
│   │   ├── nlq.py           #   中文自然语言问答
│   │   ├── importer.py      #   Markdown/TXT/CSV/Excel/JSON 解析与落库
│   │   ├── ai.py            #   大模型：分析 / 意图 / 归类 / 规范命名 / 整理
│   │   ├── llm.py           #   OpenAI 兼容客户端
│   │   └── categorize.py    #   分类推断（转发 core.categories）
│   ├── api/                 # 路由层（39 个业务端点）
│   │   ├── deps.py          #   服务容器 + API Key 鉴权
│   │   ├── routes_inventory.py
│   │   ├── routes_analysis.py
│   │   ├── routes_import.py
│   │   └── routes_admin.py
│   ├── bot/                 # QQ 指令层（与 REST 共用同一套服务）
│   │   ├── commands.py      #   指令路由、多轮追问、批量确认
│   │   └── session.py       #   会话上下文（候选、待确认、追问队列）
│   └── qq/                  # QQ 官方机器人 WebSocket 适配层
│       ├── protocol.py      #   握手报文、事件解析、附件解析（纯函数）
│       ├── gateway.py       #   WebSocket 常驻循环、心跳、重连
│       ├── openapi.py       #   access_token 换票 + 消息下发
│       ├── bot.py           #   编排：白名单 → 前缀 → handler → 回复
│       └── types.py
├── scripts/
│   ├── deploy.sh            # ★ 一键部署（装依赖 + 问凭证 + 写 .env + 启动）
│   ├── run.sh               # 只启动服务（读已有 .env）
│   ├── dev.sh               # 开发循环：同步 → 编译 → 自检 →（可选）测试
│   ├── scan_secrets.sh      # 上传前扫一遍有没有夹带凭证
│   ├── selftest.py          # 82 项核心引擎断言（只需标准库）
│   ├── e2e.sh               # 72 项真实 HTTP 端到端验收
│   ├── check_qq.py          # QQ 适配层离线自检（25 项）
│   ├── check_commands.py    # 指令面验收：51 条指令逐条真跑
│   ├── make_sample_xlsx.py  # 生成 Excel 示例
│   ├── list_routes.py       # 打印全部端点
│   └── bench.py             # 性能基准（本地临时库计时）
├── samples/                 # 示例数据（Markdown / TXT / XLSX / 杂乱原文）
├── docs/                    # 接口、导入格式、QQ 接入、指令速查
├── tests/                   # pytest 测试套件（530 项）
├── requirements.txt
├── .env.example             # 配置模板（只有占位符，可以安全提交）
└── .gitignore               # 已挡住 .env / 数据库 / 日志 / .venv
```

> 运行期产物（`.env`、`data/`、`*.db`、`*.log`、`.venv/`）都在 `.gitignore` 里，
> 不会进仓库，也不会跟着 `git clone` 发给别人。

---

## 四、接口一览

完整清单见 [docs/api.md](docs/api.md)（**39 个业务端点** + 2 个无鉴权端点）。常用：

| 用途 | 接口 |
|---|---|
| 查库存（模糊匹配） | `GET /api/v1/search?q=100nF` |
| 批量把名字解析成唯一物品 | `POST /api/v1/search/match` |
| 入库 / 出库 / 盘点 | `POST /api/v1/stock/in`、`/out`、`/set`、`/change`（统一入口） |
| 流水 | `GET /api/v1/stock/movements` |
| 统计报表 | `GET /api/v1/analysis/overview`、`/categories`、`/locations`、`/low-stock` |
| 自然语言问答 | `POST /api/v1/analysis/nlq` |
| 大模型分析 | `POST /api/v1/analysis/ai`（需 `analyze` 权限 + LLM Key） |
| 导入 | `POST /api/v1/import/commit-file`（上传）、`/commit`（文本）、`/ai`（AI 规范化） |
| 导出 | `GET /api/v1/export/markdown`、`/export/xlsx` |
| 机器人命令 | `POST /api/v1/bot/command` |
| Key 管理 | `POST/GET /api/v1/admin/keys`、`DELETE /api/v1/admin/keys/{id}` |

鉴权：`X-API-Key: whk_xxx` 或 `Authorization: Bearer whk_xxx`。
权限：`read`（查询）→ `write`（增删改、出入库、导入）→ `analyze`（LLM 相关）→ `admin`（Key、审计、删除；隐式包含全部）。

---

## 五、模糊匹配怎么工作

实现移植自 [konamivrc6/component-inventory](https://github.com/konamivrc6/component-inventory)
（标签式电子元件库存 CLI，其 `--selftest` 有 848 项断言），
并按本项目的「名称 + 别名 + 规格 + 分类」模型做了适配。分层递进、首个命中即返回：

| 分数 | 命中条件 | 例子 |
|---|---|---|
| 1.0 | 整串归一化全等（名称/别名/规格） | `stm32f103c8t6` → `STM32F103C8T6` |
| 1.0 | **物理量等价且两侧维度都明确** | `0.1uF` ≡ `100nF` ≡ `104`(EIA) |
| 0.9 | 强类型 / 介质 / 封装归约相等 | `电容器`→`C`、`陶瓷电容`→`MLCC`、`SOP8`≡`SOIC-8` |
| 0.7 | 物理量等价但维度靠推断补出、弱描述词 | `白`→`LED` |
| 0.35~0.7 | 子串部分匹配，按长度比打折 | `STM32F103` → `STM32F103C8T6` |

**归一化规则**（`app/core/normalize.py`）：

- NFKC 全角转半角；`µ`(U+00B5) / `μ`(U+03BC) → `u`；`Ω`(U+2126) → `Ω`(U+03A9)
- **不做大小写折叠**：`M`(兆) 与 `m`(毫) 语义不同，永不互兜
- SI 前缀换算到基本单位：`p n u m k K M G T`
- 中缀小数：`4R7`=4.7Ω、`R47`=0.47Ω、`0R05`=0.05Ω、`1k2`=1200、`2M2`=2.2e6、`4u7`=4.7e-6
- 单位后缀（按长度降序匹配，`Hz` 先于 `h`）：`ohms/ohm/欧姆/Ω/欧/r`→电阻、
  `farad/法拉/法/f`→电容、`henry/亨利/亨/h`→电感、`hertz/赫兹/赫/hz`→频率、`volt/伏/v`→电压……
- 类型码 19 个（`R C L D Q U J SW XTAL LED OPTO FUSE POT RELAY BZ ANT BAT TP X`）+ 介质 5 类 + 弱证据描述词
- **相对参考仓库的扩展**：EIA 三位码 `104 → 100nF`（仅在维度推断为电容时启用，避免误伤型号）；
  型号前缀推断 `STM32…`→U、`1N4148`→D（所以搜「单片机」能找到 `STM32F103C8T6`）
- **复合名称里的物理量**：`100欧姆电阻`、`100Ω电阻`、`100kΩ电阻` 会先各自抽出「100 欧姆」再比较，
  因此两种写法是同一条记录；第一次用另一种写法命中时会**自动记成别名**，之后就是精确命中
- **类型码由物理量维度补出**：名称里只写了单位、没写「电阻 / R」时（`10kΩ 0805`），
  Ω 本身就说明它是电阻 → 自动补成类型码 R。所以 `查 电阻` / `查 R` / `查 欧姆` 三种说法
  都能命中同一条。电压、功率、封装码不会触发这条规则（`0.25W`、`16V`、`0805` 都不算类型）

**防误匹配守卫**（都有测试覆盖）：

- 维度闸门：查询是明确物理量却找不到等价标签 → **直接判不等，不降级到子串**（`100Ω` 不会命中电容）
- 数字开头必须落在词界：`100nF` 不会命中 `1100nF`
- 前导零封装不参与数值解析：`0805` 不会被当成 805Ω，`805` 也不会命中 `0805`
- 型号拦截：`1N4148` / `STM32F103C8T6` / `LQFP48` 不会被解析成物理量
- 单字符子串不出分；纯数字之间只认全等
- 分数接近时**不猜** —— 返回候选列表并提示补 `@位置`（HTTP 409 / QQ 回复候选）

> 偏差说明：参考仓库刻意不切逗号，本服务面向 QQ 自然语言输入，改为把中英文逗号也当分隔符。

---

## 六、导入的三种方式

详见 [docs/import-format.md](docs/import-format.md)。核心是**规范化 Markdown 表格**：

```markdown
## STM元器件

| 名称 | 数量 | 位置 | 规格 | 别名 | 备注 |
| --- | --- | --- | --- | --- | --- |
| STM32F103C8T6 | 25 | A柜-1层-盒3 | LQFP48 | F103C8, STM32F103 | 蓝药丸板用 |
```

1. **直接上传**（`.md` / `.txt` / `.csv` / `.xlsx`）→ 服务内置解析器，表头中英文别名自动识别，
   Excel 多工作表、缺表头、行内式条目、键值块都能吃
2. **AI 预处理**（你选的方案）：`GET /api/v1/import/template` 拿到提示词 → 丢给任意大模型 →
   贴回 `POST /api/v1/import/commit`
3. **服务端 AI 规范化**：`POST /api/v1/import/ai`，把杂乱原文直接丢进来，服务调大模型整理成
   Markdown 再导入（需要配 `LLM_API_KEY`）。`samples/sample_messy_input.txt` 就是给这条路径用的样例

导入模式：`merge`（快照覆盖数量，缺则新建）/ `add`（数量并入）/ `replace`（清空该分类重建）；
支持 `dry_run` 试运行。

---

## 七、QQ 机器人接入

详见 [docs/qq-setup.md](docs/qq-setup.md)。要点：

- 走 **QQ 机器人开放平台 WebSocket 网关**，**不需要公网回调地址**，也不依赖第三方框架（自己实现协议层）
- 在 `.env` 填 `QQ_APP_ID` / `QQ_APP_SECRET`（或 `QQ_BOT_TOKEN`），把 `QQ_BOT_ENABLED` 设为 `true`
- 支持单聊（C2C）、群聊（被 @）、频道；白名单用 `QQ_ALLOWED_USERS` / `QQ_ALLOWED_GROUPS` 限制
- 机器人和 HTTP 服务**同进程**运行：`uvicorn` 起来机器人就起来了，日志里能看到连接状态

**QQ 指令速查见 [docs/commands.md](docs/commands.md)** —— 22 个命令、82 个别名、
每种用法的真实示例、以及哪些操作需要二次确认。这里只列最常用的：

```
入库 NE555 30 @C柜-1层 #555 (DIP-8)    出库 NE555 5        盘点 NE555 42
库存 / 清单 / 查 STM32 / 分类 STM元器件 / 位置 A柜 / 零库存 / 库存不足
采购 <清单>      导入（或直接粘表格 / 发文件）      整理 → 合并 1
归类 杜邦线 耗材      删除 <名称>      出库全部 / 清理零库存
```

除此之外**直接说人话也行**：规则认不出的说法会交给大模型判断意图
（「全部不要了」→ 整批出库、「手头还有多少东西」→ 总览），拿不准会明确说不知道、
不会硬猜着执行。

几个已经打磨过的交互细节（都有测试覆盖）：

- **入库后追问细节**：缺封装就问封装、缺别名就问别名，回「跳过」即不再追问
- **清单只显示在库物品**：已出库（数量 0）的不占版面，用 `零库存` 单独查看
- **位置可以省略**：`入库 杜邦线 100` 直接可用；写 `无位置` / `跳过位置` 也认
- **整批操作必须二次确认**：`出库全部` / `清理零库存` 都会先报影响面，回「确认」才执行
- **危险说法有边界**：`出库 全部电容` 不会被误判成清空全库

---

## 八、AI 能力

规则解析负责**又快又省**的常见情况（`入库 NE555 30 @C柜`），
大模型负责**听得懂人话**的部分。两者按 `QQ_PARSE_MODE` 组合：

| 模式 | 行为 |
|---|---|
| `auto`（默认） | 规则优先；规则解析不出来（没名称/没数量）或明显是自然语言口吻时才调大模型 |
| `ai` | 入库/出库**一律**交给大模型分析（最懂人话，但每条消息都会调用 LLM） |
| `rules` | 只用关键词规则，完全不联网 |

`QQ_AI_CONFIDENCE`（默认 0.5）是自动执行的可信阈值，低于它就不落库。

### 1. AI 语义解析入库

```
用户：昨天进了一盒 100欧姆电阻 放在 C库，是 0805 的
机器人：新建并入库成功：100欧姆电阻（0805） 0 → 1，位置 C库
        （AI 理解：进货一盒，数量1，0805规格，C库）
```

**数量听不出来时会先反问，而不是瞎记**：

```
用户：入库 杜邦线 一些
机器人：我理解你是要入库「杜邦线」，但没听出数量。
        （AI 判断：明确入库，数量只说“一些”未给具体数字）
        补一句就行，例如：入库 杜邦线 10
```

### 2. AI 自动分类

分类**不是封闭枚举**：模型可以为一个明显不属于现有分类的物品新建分类
（以中文名作为分类码），新分类立刻能被 `分类 <名字>`、统计和过滤使用。

```
用户：进了一块 ESP32-C3 开发板 放 A柜
机器人：新建并入库成功：ESP32-C3 开发板 0 → 1，位置 A柜
        （已归入新分类：开发板）
```

### 3. AI 归类整理（`整理`）

让模型通读在库条目，提出**补封装 / 补别名 / 归分类**的建议，**确认后**才写库：

```
用户：整理
机器人：我看了一遍当前在库的条目，建议改 5 处：
        （补齐可识别的规格与别名）
          · NE555：spec （空） → DIP-8；别名 +555定时器
          · Steam 50元充值卡：spec （空） → 50元
          · 100欧姆电阻：别名 +100Ω、101
          · AMS1117-3.3：别名 +AMS1117-3.3V

        另外这几组可能是同一种东西（我不会自动合并，你确认后可以手动处理）：
          · 100欧姆电阻 ≈ 100Ω电阻

        回复「确认」应用这些修改，或「取消」放弃。
```

API 侧对应：`POST /api/v1/analysis/ai/parse-stock`（只解析）、
`POST /api/v1/analysis/tidy`（`apply=false` 只给建议）。

---

## 九、测试与验收

```bash
bash scripts/dev.sh                    # 编译 + 82 项核心自检
bash scripts/dev.sh -m pytest -q       # 再加 530 项 pytest
bash scripts/e2e.sh                    # 72 项真实 HTTP 端到端（起真 uvicorn）
python3 scripts/check_qq.py            # 25 项 QQ 适配层离线自检
python3 scripts/check_commands.py      # 51 条指令逐条真跑（需服务在跑）
python3 scripts/list_routes.py         # 打印全部端点
python3 scripts/bench.py 2000          # 性能基准（本地临时库，不碰线上数据）
```

`scripts/dev.sh` 会把仓库同步到一个本机运行目录（默认 `~/warehouse-bot`）再跑（源码目录由脚本自身位置推断，仓库换地方也不用改）。
如果代码本来就在 Linux 原生文件系统里、且已装好依赖，直接 `cd` 过去跑 pytest 也行。

> AI 相关的测试用**假 LLM 打桩**，不需要 Key、不烧 token；真实联调见上面的验收表。

---

## 十、已知限制

- **QQ 真实链路已联调但未长期运行**：换票、WebSocket 握手、心跳（41.2s）都已实测通过，
  真实消息往返也跑过，但自动重连、断线恢复这些边界情况没有长时间压测。
- **QQ 附件的真实 CDN URL 未验证**：`attachments` 解析、下载、大小限制、失败兜底都测过
  （用本地 HTTP 冒充 CDN 跑通了全链路），但 QQ 真实文件 URL 是否需要额外鉴权头，
  要等你真发一次文件才能确认。失败时会回「文件下载失败」并在日志里写明 HTTP 状态码。
- **图片 / 语音 / 视频不支持**：会回明确的「处理不了」提示，而不是装死。
- **旧版 `.xls` 不支持**：请另存为 `.xlsx` 或 CSV。
- **SQLite 单机、单进程**：适合个人/小团队量级（本项目场景）。
  注意检索缓存与写锁都是**进程内**的，`uvicorn --workers >1` 需要改成数据库层的乐观锁。
  要多人高并发写，需要换 PostgreSQL。
- **AI 规范化受上下文限制**：原文超过 12000 字符会截断，超大 Excel 建议先转成 Markdown 再导入。
- **导入/合并不是一个整体事务**：逐行落库，中途失败会留下「已写入一部分」的状态
  （批次记录在最后写，所以这种时候不会留下批次记录）。大批量导入建议先 `dry_run` 预览。
- **行内解析的两处已知误判**：`C#编程 1` 会把 `#` 当成别名分隔符（名称变 `C`）；
  `USB线 2.0` 会把结尾的版本号当数量。给名称加引号或改写成「USB线（2.0 版） 3」可绕开。
- **`整理` 只合并它敢确认的**：重复条目会**编号列出来**等你决定（回「合并 1」），
  不会自作主张合并 —— 规格不同的两组还会先警告。这是有意的保守。
- **AI 调用会消耗 token**：`QQ_PARSE_MODE=ai` 时每条库存消息都会调用一次大模型；
  `QQ_AI_INTENT` 只在规则认不出时才调用。日常用默认的 `auto` 最划算。
- **权限边界**：`POST /bot/command` 需要 `write`（它后面的命令路由器能改库、能删记录）。
  `analyze` 只用于只读的 AI 分析；`tidy` 带 `apply=true` 时会额外要求 `write`；
  `import/ai` 默认 `dry_run=true` 只预览；`mode=replace`（会先清空涉及分类）需要 `admin`。
