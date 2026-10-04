"""QQ 消息命令路由。

同一套逻辑被两个入口复用：

* ``POST /api/v1/bot/command`` —— 脱离 QQ 也能直接测（也方便接别的机器人框架）
* ``app.qq.bot.QQBot`` —— 真实 QQ 消息进来后调用

设计原则
--------
1. **写操作要么明确成功，要么给用户可执行的下一步**。模糊匹配拿不准时不猜，
   把候选编号列出来。
2. **列出来的东西都能回溯**：清单带序号，用户回「2」就能选中第 2 项
   （见 :mod:`app.bot.session`）。
3. **危险操作必须二次确认**：「出库全部」一句话就把库存清零是不可接受的。
4. **汇总必须跟具体清单**：只回「8 种 / 1857 件」等于没说。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from ..config import Settings
from ..core.normalize import fold
from ..errors import Ambiguous, LocationConflict, NotFound, WarehouseError
from ..models import (
    BotCommandResponse,
    ImportPreviewOut,
    ImportRow,
    ItemUpdate,
    StockAction,
    StockChangeRequest,
    TidyPlan,
)
from ..services.analysis import AnalysisService
from ..services.categorize import (
    DEFAULT_CATEGORY,
    category_label,
    detect_category_hint,
    match_category,
    normalize_category_code,
)
from ..services.importer import parse_inline_entry, parse_input
from ..services.inventory import InventoryService
from ..services.nlq import ITEM_LIMIT, NLQService
from ..utils import fmt_qty, truncate
from .session import (
    PendingConfirmation,
    PendingIntent,
    PromptSlot,
    Session,
    SessionStore,
    build_candidates,
    build_candidates_from_hits,
    is_affirmative,
    is_negative,
    parse_selection,
)

logger = logging.getLogger(__name__)

HELP_TEXT = """\
【仓储助手】可用指令
· 库存 —— 在库总览 + 具体清单（已出库的不占版面）
· 查 <关键词> —— 模糊搜索，如「查 0.1uF」「查 STM32」「查 电容」
· 入库 <名称> <数量> [@位置] [#别名] [（规格）]
   例：入库 STM32F103C8T6 25 @A柜1层 #F103C8 (LQFP48)
   位置可以不写：入库 杜邦线 100 无位置
· 出库 <名称> <数量>
· 盘点 <名称> <数量> —— 直接把数量设为该值
· 位置 <位置名> —— 看某位置下有什么
· 别名 <名称> —— 看这个物品已有的别名
· 别名 <名称> +<别名> —— 给它加别名（多个用逗号分隔）
· 库存不足 —— 列出快用完的物品
· STM元器件 / 游戏卡 —— 看某个分类下的清单
· 出库 <名称> <数量> —— 出库，也可以直接接一整张清单（「出库」+ 粘贴多行）
· 盘点 <名称> <数量> —— 把数量直接改成这个值，同样支持整张清单
· 零库存 —— 看已出库（数量为 0）的条目
· 清理零库存 —— 删掉这些空记录（会先让你确认）
· 采购 <清单> —— 对照库存输出该买什么、已有什么在哪（可直接粘贴表格）
· 导入 —— 把整份清单直接粘给我，我解析后让你确认
   （Markdown 表格 / 「名称 × 数量 @位置」/「### 名称 + 数量: N」都行）
· 归类 <名称> <分类> —— 改分类，也可以说「杜邦线归入stm元器件」；分类不存在会自动新建
· 删除 <名称> —— 删掉某条记录（会先让你确认）
· 删除全部 —— 清空所有物品记录（**危险，会先让你确认**）
· 整理 —— 让 AI 检查一遍，提出归类/补规格/补别名的建议（会先让你确认）
· 合并 —— 整理之后用来合并重复条目：「合并 1」合并第 1 组、「合并这些」合并全部，
   也可以「合并 <要并掉的> 到 <保留的>」手动指定（数量相加、名称留成别名）
· 出库全部 / 这些全部出库 —— 整批清零（会先让你确认）
· 取消 —— 撤销当前待确认的操作（也可以说「算了」）

小技巧：
· 上面的清单都可以回序号继续操作，比如回「2」；嫌麻烦回「随便」也行
· 「这些」指上一次列出来的那批物品
· 入库已有的物品**不用重复填位置**；填了新位置会问「合并 / 分开」；**换了封装则是另一种物品**，会单独建档
· 入库后面跟一整张表格 = 批量导入（不会把表格压成一条名称）
· 入库后我会追问封装和别名，不想填就回「跳过」
· 也可以直接用大白话说：「我把杜邦线用掉两卷」「昨天进了一盒 0.1uF 电容放在 A 柜」
· 其它问题直接问，如「STM32 还有多少」「游戏卡放哪了」"""

#: 命令别名（把各种口语写法归一到规范命令）
COMMAND_ALIASES: dict[str, str] = {
    "帮助": "help", "help": "help", "菜单": "help", "指令": "help", "用法": "help",
    "库存": "overview", "总览": "overview", "概况": "overview", "库存总览": "overview", "统计": "overview",
    "清单": "list", "列表": "list", "所有物品": "list", "全部物品": "list",
    "查": "search", "查询": "search", "搜索": "search", "搜": "search", "找": "search",
    "入库": "in", "进货": "in", "收": "in",
    "出库": "out", "领用": "out", "取": "out", "用掉": "out",
    "盘点": "set", "设为": "set", "更正": "set",
    "位置": "location", "库位": "location", "柜子": "location",
    "别名": "alias", "标签": "alias",
    "分类": "category", "类别": "category",
    "零库存": "zero_list", "空库存": "zero_list", "已出库": "zero_list", "已清零": "zero_list",
    "清理零库存": "zero_clean", "删除零库存": "zero_clean", "清空零库存": "zero_clean",
    "清理已清零": "zero_clean", "删除已清零": "zero_clean",
    "库存不足": "low", "低库存": "low", "缺货": "low", "补货": "low",
    "整理": "tidy", "ai整理": "tidy", "AI整理": "tidy", "归纳": "tidy", "规整": "tidy",
    "导入": "import", "批量导入": "import", "导入清单": "import",
    "采购": "procure", "采购清单": "procure", "对单": "procure", "配单": "procure",
    "比对": "procure", "缺料": "procure", "缺什么": "procure", "要买什么": "procure",
    "归类": "categorize", "归入": "categorize", "归到": "categorize", "改分类": "categorize",
    "合并": "merge", "合并这些": "merge", "合并全部": "merge", "都合并": "merge",
    "删除": "delete", "删掉": "delete", "移除": "delete", "删": "delete",
    "删除全部": "delete_all", "清空全部": "delete_all", "清空数据": "delete_all",
    "确认": "confirm", "取消": "cancel",
}

#: 「X 归入 Y」这类自然说法
_CATEGORIZE_RE = re.compile(
    r"^(?P<name>.+?)\s*(?:归入|归到|归类到|归类为|归为|归成|算作|算到|放入|放到)\s*(?P<category>.+)$"
)


def parse_categorize_request(text: str) -> tuple[str, str] | None:
    """识别「杜邦线归入stm元器件」这种说法，返回 ``(名称, 分类说法)``。"""
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return None
    match = _CATEGORIZE_RE.match(raw)
    if not match:
        return None
    name = match.group("name").strip(" 的：:，,")
    category = match.group("category").strip(" 的：:，,")
    if not name or not category:
        return None
    return name, category

#: 位置冲突时用户可能回的话
_MERGE_WORDS = {"合并", "合", "合在一起", "并", "并到一条", "是", "对", "1", "①", "一起", "改位置"}
_SPLIT_WORDS = {"分开", "分", "分开存", "另建", "另存", "新建", "2", "②", "不动原来", "放两处"}

#: 采购清单比对时，候选要达到这个分数才算「已经有了」。
#: 0.9 是「整词命中/类型归约」那一档；多词名称（``排母 11P``）靠整词命中也能到 0.92。
_PROCURE_MATCH_SCORE = 0.9

#: 规格比较时忽略的单位后缀 —— 「2.54」和「2.54mm」是同一种封装。
_SPEC_UNIT_SUFFIX = re.compile(r"(?:mm|cm|毫米|厘米|mil|寸)$")


def _spec_key(text: str) -> str:
    """规格比较键：折叠写法差异，并忽略 ``mm``/``cm`` 这类单位后缀。

    实测踩到的坑：库里存的是 ``2.54``，采购清单里写 ``2.54mm``，
    严格的字符串比较会把它报成「库存里没有」。
    """
    return _SPEC_UNIT_SUFFIX.sub("", fold(text or ""))

#: 名称里含「字母+数字」混合的型号 token，多半是电子元件
_MODEL_TOKEN = re.compile(r"[A-Za-z]{1,}[0-9]|[0-9]{1,}[A-Za-z]{2,}")


def _rule_detail_decision(record: Any) -> tuple[bool, bool] | None:
    """用规则先判断要不要追问；判不了返回 ``None`` 交给大模型。

    规则能覆盖的情况就不必多花一次 LLM 往返（也更省用户等待时间）。
    """
    name = record.name or ""
    category = record.category

    # 名称里含「字母+数字」的型号 token（NE555、STM32F103C8T6）→ 大概率是电子元件，
    # 封装会直接影响选料与搜索，值得问。**不再按内置分类分支** —— 分类是 AI 定的，
    # 这里只做这一条与分类无关的规则，其余交给 AI 判断。
    if _MODEL_TOKEN.search(name):
        return (True, True)
    return None

#: 独立数字（前后都不是字母数字），用作「入库 XX 25」的兜底数量解析
_LOOSE_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_COMMAND_RE = re.compile(r"^(\S+)\s*(.*)$", re.DOTALL)

# ---- 整批操作识别 --------------------------------------------------------- #
_BULK_QUANTIFIER = re.compile(r"全部|所有|都|统统|整批|全都|清空|清零")
_BULK_VERB = re.compile(r"出库|领用|取出|取走|出掉|用掉|清空|清零|清除")
_REFER = re.compile(r"这些|那些|上面|刚才|它们|他们|此|这")
_BULK_FILLER = re.compile(
    r"请|帮我|帮忙|麻烦|把|将|这些|那些|上面|刚才|它们|他们|全部|所有|都|统统|整批|全都|"
    r"清空|清零|清除|出库|领用|取出|取走|出掉|用掉|物品|东西|库存|的|了|吧|呢|"
    r"[\s，,。！!？?、~～]"
)

#: 用户回这些词表示「这个字段不填了」
_SKIP_WORDS = {
    "跳过", "略过", "跳", "没有", "无", "不知道", "不确定", "不清楚", "不清楚了",
    "暂无", "没了", "不知道了", "不用了", "先跳过", "skip", "next", "-", "—",
}

#: 出现这些词说明用户是在下新指令，不是在回答追问。
#: 刻意不含单字「收」——「收纳盒」这种别名会被误判成指令。
_STOCK_VERB = re.compile(r"入库|出库|盘点|领用|进货")

#: 用户懒得分位置时可以说的话，回这些就取第一个候选
_PICK_FIRST_WORDS = {
    "随便", "都行", "都可以", "任意", "任意一个", "随意", "你决定", "看你",
    "第一个", "第一个吧", "就第一个", "第一项",
}

#: 显式表示「这个位置不填」（前缀词必须有，避免把「XX位置」这种名称截断）
_SKIP_LOCATION_TAIL = re.compile(
    r"[\s]*(?:跳过|忽略|省略|没有|无|未知|不指定|不填|不清楚|暂无)\s*"
    r"(?:位置信息|存放位置|存储位置|库位|位置)\s*$"
)

#: 出现这些说法，说明用户在用自然语言描述库存变更 —— 值得交给大模型
_AI_CUES = re.compile(
    r"大概|大约|差不多|一些|若干|记不清|不记得|好多|少量|放在|里头|里面|还剩|"
    r"加了|拿了|用掉|收到|进货|补齐|补货|多了|少了|总共|全部|一[盒箱包捆批]|两[盒箱包捆批]"
)

#: 批量文本导入：至少两行带结构化标记的内容
_IMPORT_LINE = re.compile(r"[|×✕@#（(]|\d+\s*(?:个|件|张|片|只|根|套|台|块|包|枚)")

#: 出现这些动词，说明这句话可能在描述一次库存变更（而不是查询）
_STOCK_INTENT = re.compile(
    r"入库|出库|领用|进货|进了|入了|到货|收了|收到|用了|用掉|拿了|拿走|加了|补了|补货|盘点|更正"
)


def _render_import_row(row: Any) -> str:
    original = getattr(row, "original_name", "")
    text = f"{original} → {row.name}" if original else row.name
    if row.spec:
        text += f"（{row.spec}）"
    text += f" × {fmt_qty(row.quantity)}"
    if row.location:
        text += f" @ {row.location}"
    return text


#: 「合并 <要并掉的> 到 <保留的>」
_MERGE_RE = re.compile(r"^(?:把\s*)?(?P<src>.+?)\s*(?:合并)?\s*(?:到|进|->|→|并入)\s*(?P<dst>.+)$")
#: 「把 A 合并到 B」这种自然说法（不带「合并」命令词时也能识别）
_MERGE_NATURAL_RE = re.compile(r"^(?:把\s*)?(?P<src>.+?)\s*合并(?:到|进|入)\s*(?P<dst>.+)$")


def parse_merge_request(text: str) -> tuple[str, str] | None:
    """识别「把杜邦线合并到跳线」，返回 ``(要并掉的, 保留的)``。"""
    raw = (text or "").strip()
    if not raw or len(raw) > 60:
        return None
    match = _MERGE_NATURAL_RE.match(raw)
    if not match:
        return None
    src = match.group("src").strip(" 的，,")
    dst = match.group("dst").strip(" 的，,")
    if not src or not dst or src == dst:
        return None
    return src, dst


#: AI 判出的意图 → 内部命令（不需要额外参数的那些直接分发）
_AI_INTENT_COMMANDS: dict[str, str] = {
    "list": "list",
    "inventory": "list",
    "overview": "overview",
    "stats": "overview",
    "low": "low",
    "zero_list": "zero_list",
    "zero_clean": "zero_clean",
    "tidy": "tidy",
    "help": "help",
    "import": "import",
    "delete_all": "delete_all",
}

#: 这些意图的「可信度下限加成」——按**影响面**分级：
#: 会改库存、或影响面大的（全部库存 / 删记录）要求更高分；普通查询不设加成。
_AI_INTENT_MIN_CONFIDENCE: dict[str, float] = {
    "out_all": 0.70,       # 影响全部库存
    "delete_all": 0.70,    # 删掉所有记录
    "delete_last": 0.70,   # 删掉上一批记录（不可恢复）
    "stock_in": 0.75,      # 立刻改库存
    "stock_out": 0.75,
    "stock_set": 0.75,
}

#: 会**立刻改库存**的意图，需要更高的可信度才执行
_AI_INTENT_STOCK: dict[str, str] = {
    "stock_in": "in",
    "stock_out": "out",
    "stock_set": "set",
}

#: 这些意图「一条都没找到」说明规则没理解用户，值得让 AI 再判一次。
#: 反过来，``low_stock`` / ``list`` 的空结果本身就是答案，不该再去问 AI。
_AI_INTENT_FALLBACK_INTENTS = frozenset({"unknown", "search", "count", "where", "spec", "alias"})


def _category_summary(rows: Any) -> str:
    """把归类结果汇总成「STM元器件 12、耗材 3」这样的一行。"""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.category] = counts.get(row.category, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return "、".join(f"{category_label(code)} {count}" for code, count in ordered)


def _describe_tidy(change: Any) -> str:
    parts: list[str] = []
    for key, value in change.after.items():
        before = change.before.get(key)
        if key == "aliases":
            added = [a for a in value if a not in (before or [])]
            parts.append(f"别名 +{'、'.join(added)}")
        elif key == "category":
            parts.append(f"分类 {category_label(before)} → {category_label(value)}")
        else:
            parts.append(f"{key} {before or '（空）'} → {value}")
    reason = f"（{change.reason}）" if change.reason else ""
    return f"{change.name}：" + "；".join(parts) + reason


#: 位置特征词。用于容忍「入库 100欧姆电阻 100 C库」这种**没写 @** 的写法 ——
#: 用户不会记得要加 @，但「C库 / A柜-1层-盒3 / 3号货架」这种词一看就是位置。
_LOCATION_HINT = re.compile(r"柜|层|盒|货架|架|箱|抽屉|库房|库位|仓位|库|区")


def split_trailing_location(name: str) -> tuple[str, str] | None:
    """把「名称 位置」从最后一个空格处拆开。

    仅当尾部 token 含位置特征词时才拆，避免误伤「八方旅人 随身包」这类名称。
    """
    parts = name.rsplit(" ", 1)
    if len(parts) != 2:
        return None
    head, tail = parts[0].strip(), parts[1].strip()
    if not head or not tail or len(tail) > 24:
        return None
    if not _LOCATION_HINT.search(tail):
        return None
    return head, tail


def parse_bulk_request(text: str) -> tuple[str, str | None] | None:
    """识别「整批出库」类说法。

    返回 ``(scope, category)``：

    * ``("last", None)`` —— 指代上一批（「将这些物品全部出库」）
    * ``("all", None)`` —— 全部库存（「出库全部」「清空库存」）
    * ``("category", "STM元器件")`` —— 某个分类（「STM元器件 全部出库」）

    不是整批请求返回 ``None``。必须同时出现「量词」和「出库动词」，
    并且把量词/动词/指代词都剔除后不剩别的东西 ——
    这样「出库 全部电容」不会被误判成清空全库。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 25:
        return None
    if not (_BULK_QUANTIFIER.search(raw) and _BULK_VERB.search(raw)):
        return None
    residue = _BULK_FILLER.sub("", raw).strip()
    if not residue:
        return ("last" if _REFER.search(raw) else "all"), None
    category = match_category(residue)
    if category:
        return "category", category
    return None


#: 「清除 / 用掉 / 出库」→ 整批**清零**（记录保留，可审计）
_CLEAR_OUT_VERB = (
    r"清除|清掉|清空|清零|用掉|用完|用光|耗掉|出库|领用|取出|取走|出掉|不要了"
)
#: 「删除 / 删掉」→ 把记录**真的删掉**
_CLEAR_DELETE_VERB = r"删除|删掉|移除|去掉|拿掉|删"
#: 含糊动词（「把这些处理掉」）—— 没说清是清零还是删除，得反问
_CLEAR_VAGUE_VERB = r"处理|处置|收拾|弄掉|弄|搞掉|搞|整整|清理"
_CLEAR_OUT_RE = re.compile(_CLEAR_OUT_VERB)
_CLEAR_DELETE_RE = re.compile(_CLEAR_DELETE_VERB)
_CLEAR_VAGUE_RE = re.compile(_CLEAR_VAGUE_VERB)
_CLEAR_VERB = re.compile(f"{_CLEAR_OUT_VERB}|{_CLEAR_DELETE_VERB}|{_CLEAR_VAGUE_VERB}")

#: 指代「上一批」的说法。除了「这些」还认「采购清单里的物品」这类 ——
#: 用户刚跑完 `采购`，就是这么说话的。
_CLEAR_REFER = re.compile(
    r"这些|那些|这批|那批|上一批|刚才|上面|它们|他们|此|"
    r"采购清单|清单里面|清单里|清单中|那份清单|这份清单|列表里面|列表里"
)

#: 口语填充词 —— 全部剔除后不剩别的，才算「处置上一批」。
#: 注意**多字词必须排在单字词前面**（``里面`` 要在 ``里`` 之前），否则会剩个「面」。
_CLEAR_FILLER = re.compile(
    r"采购清单|清单里面|清单里|清单中|那份清单|这份清单|列表里面|列表里|"
    r"麻烦|库存|仓库|货架|里面|全部|所有|统统|整批|一起|一并|这些|那些|这批|那批|上一批|刚才|上面|"
    r"它们|他们|东西|物品|条目|记录|列表|清单|顺便|一下|需要|现在|好的|帮我|谢谢|可以|"
    + _CLEAR_VERB.pattern
    + r"|好|嗯|那|行|从|把|将|给|帮|请|里|中|上|的|都|全|我|要|想|就|也|还|了|吧|啊|呢|嘛|此|掉|"
    r"[\s，,。！!？?、~～]"
)


def parse_clear_these(text: str) -> str | None:
    """识别「把采购清单里的物品删掉」这类**指代上一批**的请求。

    返回：

    * ``"out"`` —— 清零（数量归零，记录保留）
    * ``"delete"`` —— 真的删掉记录
    * ``"ask"`` —— 只说了「处理掉」这种含糊动词，**要反问用户**是哪种
    * ``None`` —— 不是这类请求

    比 :func:`parse_bulk_request` 宽松：允许「好的，现在从库存里面」这类口语填充词。
    但**必须同时**出现指代词与动词，且把它们全部剔除后不剩别的东西 ——
    所以「出库 STM32 25」不会被误判成整批操作。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 50:
        return None
    if not (_CLEAR_REFER.search(raw) and _CLEAR_VERB.search(raw)):
        return None
    if _CLEAR_FILLER.sub("", raw).strip():
        return None
    if _CLEAR_DELETE_RE.search(raw):
        return "delete"
    if _CLEAR_OUT_RE.search(raw):
        return "out"
    return "ask" if _CLEAR_VAGUE_RE.search(raw) else None


#: 清单行首的序号 / 项目符号（用户经常直接把机器人列出的清单复制回来）
_BATCH_LINE_PREFIX = re.compile(r"^\s*(?:[-*+•·]|\d+\s*[.)、）])\s*")


def _parse_stock_list(text: str) -> list[tuple[str, float, str, str]]:
    """解析「批量出库 / 批量盘点」的清单，返回 ``[(名称, 数量, 规格, 位置), ...]``。

    特意兼容**机器人自己渲染出来的行**——用户最常见的操作就是把上一份清单
    复制回来加个动词，形如 ``R 51Ω 0.25W 直插（直插） × 200``。
    """
    rows: list[tuple[str, float, str, str]] = []
    raw = (text or "").strip()
    if not raw:
        return rows

    # 表格 / Tab 分隔 → 交给文件解析器（表头自动识别，名称/数量/规格/位置都能拿到）
    if "|" in raw or "\t" in raw:
        try:
            preview = parse_input("stock-list.md", text=raw)
        except WarehouseError:
            preview = None
        if preview and preview.rows:
            return [
                (
                    row.name,
                    float(row.quantity or 0),
                    (row.spec or "").strip(),
                    (row.location or "").strip(),
                )
                for row in preview.rows
                if row.name
            ]

    for line in raw.splitlines():
        cleaned = _BATCH_LINE_PREFIX.sub("", line).strip()
        if not cleaned:
            continue
        entry = parse_inline_entry(cleaned)
        if entry is None or not entry.name:
            continue
        rows.append(
            (
                entry.name,
                float(entry.quantity or 0),
                (entry.spec or "").strip(),
                (entry.location or "").strip(),
            )
        )
    return rows


def looks_like_inventory_text(text: str) -> bool:
    """判断一段消息是不是「一整批库存清单」（用于 QQ 里直接粘贴表格）。"""
    raw = (text or "").strip()
    if "\n" not in raw:
        return False
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    if any(line.count("|") >= 2 for line in lines):   # markdown 表格
        return True
    return sum(1 for line in lines if _IMPORT_LINE.search(line)) >= 2


def _parse_procure_list(text: str) -> list[tuple[str, float, str, str]]:
    """解析采购清单，返回 ``[(名称, 需要数量, 规格, 位置), ...]``。

    支持 Markdown 表格 / Tab 分隔（走文件解析器），也支持逐行或逗号分隔的
    ``名称 数量``。**规格一定要带上** —— 采购比对必须区分封装，
    否则「要 0805 的 100nF」会把 0603 的库存也算进去。
    """
    raw = (text or "").strip()
    if not raw:
        return []

    if "|" in raw or "\t" in raw:
        try:
            preview = parse_input("procure.md", text=raw)
        except WarehouseError:
            preview = None
        if preview and preview.rows:
            return [
                (
                    row.name,
                    float(row.quantity or 0),
                    (row.spec or "").strip(),
                    (row.location or "").strip(),
                )
                for row in preview.rows
                if row.name
            ]

    segments = re.split(r"[\n;；]+", raw)
    if len(segments) == 1:
        segments = re.split(r"[,，、]+", raw)

    items: list[tuple[str, float, str, str]] = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        row = parse_inline_entry(segment)
        name = row.name if row else segment
        quantity = float(row.quantity) if row else 0.0
        spec = (row.spec or "").strip() if row else ""
        location = (row.location or "").strip() if row else ""
        if quantity <= 0:
            # 从**已清洗过的名称**上再剥一次数量，别从原始串重建（会把规格带回来）
            source = name if row is not None else segment
            matches = list(_LOOSE_NUMBER.finditer(source))
            if matches:
                last = matches[-1]
                quantity = float(last.group(1))
                name = f"{source[: last.start()]} {source[last.end():]}"
        name = re.sub(r"\s+", " ", name).strip(" @#")
        if name:
            items.append((name, quantity, spec, location))
    return items


class CommandRouter:
    def __init__(
        self,
        inventory: InventoryService,
        analysis: AnalysisService,
        nlq: NLQService,
        settings: Settings,
        sessions: SessionStore | None = None,
        ai: Any | None = None,
        importer: Any | None = None,
    ) -> None:
        self.inventory = inventory
        self.analysis = analysis
        self.nlq = nlq
        self.settings = settings
        self.sessions = sessions or SessionStore()
        self.ai = ai
        self.importer = importer

    # ------------------------------------------------------------------ 入口
    async def handle(
        self,
        text: str,
        *,
        operator: str = "",
        scene: str = "api",
        conversation: str | None = None,
    ) -> BotCommandResponse:
        raw = (text or "").strip()
        session = self.sessions.get(self._key(scene, conversation or operator))
        if not raw:
            return self._reply(HELP_TEXT, "help")

        try:
            return await self._route(raw, operator, scene, session)
        except Ambiguous as exc:
            return self._ambiguous_reply(exc, "resolve", session)
        except (NotFound, WarehouseError) as exc:
            return self._reply(exc.message, "error")
        except Exception:  # noqa: BLE001
            logger.exception("处理指令失败：%s", raw)
            return self._reply("处理出错了，请稍后再试或联系管理员。", "error", handled=False)

    async def _route(self, raw: str, operator: str, scene: str, session: Session) -> BotCommandResponse:
        # ---- 1. 有待确认的操作 ----
        if session.pending:
            if session.pending.action == "location_choice":
                return await self._resolve_location_choice(raw, session, operator, scene)
            if is_affirmative(raw):
                return await self._execute_pending(session, operator, scene)
            if is_negative(raw):
                session.clear_pending()
                return self._reply("已取消。", "cancel")

            # 「把这些处理掉」的追问：回「出库」或「删除」就执行
            if session.pending.action == "dispose":
                if _CLEAR_DELETE_RE.search(raw):
                    session.clear_pending()
                    return self._request_bulk_delete(session, operator)
                if _CLEAR_OUT_RE.search(raw) or raw.strip() in {"清零", "清空"}:
                    session.clear_pending()
                    return self._request_bulk_out("last", None, operator, scene, session)
                if is_negative(raw):
                    session.clear_pending()
                    return self._reply("已取消。", "cancel")
                return self._reply(
                    f"「这些」指的是{session.pending.description}。\n"
                    "回复「出库」清零，或「删除」删掉记录；回「取消」放弃。",
                    "dispose",
                )

            # 「整理」的结果里明确告诉用户可以回「合并 1」—— 这时不能被待确认卡住
            if session.pending.action == "tidy":
                command_match = _COMMAND_RE.match(raw)
                if command_match is not None and COMMAND_ALIASES.get(command_match.group(1)) == "merge":
                    session.clear_pending()
                    return self._merge(
                        command_match.group(2).strip(), session, operator, scene
                    )
                natural_merge = parse_merge_request(raw)
                if natural_merge:
                    session.clear_pending()
                    return self._merge(
                        f"{natural_merge[0]} 到 {natural_merge[1]}", session, operator, scene
                    )
            return self._reply(
                "还有一个待确认的操作：\n"
                f"  {session.pending.description}\n"
                "回复「确认」执行，或「取消」放弃。",
                "confirm",
            )

        # ---- 2. 正在等用户补全字段（入库后追问封装/别名）----
        if session.prompts:
            outcome = self._handle_prompt_answer(raw, session, operator)
            if outcome is not None:
                return outcome
            # 返回 None = 用户其实是在下新指令，放弃追问、落回正常流程

        # ---- 3. 回序号选择（也接受「随便」「第一个」）----
        numbers = parse_selection(raw)
        if not numbers and session.candidates and raw.strip() in _PICK_FIRST_WORDS:
            numbers = [1]
        if numbers and session.candidates:
            return await self._select(session, numbers, operator, scene)

        # ---- 4. 整批出库（自然语言，必须排在命令解析之前）----
        bulk = parse_bulk_request(raw)
        if bulk:
            return self._request_bulk_out(bulk[0], bulk[1], operator, scene, session)

        # 「从库存里面清除这些东西，我要用掉」—— 指代上一批
        clear_these = parse_clear_these(raw)
        if clear_these == "out":
            return self._request_bulk_out("last", None, operator, scene, session)
        if clear_these == "delete":
            return self._request_bulk_delete(session, operator)
        if clear_these == "ask":
            return self._ask_dispose(session)

        # ---- 4b. 「X 归入 Y」这类归类说法 ----
        categorize = parse_categorize_request(raw)
        if categorize:
            return self._categorize(categorize[0], categorize[1], operator, session)

        # ---- 4c. 「把 A 合并到 B」这类说法 ----
        merge_request = parse_merge_request(raw)
        if merge_request:
            return self._merge(f"{merge_request[0]} 到 {merge_request[1]}", session, operator, scene)

        # ---- 5. 命令优先于「粘贴导入」（否则「采购 + 表格」会被导入截走）----
        match = _COMMAND_RE.match(raw)
        head, rest = match.group(1), match.group(2).strip()
        command = COMMAND_ALIASES.get(head) or COMMAND_ALIASES.get(head.casefold())

        # ---- 6. 直接粘贴的一整批清单 → 走文件解析器导入 ----
        if command is None and self.importer is not None and looks_like_inventory_text(raw):
            response = await self._request_text_import(raw, session, operator, scene)
            if response is not None:
                return response

        if command is None:
            return await self._nlq(raw, session, operator, scene)
        return await self._dispatch(command, rest, raw, operator, scene, session)

    # ------------------------------------------------------------------ 分发
    async def _dispatch(
        self, command: str, rest: str, raw: str, operator: str, scene: str, session: Session
    ) -> BotCommandResponse:
        if command == "help":
            return self._reply(HELP_TEXT, command)
        if command == "overview":
            return self._overview(session)
        if command == "list":
            return self._list(session)
        if command == "category":
            if not rest:
                return self._reply("用法：分类 <分类名>，例如「分类 STM元器件」", command)
            return self._category(rest, session)
        if command == "procure":
            return self._procure(rest, session)
        if command == "categorize":
            return self._categorize_command(rest, operator, session)
        if command == "delete":
            return self._delete(rest, session, operator, scene)
        if command == "merge":
            return self._merge(rest, session, operator, scene)
        if command == "delete_all":
            return self._delete_all(session)
        if command == "search":
            if not rest:
                return self._reply("用法：查 <关键词>", command)
            return self._search(rest, session)
        if command in {"in", "out", "set"}:
            return await self._stock(command, rest, raw, operator, scene, session)
        if command == "location":
            if not rest:
                return self._reply("用法：位置 <位置名>", command)
            return self._location(rest, session)
        if command == "alias":
            return self._alias(rest, operator, session)
        if command == "low":
            return self._low(session)
        if command == "zero_list":
            return self._zero_list(session)
        if command == "zero_clean":
            return self._request_zero_clean(session)
        if command == "tidy":
            return await self._tidy(session, operator)
        if command == "import":
            # 「导入 <表格>」也可以直接把内容跟在后面
            if rest and looks_like_inventory_text(rest):
                response = await self._request_text_import(rest, session, operator, scene)
                if response is not None:
                    return response
            return self._reply(
                "把整份清单直接发给我就行，我会自动解析成条目并让你确认。\n"
                "支持这几种写法：\n"
                "· Markdown 表格（| 名称 | 数量 | 位置 | 规格 | 别名 |）\n"
                "· 行内式：STM32F103C8T6 × 25 @A柜-1层 #F103C8 (LQFP48)\n"
                "· 键值块：### 名称 + 若干「数量: 25」「位置: A柜」\n"
                "也可以走 HTTP 接口上传 Excel/CSV/TXT 文件。",
                "import",
            )
        if command == "confirm":
            return self._reply("现在没有待确认的操作。", command)
        if command == "cancel":
            return self._reply("现在没有待确认的操作。", command)
        return self._reply(HELP_TEXT, command)

    # ------------------------------------------------------------------ AI 解析
    def _llm_ready(self) -> bool:
        return bool(self.ai is not None and getattr(self.ai, "llm", None) and self.ai.llm.ready)

    def _should_use_ai(self, text: str, *, name: str, quantity: float, command: str) -> bool:
        if not self._llm_ready():
            return False
        mode = (self.settings.qq_parse_mode or "auto").lower()
        if mode == "rules":
            return False
        if mode == "ai":
            return True
        # auto：规则解析用不动，或明显是自然语言口吻时才问大模型
        if not name:
            return True
        if command in {"in", "out"} and quantity <= 0:
            return True
        return bool(_AI_CUES.search(text)) or len(text) >= 18

    async def _try_ai_intent(
        self, raw: str, session: Session, operator: str, scene: str
    ) -> BotCommandResponse | None:
        """规则都没认出用户想干什么时，让大模型做模糊指令匹配。

        这是最后一道兜底：只有 NLQ 也说不清（``unknown`` 或搜索无结果）时才会走到这里。
        """
        if not self.settings.qq_ai_intent or not self._llm_ready():
            return None
        assert self.ai is not None  # noqa: S101 - _llm_ready 已判断
        try:
            plan = await self.ai.classify_intent(raw)
        except Exception:  # noqa: BLE001 - 识别失败不能挡住主流程
            logger.warning("AI 意图识别失败：%s", raw, exc_info=True)
            return None

        threshold = float(self.settings.qq_ai_confidence or 0.5)
        floor = max(threshold, _AI_INTENT_MIN_CONFIDENCE.get(plan.command, 0.0))
        if plan.command in {"", "unknown"} or plan.confidence < floor:
            logger.info(
                "AI 意图未采用（command=%s confidence=%.2f < %.2f）：%s",
                plan.command,
                plan.confidence,
                floor,
                raw,
            )
            return None
        logger.info(
            "AI 意图：%r → %s（%.2f，%s）", raw, plan.command, plan.confidence, plan.reason
        )

        # 会改库存的意图必须给出名称（数量说不清时交给 stock 的 AI 解析去追问）
        if plan.command in _AI_INTENT_STOCK:
            verb = _AI_INTENT_STOCK[plan.command]
            if not plan.name:
                return None
            if verb in {"in", "out"} and plan.quantity <= 0:
                return await self._stock(verb, plan.name, raw, operator, scene, session)
            rest = plan.name
            if plan.quantity > 0:
                rest += f" {fmt_qty(plan.quantity)}"
            if plan.location:
                rest += f" @{plan.location}"
            return await self._stock(verb, rest, raw, operator, scene, session)

        if plan.command == "out_all":
            return self._request_bulk_out("all", None, operator, scene, session)

        # 指代上一批（刚列出的清单 / 刚对比过的采购清单）
        if plan.command in {"out_last", "delete_last"}:
            if not session.last_item_ids:
                return None      # 没有指代对象就别动
            if plan.command == "out_last":
                return self._request_bulk_out("last", None, operator, scene, session)
            return self._request_bulk_delete(session, operator)

        if plan.command in {"search", "category", "location"}:
            if not plan.name:
                return None
            if plan.command == "search":
                return self._search(plan.name, session)
            if plan.command == "category":
                return self._category(plan.name, session)
            return self._location(plan.name, session)

        command = _AI_INTENT_COMMANDS.get(plan.command)
        if command is None:
            return None
        logger.info("AI 指令匹配：%r → %s", raw, command)
        return await self._dispatch(command, "", raw, operator, scene, session)

    async def _try_ai_stock(
        self, text: str, operator: str, scene: str, session: Session
    ) -> BotCommandResponse | None:
        """让大模型解析这段文字；解析不出可信结果就返回 None，由调用方回退。"""
        assert self.ai is not None  # noqa: S101 - 调用点已判断
        try:
            parsed = await self.ai.parse_stock(text)
        except Exception:  # noqa: BLE001 - AI 不可用不能影响主流程
            logger.exception("AI 解析库存失败：%s", text)
            return None

        threshold = float(self.settings.qq_ai_confidence or 0.5)
        if parsed.action == "unknown" or not parsed.name or parsed.confidence < threshold:
            logger.info(
                "AI 解析未采用（action=%s confidence=%.2f）：%s", parsed.action, parsed.confidence, text
            )
            return None

        if parsed.action in {"in", "out"} and parsed.quantity <= 0:
            verb = "入库" if parsed.action == "in" else "出库"
            return self._reply(
                f"我理解你是要{verb}「{parsed.name}」，但没听出数量。\n"
                f"（AI 判断：{parsed.reason or '缺少数量'}）\n"
                f"补一句就行，例如：{verb} {parsed.name} 10",
                parsed.action,
            )
        return await self._apply_ai_parse(parsed, operator, scene, session, text)

    async def _apply_ai_parse(
        self, parsed, operator: str, scene: str, session: Session, raw: str
    ) -> BotCommandResponse:
        action = {
            "in": StockAction.IN,
            "out": StockAction.OUT,
            "set": StockAction.SET,
        }[parsed.action]
        request = StockChangeRequest(
            action=action,
            name=parsed.name,
            quantity=max(parsed.quantity, 0.0),
            location=parsed.location or None,
            spec=parsed.spec or None,
            category=None if parsed.category == DEFAULT_CATEGORY else parsed.category,
            aliases=parsed.aliases,
            operator=operator,
            source="qq" if scene.startswith("qq") else "api",
            raw_text=raw,
            auto_create=action is StockAction.IN,
        )
        try:
            result = self.inventory.change_stock(request)
        except Ambiguous as exc:
            return self._ambiguous_reply(exc, parsed.action, session, label=parsed.name)
        except LocationConflict as exc:
            return self._ask_location_choice(exc, request, session)

        reply = result.message
        if parsed.reason:
            reply += f"\n（AI 理解：{parsed.reason}）"
        if parsed.category and parsed.category != DEFAULT_CATEGORY:
            reply += f"\n（已归入新分类：{category_label(parsed.category)}）"
        response = self._reply(reply, parsed.action, result.model_dump())
        if action is StockAction.IN and result.item is not None:
            return await self._maybe_ask_details(response, session, result.item.id)
        return response

    # ------------------------------------------------------------------ 文本批量导入
    async def _import_preview_reply(
        self,
        preview,
        session: Session,
        *,
        action: str,
        payload: dict,
        header: str,
    ) -> BotCommandResponse:
        """统一的「解析预览 → 等确认」回复。

        先把整批交给 AI 做预处理（**规范命名 + 归类**，一次调用），再让用户确认；
        结果直接写进 ``preview.rows``，所以确认后落库的就是 AI 规范好的名称与分类。
        把**解析结果**存进会话（而不是原始字节），避免大文件常驻内存。
        """
        normalize = bool(self.settings.ai_normalize_on_import)
        classify = bool(self.settings.ai_classify_on_import)
        summary = None
        if (normalize or classify) and self._llm_ready() and preview.rows:
            assert self.ai is not None  # noqa: S101 - _llm_ready 已判断
            try:
                summary = await self.ai.analyze_preview(
                    preview, normalize=normalize, classify=classify
                )
            except Exception:  # noqa: BLE001 - 预处理失败不能挡住导入
                logger.warning("AI 导入预处理失败，保留原值", exc_info=True)

        session.set_pending(
            PendingConfirmation(
                description=f"导入 {preview.total} 条库存记录（{preview.filename or '未命名'}）",
                action=action,
                payload={
                    **payload,
                    "rows": [row.model_dump() for row in preview.rows],
                    "filename": preview.filename,
                    "fmt": preview.fmt,
                },
            )
        )
        lines = [f"{header} {preview.total} 条记录："]
        lines.extend("  " + _render_import_row(row) for row in preview.rows[:10])
        if preview.total > 10:
            lines.append(f"  …… 还有 {preview.total - 10} 条")
        if preview.warnings:
            lines.append(f"（有 {len(preview.warnings)} 条提示，例如：{preview.warnings[0]}）")

        if summary is not None and (summary.renamed or summary.categorized):
            actions: list[str] = []
            if summary.renamed:
                actions.append(f"规范命名 {summary.renamed} 条")
            if summary.categorized:
                actions.append(f"归类 {summary.categorized} 条")
            extra = (
                f"；新建分类：{'、'.join(category_label(code) for code in summary.new_categories)}"
                if summary.new_categories
                else ""
            )
            lines.append(f"（AI {'、'.join(actions)}{extra}）")
            breakdown = _category_summary(preview.rows)
            if breakdown:
                lines.append(f"（分类：{breakdown}）")
            if summary.renamed:
                lines.append("原名称已保留为别名，搜旧写法照样能找到。")

        lines.append("")
        lines.append("回复「确认」导入（同名同位置的数量会被覆盖），或「取消」放弃。")
        return self._reply(
            "\n".join(lines),
            "import",
            {
                "total": preview.total,
                "renamed": summary.renamed if summary else 0,
                "new_categories": summary.new_categories if summary else [],
            },
        )

    async def handle_attachment(
        self,
        filename: str,
        data: bytes,
        *,
        operator: str = "",
        scene: str = "api",
        conversation: str | None = None,
    ) -> BotCommandResponse:
        """处理外部渠道（QQ 文件消息等）直接发来的文件。"""
        session = self.sessions.get(self._key(scene, conversation or operator))
        if self.importer is None:
            return self._reply("导入功能不可用。", "import")

        name = filename or "qq-file"
        try:
            preview = parse_input(name, data=data)
        except WarehouseError as exc:
            return self._reply(
                f"「{name}」我没法解析：{exc.message}\n"
                "支持的格式：.xlsx / .csv / .txt / .md / .json（Excel 请存成 .xlsx，旧版 .xls 请另存）",
                "import",
            )
        if not preview.rows:
            return self._reply(
                f"读到了「{name}」（{len(data)} 字节），但没解析出任何条目。\n"
                "文件里需要有表头或结构化写法，例如：\n"
                "| 名称 | 数量 | 位置 |\n| --- | --- | --- |\n| NE555 | 30 | C柜-1层 |",
                "import",
            )
        return await self._import_preview_reply(
            preview,
            session,
            action="import_file",
            payload={},
            header=f"我从「{name}」里解析出",
        )

    async def _request_text_import(
        self, raw: str, session: Session, operator: str, scene: str
    ) -> BotCommandResponse | None:
        """把 QQ 里直接粘贴的一整批清单交给文件解析器。"""
        if self.importer is None:
            return None
        try:
            preview = parse_input("qq-paste.md", text=raw)
        except WarehouseError as exc:
            return self._reply(f"这段内容我没法解析：{exc.message}", "import")
        if not preview.rows:
            return None
        return await self._import_preview_reply(
            preview, session, action="import_text", payload={}, header="我从这段内容里解析出"
        )

    # ------------------------------------------------------------------ AI 整理
    async def _tidy(self, session: Session, operator: str) -> BotCommandResponse:
        if not self._llm_ready():
            return self._reply("AI 整理需要先配置大模型（.env 里 LLM_ENABLED=true 与 LLM_API_KEY）。", "tidy")
        assert self.ai is not None  # noqa: S101
        try:
            plan = await self.ai.propose_tidy()
        except WarehouseError as exc:
            return self._reply(f"整理失败：{exc.message}", "tidy")

        if not plan.changes and not plan.duplicates:
            detail = f"\n（{plan.summary}）" if plan.summary else ""
            return self._reply(f"库存看起来已经挺整齐了，没有要改的。{detail}", "tidy")

        session.set_pending(
            PendingConfirmation(
                description=f"应用 {len(plan.changes)} 处整理建议",
                action="tidy",
                payload={"plan": plan.model_dump()},
            )
        )
        lines = [f"我看了一遍当前在库的条目，建议改 {len(plan.changes)} 处："]
        if plan.summary:
            lines.append(f"（{plan.summary}）")
        lines.extend("  · " + _describe_tidy(change) for change in plan.changes[:10])
        if len(plan.changes) > 10:
            lines.append(f"  …… 还有 {len(plan.changes) - 10} 处")

        # 把「可能是同一种东西」的分组记进会话，用户可以回「合并 1」真的合并掉
        session.merge_groups = [list(group) for group in plan.duplicates if len(group) > 1]
        if session.merge_groups:
            lines.append("")
            lines.append("另外这几组可能是同一种东西（我不会自动合并）：")
            for index, group in enumerate(session.merge_groups[:5], start=1):
                names = self._group_names(group)
                if names:
                    lines.append(f"  {index}. " + " ≈ ".join(names))
            if len(session.merge_groups) > 5:
                lines.append(f"  …… 还有 {len(session.merge_groups) - 5} 组")
            lines.append("要合并就回「合并 1」，或「合并这些」全部合并；也可以自己手动处理。")
        lines.append("")
        lines.append("回复「确认」应用这些修改，或「取消」放弃。")
        return self._reply("\n".join(lines), "tidy", {"changes": len(plan.changes)})

    # ------------------------------------------------------------------ 位置冲突
    def _ask_location_choice(
        self, exc: LocationConflict, request: StockChangeRequest, session: Session
    ) -> BotCommandResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        item = detail.get("item") or {}
        name = str(item.get("name") or request.name or "")
        new_location = str(detail.get("requested_location") or request.location or "")
        existing_location = str(item.get("location") or "")
        existing_quantity = item.get("quantity", 0)

        session.set_pending(
            PendingConfirmation(
                description=f"「{name}」的位置怎么处理（合并到 {new_location} / 分开存）",
                action="location_choice",
                item_ids=[int(item["id"])] if item.get("id") else [],
                payload={
                    "name": name,
                    "quantity": request.quantity,
                    "spec": request.spec,
                    "aliases": list(request.aliases or []),
                    "location": new_location,
                },
            )
        )
        return self._reply(
            f"「{name}」已经有一条记录了：\n"
            f"  @ {existing_location or '（未指定位置）'} × {fmt_qty(existing_quantity)}\n\n"
            f"你这次填的位置是「{new_location}」，要怎么处理？\n"
            f"  ① 合并 —— 把位置改成 {new_location}，只保留一条\n"
            f"  ② 分开 —— 在 {new_location} 另建一条，原来那条不动\n\n"
            f"回复「合并」或「分开」（回复「取消」放弃本次入库）。",
            "location",
            {"existing": item, "requested_location": new_location},
        )

    async def _resolve_location_choice(
        self, raw: str, session: Session, operator: str, scene: str
    ) -> BotCommandResponse:
        pending = session.pending
        assert pending is not None  # noqa: S101 - 调用点已判断
        text = raw.strip()
        if text in _MERGE_WORDS:
            merge = True
        elif text in _SPLIT_WORDS:
            merge = False
        elif is_negative(text):
            session.clear_pending()
            return self._reply("已取消，本次没有入库。", "location")
        else:
            return self._reply("回复「合并」或「分开」就行；回复「取消」放弃本次入库。", "location")

        session.clear_pending()
        payload = pending.payload
        request = StockChangeRequest(
            action=StockAction.IN,
            item_id=pending.item_ids[0] if pending.item_ids else None,
            name=payload.get("name") or None,
            quantity=float(payload.get("quantity") or 0),
            location=payload.get("location"),
            spec=payload.get("spec") or None,
            aliases=list(payload.get("aliases") or []),
            operator=operator,
            source="qq" if scene.startswith("qq") else "api",
            merge_location=merge,
        )
        try:
            result = self.inventory.change_stock(request)
        except WarehouseError as exc:
            return self._reply(exc.message, "location")

        prefix = "已合并位置" if merge else "已在目标位置另建一条"
        response = self._reply(f"{prefix}：{result.message}", "location", result.model_dump())
        if result.created and result.item is not None:
            return await self._maybe_ask_details(response, session, result.item.id)
        return response

    # ------------------------------------------------------------------ 采购清单比对
    def _procure(self, rest: str, session: Session) -> BotCommandResponse:
        items = _parse_procure_list(rest)
        if not items:
            return self._reply(
                "把采购清单发给我就行，支持两种写法：\n"
                "· 表格（可以直接从 Excel 粘贴）：\n"
                "    采购\n"
                "    | 名称 | 数量 | 规格 |\n    | --- | --- | --- |\n"
                "    | 100nF 50V | 1000 | 0805 |\n"
                "· 一行一个：\n"
                "    采购 STM32F103C8T6 25, 100nF 50V（0805） 1000\n\n"
                "我会对比库存，告诉你哪些够了（在哪个位置、多少件）、哪些还差多少。\n"
                "**写了规格就按规格算** —— 要 0805 的不会拿 0603 的库存顶上。",
                "procure",
            )

        enough: list = []
        short: list = []
        missing: list = []
        matched_ids: list[int] = []

        for name, required, spec, _location in items[:40]:
            all_hits = self.inventory.search(name, limit=10).hits
            strong = [hit for hit in all_hits if hit.score >= _PROCURE_MATCH_SCORE]
            other_specs: list[str] = []

            # 清单写了封装就以封装为准 —— 要 0805 的不能拿 0603 的库存顶上
            if spec:
                matched = [hit for hit in strong if _spec_key(hit.spec) == _spec_key(spec)]
                if not matched:
                    target_key = _spec_key(spec)
                    other_specs = sorted(
                        {hit.spec for hit in all_hits if hit.spec and _spec_key(hit.spec) != target_key}
                    )
                strong = matched

            entry = {
                "name": name,
                "spec": spec,
                "required": required,
                "available": sum(hit.quantity for hit in strong),
                "hits": strong,
                "other_specs": other_specs,
            }
            matched_ids.extend(hit.id for hit in strong)
            if not strong:
                missing.append(entry)
            elif required > 0 and entry["available"] < required:
                short.append(entry)
            else:
                enough.append(entry)

        lines = [
            f"对照采购清单：{len(items)} 项 —— 已够 {len(enough)}，不足 {len(short)}，缺 {len(missing)}"
        ]

        def label_for(entry: dict) -> str:
            return f"{entry['name']}（{entry['spec']}）" if entry["spec"] else entry["name"]

        def block(title: str, entries: list, marker: str, deficit: bool) -> None:
            if not entries:
                return
            lines.append("")
            lines.append(title)
            for entry in entries[:10]:
                head = f"  {marker} {label_for(entry)}"
                if entry["required"] > 0:
                    head += f"  需要 {fmt_qty(entry['required'])} / 现有 {fmt_qty(entry['available'])}"
                    if deficit:
                        head += f"　还差 {fmt_qty(entry['required'] - entry['available'])}"
                else:
                    head += f"  现有 {fmt_qty(entry['available'])}（清单没写数量）"
                lines.append(head)
                for hit in entry["hits"][:5]:
                    line = f"      @ {hit.location or '（未指定位置）'} × {fmt_qty(hit.quantity)}"
                    if hit.spec:
                        line += f"（{hit.spec}）"
                    lines.append(line)

        block("【已够】", enough, "✓", deficit=False)
        block("【不足，需要补】", short, "⚠", deficit=True)

        if missing:
            lines.append("")
            lines.append("【库存里没有】")
            for entry in missing[:10]:
                head = f"  ✗ {label_for(entry)}"
                if entry["required"] > 0:
                    head += f"  需要 {fmt_qty(entry['required'])}"
                lines.append(head)
                if entry["other_specs"]:
                    lines.append(
                        f"      （有同名但封装不同：{'、'.join(entry['other_specs'][:3])}）"
                    )

        to_buy = [(label_for(e), e["required"] - e["available"]) for e in short]
        to_buy += [(label_for(e), e["required"]) for e in missing]

        lines.append("")
        if to_buy:
            lines.append("需要额外购买：")
            for name, amount in to_buy[:15]:
                lines.append(f"  · {name}" + (f" × {fmt_qty(amount)}" if amount > 0 else "（数量待定）"))
            total = sum(amount for _n, amount in to_buy)
            lines.append(f"合计 {len(to_buy)} 项" + (f" / {fmt_qty(total)} 件" if total else ""))
        else:
            lines.append("清单里的东西库存都够，不需要额外采购 👍")

        session.set_items(matched_ids, label="采购清单里已拥有的物品")
        return self._reply(
            "\n".join(lines),
            "procure",
            {"total": len(items), "enough": len(enough), "short": len(short), "missing": len(missing)},
        )

    # ------------------------------------------------------------------ 归类
    def _categorize(
        self, name: str, category_text: str, operator: str, session: Session
    ) -> BotCommandResponse:
        if not name or not category_text:
            return self._reply("用法：归类 <名称> <分类>，例如「归类 杜邦线 STM元器件」", "categorize")
        try:
            record, _, candidates, matched_by = self.inventory.resolve(name)
        except Ambiguous as exc:
            return self._ambiguous_reply(exc, "categorize", session, label=name)

        if matched_by == "weak-type":
            record = None      # 只是同类，不是同一条
        if record is None:
            lines = [f"没找到「{name}」"]
            if candidates:
                lines.append("你是不是想说：")
                lines.extend(f"  · {c.name}" for c in candidates[:5])
            return self._reply("\n".join(lines), "categorize")

        # 先按「库里已有的分类」做大小写 / 包含匹配，避免建出仅大小写不同的重复分类：
        # 库里有「STM元器件」时，「归入 stm元器件」应当复用而不是新建一个小写分类。
        code = self._known_category(category_text) or match_category(category_text)
        if code == record.category:
            return self._reply(f"「{record.name}」本来就在「{category_label(code)}」里。", "categorize")

        previous = category_label(record.category)
        existed = code in set(self.inventory.repo.category_names())
        updated = self.inventory.update_item(record.id, ItemUpdate(category=code, operator=operator))
        suffix = "" if existed else f"（新建了分类「{category_label(code)}」）"
        return self._reply(
            f"已把「{updated.name}」从「{previous}」改到「{category_label(code)}」{suffix}",
            "categorize",
            {"item_id": updated.id, "category": code},
        )

    def _categorize_command(self, rest: str, operator: str, session: Session) -> BotCommandResponse:
        parts = rest.split()
        if len(parts) < 2:
            return self._reply(
                "用法：归类 <名称> <分类>\n例如：归类 杜邦线 STM元器件、归类 杜邦线 耗材",
                "categorize",
            )
        if len(parts) >= 3 and parts[-2] in {"到", "为", "成", "→", "->"}:
            return self._categorize(" ".join(parts[:-2]), parts[-1], operator, session)
        return self._categorize(" ".join(parts[:-1]), parts[-1], operator, session)

    # ------------------------------------------------------------------ 删除
    def _delete(self, rest: str, session: Session, operator: str, scene: str) -> BotCommandResponse:
        if not rest:
            return self._reply(
                "用法：删除 <名称>（会先让你确认）\n"
                "· 只删已清零的条目 → 「清理零库存」\n"
                "· 清空全部 → 「删除全部」",
                "delete",
            )
        try:
            record, _, candidates, matched_by = self.inventory.resolve(rest)
        except Ambiguous as exc:
            return self._ambiguous_reply(exc, "delete", session, label=rest)

        if matched_by == "weak-type":
            record = None
        if record is None:
            lines = [f"没找到「{rest}」"]
            if candidates:
                lines.append("你是不是想说：")
                lines.extend(f"  · {c.name}" for c in candidates[:5])
            return self._reply("\n".join(lines), "delete")

        session.set_pending(
            PendingConfirmation(
                description=f"删除「{record.name}」",
                action="delete_item",
                item_ids=[record.id],
                payload={"name": record.name},
            )
        )
        return self._reply(
            f"将删除「{record.name}」"
            f"（{fmt_qty(record.quantity)} 件{(' @ ' + record.location) if record.location else ''}）。\n"
            "出入库流水会保留，但这条物品记录本身不再存在，且无法恢复。\n\n"
            "回复「确认」执行，或「取消」放弃。",
            "delete",
        )

    def _delete_all(self, session: Session) -> BotCommandResponse:
        records = self._all_items()
        if not records:
            return self._reply("库是空的，没有可删的。", "delete_all")
        session.set_pending(
            PendingConfirmation(
                description=f"删除全部 {len(records)} 条物品记录",
                action="delete_all",
                item_ids=[r.id for r in records],
            )
        )
        names = "、".join(r.name for r in records[:8])
        return self._reply(
            f"⚠️ 这会删除**全部 {len(records)} 条**物品记录：\n"
            f"涉及：{names}" + ("…" if len(records) > 8 else "") + "\n\n"
            "出入库流水和审计日志会保留，但物品记录无法恢复。\n"
            "回复「确认」执行，或「取消」放弃。",
            "delete_all",
            {"count": len(records)},
        )

    # ------------------------------------------------------------------ 合并重复条目
    @staticmethod
    def _describe_record(record: Any) -> str:
        spec = f"（{record.spec}）" if record.spec else ""
        location = f" @ {record.location}" if record.location else ""
        return f"{record.name}{spec}{location} × {fmt_qty(record.quantity)}"

    def _group_names(self, item_ids: Sequence[int]) -> list[str]:
        """把一组 id 渲染成 ``名称（规格） ×N``，用于「整理」结果里展示候选。"""
        names: list[str] = []
        for item_id in item_ids:
            try:
                names.append(self._describe_record(self.inventory.get_item(item_id)))
            except NotFound:
                continue
        return names

    def _merge(self, rest: str, session: Session, operator: str, scene: str) -> BotCommandResponse:
        """合并「可能是同一种东西」的重复条目（整理之后使用）。"""
        resolved = self._resolve_merge_plan(rest, session)
        if isinstance(resolved, BotCommandResponse):
            return resolved
        plan, problems = resolved

        if not plan:
            lines = ["没有可以合并的分组。"]
            lines.extend(f"  · {problem}" for problem in problems[:5])
            return self._reply("\n".join(lines), "merge")

        session.set_pending(
            PendingConfirmation(
                description=f"合并 {len(plan)} 组记录",
                action="merge",
                item_ids=[target.id for target, _sources in plan],
                payload={
                    "groups": [
                        {"target": target.id, "sources": [record.id for record in sources]}
                        for target, sources in plan
                    ]
                },
            )
        )

        lines = [f"将合并 {len(plan)} 组："]
        for target, sources in plan:
            lines.append(f"  保留：{self._describe_record(target)}")
            for record in sources:
                lines.append(f"  并入：{self._describe_record(record)}")
            total = target.quantity + sum(record.quantity for record in sources)
            lines.append(f"  → 合并后 {fmt_qty(total)} 件，被并入的名称会留成别名")
            specs = {record.spec for record in [target, *sources] if record.spec}
            if len(specs) > 1:
                lines.append(
                    f"  ⚠️ 规格不同（{' / '.join(sorted(specs))}）"
                    "—— 如果不是同一种东西，请回「取消」"
                )
        if problems:
            lines.append("")
            lines.extend(f"  · {problem}" for problem in problems[:5])
        lines.append("")
        lines.append("回复「确认」执行，或「取消」放弃。")
        return self._reply("\n".join(lines), "merge", {"groups": len(plan)})

    def _resolve_merge_plan(
        self, rest: str, session: Session
    ) -> tuple[list[tuple[Any, list[Any]]], list[str]] | BotCommandResponse:
        """把用户输入解析成 ``[(保留的记录, [要并入的记录]), ...]``。"""
        text = (rest or "").strip()
        groups = [list(group) for group in session.merge_groups]

        def load(item_ids: Sequence[int]) -> list[Any]:
            records: list[Any] = []
            for item_id in item_ids:
                try:
                    records.append(self.inventory.get_item(item_id))
                except NotFound:
                    continue
            return records

        if not text or text in {"这些", "全部", "所有", "都", "这些全部", "全部合并"}:
            if not groups:
                return self._reply(
                    "我还没有可以合并的候选。先发「整理」，我会找出可能重复的记录。", "merge"
                )
            chosen = groups
        elif all(part.isdigit() for part in text.split()):
            if not groups:
                return self._reply("我还没有可以合并的候选。先发「整理」。", "merge")
            chosen = []
            for part in text.split():
                index = int(part) - 1
                if not 0 <= index < len(groups):
                    return self._reply(
                        f"没有第 {part} 组（上次整理一共 {len(groups)} 组）。", "merge"
                    )
                chosen.append(groups[index])
        else:
            match = _MERGE_RE.match(text)
            if not match:
                return self._reply(
                    "用法：\n"
                    "· 合并 1 —— 合并上次「整理」列出的第 1 组\n"
                    "· 合并这些 —— 合并全部分组\n"
                    "· 合并 <要并掉的> 到 <保留的> —— 手动指定两条记录",
                    "merge",
                )
            source_name = match.group("src").strip(" 的，,")
            target_name = match.group("dst").strip(" 的，,")

            def parse_side(text: str) -> tuple[str, str | None]:
                """允许写成 ``名称 @位置``（同名记录分散在多个位置时要用）。"""
                row = parse_inline_entry(text)
                if row is None or not row.name:
                    return text, None
                return row.name, (row.location or None)

            source_text, source_location = parse_side(source_name)
            target_text, target_location = parse_side(target_name)
            try:
                source, _score, _candidates, source_matched = self.inventory.resolve(
                    source_text, location=source_location
                )
                target, _tscore, _tcandidates, target_matched = self.inventory.resolve(
                    target_text, location=target_location
                )
            except Ambiguous:
                return self._reply("名称有多个候选，补个 @位置 再试。", "merge")
            if source_matched == "weak-type":
                source = None
            if target_matched == "weak-type":
                target = None
            if source is None or target is None:
                return self._reply(
                    f"没找到「{source_name if source is None else target_name}」。", "merge"
                )
            if source.id == target.id:
                return self._reply("这两条已经是同一条记录了。", "merge")
            return ([(target, [source])], [])

        plan: list[tuple[Any, list[Any]]] = []
        problems: list[str] = []
        for group in chosen:
            records = load(group)
            if len(records) < 2:
                problems.append("有一组已经不足 2 条了，跳过")
                continue
            # 信息最全的那条当保留项：有规格 > 别名多 > 数量多 > id 小（先建的）
            target = max(records, key=lambda r: (bool(r.spec), len(r.aliases), r.quantity, -r.id))
            plan.append((target, [record for record in records if record.id != target.id]))
        return plan, problems

    # ------------------------------------------------------------------ 追问补全
    def _handle_prompt_answer(
        self, raw: str, session: Session, operator: str
    ) -> BotCommandResponse | None:
        """处理用户对「追问」的回答。

        返回 ``None`` 表示这句话其实是一条新指令，调用方应继续走正常流程。
        """
        slot = session.current_prompt()
        if slot is None:
            return None
        text = raw.strip()
        if not text:
            return None

        if text.casefold() in _SKIP_WORDS:
            session.decline_prompt()
            return self._continue_prompts(session, "已跳过。")

        if is_negative(text):
            session.clear_prompts()
            return self._reply("好的，不再追问。", "prompt")

        # 用户可能在纠正分类（「这没有封装啊，这是游戏卡」）——
        # 这时不能把他的吐槽当成规格存进去
        if slot.kind == "spec":
            hint = detect_category_hint(text)
            if hint:
                try:
                    current = self.inventory.get_item(slot.item_id)
                except NotFound:
                    current = None
                if current is not None and hint != current.category:
                    self.inventory.update_item(
                        slot.item_id, ItemUpdate(category=hint, operator=operator)
                    )
                    session.decline_prompt()
                    return self._continue_prompts(
                        session,
                        f"明白了，这是「{category_label(hint)}」，不需要填封装，已按分类处理。",
                    )

        if self._looks_like_new_command(text, session):
            session.clear_prompts()
            return None

        if slot.kind == "spec":
            self.inventory.update_item(slot.item_id, ItemUpdate(spec=text, operator=operator))
            note = f"已记录规格：{text}"
        elif slot.kind == "alias":
            aliases = [piece.strip() for piece in re.split(r"[,，、/\s]+", text) if piece.strip()]
            if not aliases:
                session.decline_prompt()
                return self._continue_prompts(session, "没识别到别名，已跳过。")
            self.inventory.add_aliases(slot.item_id, aliases, operator=operator)
            note = f"已记录别名：{'、'.join(aliases)}"
        else:
            note = "已记录。"

        session.pop_prompt()
        return self._continue_prompts(session, note)

    def _continue_prompts(self, session: Session, prefix: str) -> BotCommandResponse:
        slot = session.current_prompt()
        if slot is None:
            return self._reply(f"{prefix}\n信息补全完成 ✓", "prompt")
        return self._reply(f"{prefix}\n\n{self._prompt_question(slot)}", "prompt")

    def _prompt_question(self, slot: PromptSlot) -> str:
        try:
            record = self.inventory.get_item(slot.item_id)
        except NotFound:
            return "还有什么要补充的吗？没有就回「跳过」"
        if slot.kind == "spec":
            # 不再按内置分类切换话术 —— 分类是 AI 定的，这里统一给中性提示
            return (
                f"「{record.name}」要补充规格/封装吗？"
                "（例：LQFP48、0805、DO-35、SOT-223、50元）\n"
                "没有这回事就直接回「跳过」（比如游戏卡、日用品）。"
            )
        if slot.kind == "alias":
            return f"「{record.name}」还有别的叫法吗？多个用逗号分隔（用于模糊匹配）。\n没有就回「跳过」"
        return "还有什么要补充的吗？没有就回「跳过」"

    def _looks_like_new_command(self, text: str, session: Session) -> bool:
        """判断这句话是「新指令」还是「对追问的回答」。

        宁可把含糊的输入当回答（用户还能用「跳过」纠正），
        也不要让追问把真正的指令吃掉。
        """
        if len(text) > 40:
            return True
        match = _COMMAND_RE.match(text)
        if match and COMMAND_ALIASES.get(match.group(1)):
            return True
        if parse_bulk_request(text):
            return True
        if _STOCK_VERB.search(text):
            return True
        if session.candidates and parse_selection(text):
            return True
        intent, _ = self.nlq.parse(text)
        return intent not in {"search", "unknown"}

    async def _maybe_ask_details(
        self, response: BotCommandResponse, session: Session, item_id: int
    ) -> BotCommandResponse:
        """入库后，对缺失的字段逐个追问。

        **但先判断这些信息是否值得问**：不值得就直接完成，不打断用户。
        """
        if not self.settings.qq_ask_on_stock_in:
            return response
        try:
            record = self.inventory.get_item(item_id)
        except NotFound:
            return response

        spec_slot = not record.spec and (record.id, "spec") not in session.declined
        alias_slot = not record.aliases and (record.id, "alias") not in session.declined
        if not (spec_slot or alias_slot):
            return response

        ask_spec, ask_alias = await self._decide_detail_prompts(record, spec_slot, alias_slot)
        slots: list[PromptSlot] = []
        if ask_spec:
            slots.append(PromptSlot(kind="spec", item_id=record.id, item_name=record.name))
        if ask_alias:
            slots.append(PromptSlot(kind="alias", item_id=record.id, item_name=record.name))
        if not slots:
            return response

        session.ask(slots)
        response.reply = f"{response.reply}\n\n{self._prompt_question(slots[0])}"
        return response

    async def _decide_detail_prompts(
        self, record: Any, spec_slot: bool, alias_slot: bool
    ) -> tuple[bool, bool]:
        """决定要不要追问。规则能定的不问 AI（省一次往返），定不了才交给大模型。"""
        mode = (self.settings.qq_detail_prompts or "auto").lower()
        if mode == "never":
            return (False, False)
        if mode == "always":
            return (spec_slot, alias_slot)

        decision = _rule_detail_decision(record)
        if decision is None and self._llm_ready():
            assert self.ai is not None  # noqa: S101 - _llm_ready 已判断
            try:
                judged = await self.ai.judge_detail_prompts(record)
            except Exception:  # noqa: BLE001 - 判断失败不能挡住入库
                logger.warning("AI 判断是否追问失败，回退到默认策略", exc_info=True)
            else:
                logger.info(
                    "AI 判断追问：%s → ask_spec=%s ask_alias=%s（%s）",
                    record.name,
                    judged.ask_spec,
                    judged.ask_alias,
                    judged.reason,
                )
                decision = (judged.ask_spec, judged.ask_alias)

        if decision is None:
            # AI 不可用或失败：回到「缺什么问什么」
            decision = (spec_slot, alias_slot)
        return (decision[0] and spec_slot, decision[1] and alias_slot)

    # ------------------------------------------------------------------ 清单
    def _listing(
        self,
        session: Session,
        records: Sequence[Any],
        *,
        title: str,
        command: str,
        label: str,
        extra_lines: Sequence[str] = (),
        limit: int = ITEM_LIMIT,
        data: dict | None = None,
        empty_text: str = "（空）",
    ) -> BotCommandResponse:
        """统一渲染带序号的清单，并把候选登记进上下文。"""
        visible = list(records[:limit])
        candidates = build_candidates(visible)
        session.set_candidates(candidates, label=label)

        lines = list(extra_lines)
        if title:
            lines.append(title)
        lines.extend(candidate.render() for candidate in candidates)
        if len(records) > limit:
            lines.append(f"  …… 还有 {len(records) - limit} 项")
        if not candidates:
            lines = [*extra_lines, empty_text] if extra_lines else [empty_text]

        payload = dict(data or {})
        payload.update({"count": len(records), "items": [c.as_dict() for c in candidates]})
        return self._reply("\n".join(lines), command, payload)

    # ------------------------------------------------------------------ 在库 / 零库存
    def _all_items(self) -> list[Any]:
        """**全量**物品记录。

        ⚠️ 这里绝不能有 ``limit`` —— 破坏性操作（``出库全部``/``删除全部``）依赖它。
        以前用 ``list_items(limit=1000)``，库超过 1000 条时会**静默只处理前 1000 条**
        却上报「全部完成」，用户以为清空了实际没有。
        """
        return list(self.inventory.repo.all_items())

    def _known_category(self, name: str) -> str | None:
        """把用户写的分类说法解析成分类名 —— **只认库里真实存在的分类**。

        没有内置分类表了，所以判据就是「库里有没有」：

        * 完整分类名（``STM元器件``）
        * 包含关系（``游戏卡`` → ``Steam游戏卡``）—— 用户很少写全名
        * 忽略大小写

        包含关系**只在唯一命中时**才认，避免「卡」这种说法歧义。
        """
        # ⚠️ 必须用 default="" 探测：normalize_category_code 默认会回退成「未分类」，
        # 而「未分类」恰好是库里真实存在的分类 —— 那样任何非法名称都会被当成它。
        candidate = normalize_category_code(name, default="")
        if not candidate:
            return None
        # 只取分类集合（走索引），不再为了一个集合把全表读成记录对象
        known = set(self.inventory.repo.category_names())
        if candidate in known:
            return candidate
        folded = candidate.casefold()
        partial = [
            code
            for code in known
            if code.casefold() == folded
            or folded in code.casefold()
            or code.casefold() in folded
        ]
        return partial[0] if len(partial) == 1 else None

    def _in_stock(self) -> list[Any]:
        """在库物品：数量 > 0。清单默认只看这些 —— 已出库的条目不该继续占版面。"""
        return [record for record in self._all_items() if record.quantity > 0]

    def _zero_items(self) -> list[Any]:
        return [record for record in self._all_items() if record.quantity <= 0]

    def _overview(self, session: Session) -> BotCommandResponse:
        live = self._in_stock()
        zero_count = len(self._all_items()) - len(live)
        locations = {record.location for record in live if record.location}
        extra = [
            f"库存总览：在库 {len(live)} 种，共 {fmt_qty(sum(r.quantity for r in live))} 件，"
            f"{len(locations)} 个位置"
        ]
        for _, label, count, quantity in self.analysis.live_category_summary():
            extra.append(f"  · {label}：{count} 种 / {fmt_qty(quantity)} 件")
        if zero_count:
            extra.append(f"  （另有 {zero_count} 种已清零，回复「零库存」查看）")
        extra.append("")
        return self._listing(
            session,
            live,
            title="物品清单：",
            command="overview",
            label="在库物品",
            extra_lines=extra,
            empty_text="当前没有在库物品（数量都已是 0）。回复「零库存」查看已清零的条目。",
        )

    def _list(self, session: Session) -> BotCommandResponse:
        live = self._in_stock()
        return self._listing(
            session,
            live,
            title=f"在库共 {len(live)} 种物品：",
            command="list",
            label="在库物品",
            empty_text="当前没有在库物品。回复「零库存」查看已清零的条目。",
        )

    def _category(self, name: str, session: Session) -> BotCommandResponse:
        category = self._known_category(name)
        if not category:
            known = self.inventory.repo.category_names()
            labels = "、".join(sorted(category_label(c) for c in known)) or "（还没有分类，先入库或导入，AI 会自动归类）"
            return self._reply(f"「{name}」不是一个分类。已有的分类：{labels}", "category")
        total, records = self.inventory.list_items(category=category, limit=1000)
        live = [record for record in records if record.quantity > 0]
        label = category_label(category)
        if not total:
            return self._reply(f"「{label}」下还没有物品", "category")
        empty_note = f"  （另有 {total - len(live)} 种已清零，回复「零库存」查看）" if total > len(live) else ""
        extra = [empty_note] if empty_note else []
        return self._listing(
            session,
            live,
            title=f"「{label}」在库 {len(live)} 种：",
            command="category",
            label=label,
            extra_lines=extra,
            empty_text=f"「{label}」下的物品已全部清零。",
        )

    def _location(self, name: str, session: Session) -> BotCommandResponse:
        total, records = self.inventory.list_items(location=name, limit=1000)
        if not total:
            return self._reply(f"「{name}」下没有记录", "location")
        live = [record for record in records if record.quantity > 0]
        extra = []
        if total > len(live):
            extra.append(f"  （另有 {total - len(live)} 种已清零）")
        return self._listing(
            session,
            live,
            title=f"「{name}」在库 {len(live)} 种物品：",
            command="location",
            label=f"位置 {name}",
            extra_lines=extra,
            empty_text=f"「{name}」下的物品已全部清零。",
        )

    def _zero_list(self, session: Session) -> BotCommandResponse:
        zero = self._zero_items()
        if not zero:
            return self._reply("没有已清零的物品 👍", "zero_list")
        return self._listing(
            session,
            zero,
            title=(
                f"已清零的物品（{len(zero)} 种，数量为 0）：\n"
                "这些记录还在库里，只是数量为 0。要删掉请回「清理零库存」。"
            ),
            command="zero_list",
            label="已清零的物品",
        )

    def _request_zero_clean(self, session: Session) -> BotCommandResponse:
        zero = self._zero_items()
        if not zero:
            return self._reply("没有已清零的物品，无需清理。", "zero_clean")
        pending = PendingConfirmation(
            description=f"删除 {len(zero)} 条已清零的物品记录",
            action="delete_zero",
            item_ids=[r.id for r in zero],
        )
        session.set_pending(pending)
        names = "、".join(r.name for r in zero[:8])
        return self._reply(
            f"将删除 {len(zero)} 条数量为 0 的记录：\n"
            f"涉及：{names}" + ("…" if len(zero) > 8 else "") + "\n\n"
            "（只删记录，不影响任何在库物品。删除后出库流水仍保留。）\n"
            "回复「确认」执行，或「取消」放弃。",
            "zero_clean",
            {"count": len(zero)},
        )

    def _search(self, keyword: str, session: Session) -> BotCommandResponse:
        response = self.inventory.search(keyword, limit=ITEM_LIMIT)
        if not response.hits:
            return self._reply(
                f"没找到和「{keyword}」相关的物品。\n换个短一点的关键词试试，"
                "或者用「入库 名称 数量 @位置」先建条目。",
                "search",
            )
        live = [hit for hit in response.hits if hit.quantity > 0]
        zero = [hit for hit in response.hits if hit.quantity <= 0]
        # 搜索保留已清零的（「确实有这个条目、只是没货了」本身是有用信息）
        ordered = live + zero
        return self._listing(
            session,
            ordered,
            title=f"「{keyword}」找到 {len(ordered)} 条" + (f"（其中 {len(zero)} 条已清零）" if zero else "") + "：",
            command="search",
            label=f"「{keyword}」的搜索结果",
        )

    def _low(self, session: Session) -> BotCommandResponse:
        items = self.analysis.low_stock(5, limit=ITEM_LIMIT)
        if not items:
            return self._reply("没有库存 ≤ 5 的物品", "low")
        return self._listing(
            session, items, title="库存偏少的物品（≤ 5）：", command="low", label="低库存"
        )

    # ------------------------------------------------------------------ 序号选择
    async def _select(
        self, session: Session, numbers: list[int], operator: str, scene: str
    ) -> BotCommandResponse:
        available = {c.index for c in session.candidates}
        missing = [n for n in numbers if n not in available]
        picked = session.pick(numbers)

        if not picked:
            return self._reply(
                f"清单里只有 1~{len(session.candidates)} 项，没有第 {missing[0]} 项。",
                "select",
            )

        intent = session.candidate_intent
        if intent and len(picked) == 1:
            candidate = picked[0]
            request = StockChangeRequest(
                action=StockAction(intent.action),
                item_id=candidate.item_id,
                quantity=intent.quantity,
                location=intent.location,
                spec=intent.spec,
                operator=operator,
                source="qq" if scene.startswith("qq") else "api",
                raw_text=f"选择第 {candidate.index} 项",
            )
            result = self.inventory.change_stock(request)
            session.consume_candidates()
            return self._reply(result.message, "select", result.model_dump())

        lines = [self._describe_item(self.inventory.get_item(c.item_id)) for c in picked]
        if missing:
            lines.append(f"（没有第 {'、'.join(map(str, missing))} 项，已忽略）")
        return self._reply("\n".join(lines), "select")

    def _describe_item(self, record: Any) -> str:
        lines = [f"{record.name}" + (f"（{record.spec}）" if record.spec else "")]
        lines.append(f"  数量：{fmt_qty(record.quantity)}" + (f" {record.unit}" if record.unit else ""))
        lines.append(f"  位置：{record.location or '（未记录）'}")
        lines.append(f"  分类：{category_label(record.category)}")
        if record.aliases:
            lines.append(f"  别名：{'、'.join(record.aliases)}")
        if record.note:
            lines.append(f"  备注：{truncate(record.note, 60)}")
        lines.append(f"  编号：#{record.id}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 出入库
    async def _stock(
        self, command: str, rest: str, raw: str, operator: str, scene: str, session: Session
    ) -> BotCommandResponse:
        if not rest:
            verb = {"in": "入库", "out": "出库", "set": "盘点"}[command]
            return self._reply(f"用法：{verb} <名称> <数量> [@位置]", command)

        # 「入库 + 整张表格」→ 批量导入；「出库 / 盘点 + 清单」→ 逐条执行
        if looks_like_inventory_text(rest):
            if command == "in" and self.importer is not None:
                response = await self._request_text_import(rest, session, operator, scene)
                if response is not None:
                    return response
            if command in {"out", "set"}:
                return self._request_batch_stock(command, rest, operator, scene, session)

        row = parse_inline_entry(rest)
        name = row.name if row else rest
        quantity = row.quantity if row else 0.0
        if quantity <= 0:
            # 解析器没能从行尾认出数量时的兜底。
            # 注意：要从**已清洗过的名称**上再剥一次，不能从原始串重建 ——
            # 否则「0.1uF 50V MLCC（DIP-8） 100」会把刚剔掉的规格又带回来。
            source = name if row is not None else rest
            matches = list(_LOOSE_NUMBER.finditer(source))
            if matches:
                last = matches[-1]
                quantity = float(last.group(1))
                name = f"{source[: last.start()]} {source[last.end():]}"
        name = re.sub(r"\s+", " ", name).strip(" @#")
        # 「入库 杜邦线 100 无位置」—— 位置可以明确说不填
        name = _SKIP_LOCATION_TAIL.sub("", name).strip()

        # 规则解析用不动（没名称 / 该有数量却没有），或配置成 AI 优先 → 交给大模型
        if self._should_use_ai(rest, name=name, quantity=quantity, command=command):
            ai_response = await self._try_ai_stock(raw, operator, scene, session)
            if ai_response is not None:
                return ai_response

        if not name:
            return self._reply("没解析出物品名称，换个写法试试", command)

        # 目标是**已存在的分类** → 整批操作（必须确认）。
        # 注意必须查库确认：没有内置分类了，「随便一段文字」不能当成分类。
        category = self._known_category(name)
        if category:
            if command == "out":
                return self._request_bulk_out("category", category, operator, scene, session)
            return self._reply(
                f"「{category_label(category)}」是分类，包含多种物品，不能直接{ '入库' if command == 'in' else '盘点' }。\n"
                f"看清单：分类 {category_label(category)}\n"
                "操作单个物品：出库 STM32F103C8T6 5",
                command,
            )

        if quantity <= 0 and command != "set":
            verb = "入库" if command == "in" else "出库"
            return self._reply(f"没解析出数量。写法示例：{verb} {name} 25 @A柜1层", command)

        location = row.location if row else ""
        spec = row.spec if row and row.spec else None
        if location and location.casefold() in _SKIP_WORDS:
            location = ""
        if not location:
            # 容忍没写 @ 的位置：「100欧姆电阻 C库」→ 名称 + 位置
            split = split_trailing_location(name)
            if split:
                name, location = split

        action = {"in": StockAction.IN, "out": StockAction.OUT, "set": StockAction.SET}[command]
        request = StockChangeRequest(
            action=action,
            name=name,
            quantity=max(quantity, 0.0),
            location=location or None,
            spec=spec,
            aliases=row.aliases if row else [],
            operator=operator,
            source="qq" if scene.startswith("qq") else "api",
            raw_text=rest,
            auto_create=command == "in",
        )
        try:
            result = self.inventory.change_stock(request)
        except Ambiguous as exc:
            return self._ambiguous_reply(
                exc,
                command,
                session,
                intent=PendingIntent(
                    action=command,
                    quantity=max(quantity, 0.0),
                    location=location or None,
                    spec=spec,
                ),
                label=name,
            )
        except LocationConflict as exc:
            return self._ask_location_choice(exc, request, session)
        response = self._reply(result.message, command, result.model_dump())
        if command == "in" and result.item is not None:
            # 入库后追问缺失的规格/别名（用户可回「跳过」）
            return await self._maybe_ask_details(response, session, result.item.id)
        return response

    def _ambiguous_reply(
        self,
        exc: Ambiguous,
        command: str,
        session: Session,
        *,
        intent: PendingIntent | None = None,
        label: str = "",
    ) -> BotCommandResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        candidates = build_candidates_from_hits(detail.get("candidates") or [])
        session.set_candidates(candidates, intent=intent, label=label)

        lines = [exc.message]
        lines.extend(candidate.render() for candidate in candidates)
        if intent is not None:
            lines.append(f"回复序号即可继续{intent.describe()}，或说得更具体一点。")
        else:
            lines.append("回复序号看详情，或说得更具体一点。")
        return self._reply("\n".join(lines), command, {"candidates": [c.as_dict() for c in candidates]})

    # ------------------------------------------------------------------ 整批出库
    # ------------------------------------------------------------------ 按清单批量出入库
    def _request_batch_stock(
        self, command: str, rest: str, operator: str, scene: str, session: Session
    ) -> BotCommandResponse:
        """「出库 <清单>」「盘点 <清单>」—— 逐条解析、逐条执行，执行前确认。"""
        verb = {"out": "出库", "set": "盘点"}[command]
        entries = _parse_stock_list(rest)
        if not entries:
            return self._reply(
                f"「{verb}」的清单我没解析出来。一行一个就行，例如：\n"
                f"{verb} NE555 30\n"
                f"{verb} 100nF 50V MLCC 0805 200\n"
                "（也可以直接把「库存」列出来的清单复制回来，在前面加个「" + verb + "」）",
                command,
            )

        plan: list[tuple[Any, float]] = []
        problems: list[str] = []
        for name, quantity, spec, location in entries:
            try:
                record, _score, _candidates, matched_by = self.inventory.resolve(
                    name, location=location or None, spec=spec or None
                )
            except Ambiguous:
                problems.append(f"{name}：有多个候选，补个 @位置 再试")
                continue
            if matched_by == "weak-type":
                record = None  # 只是同类，不是同一条
            if record is None:
                problems.append(f"{name}：库存里没有这条")
                continue
            if command == "out" and quantity <= 0:
                problems.append(f"{name}：清单里没写要出多少")
                continue
            plan.append((record, quantity))

        if not plan:
            lines = [f"这 {len(entries)} 项都没法{verb}："]
            lines.extend(f"  · {problem}" for problem in problems[:10])
            return self._reply("\n".join(lines), command)

        session.set_pending(
            PendingConfirmation(
                description=f"按清单批量{verb} {len(plan)} 项",
                action=f"batch_{command}",
                item_ids=[record.id for record, _quantity in plan],
                payload={
                    "items": [{"id": record.id, "quantity": quantity} for record, quantity in plan]
                },
            )
        )

        lines = [f"将按清单批量{verb} {len(plan)} 项："]
        for record, quantity in plan[:10]:
            spec = f"（{record.spec}）" if record.spec else ""
            target = max(record.quantity - quantity, 0.0) if command == "out" else quantity
            lines.append(
                f"  · {record.name}{spec}  {fmt_qty(record.quantity)} → {fmt_qty(target)}"
            )
        if len(plan) > 10:
            lines.append(f"  …… 还有 {len(plan) - 10} 项")
        if problems:
            lines.append("")
            lines.append(f"另有 {len(problems)} 项没法处理：")
            lines.extend(f"  · {problem}" for problem in problems[:5])
        lines.append("")
        lines.append("回复「确认」执行，或「取消」放弃。")
        return self._reply(
            "\n".join(lines), command, {"planned": len(plan), "skipped": len(problems)}
        )

    def _ask_dispose(self, session: Session) -> BotCommandResponse:
        """「把这些处理掉」没说清是清零还是删除 —— 直接问，别装作没听懂。"""
        records: list[Any] = []
        for item_id in session.last_item_ids:
            try:
                records.append(self.inventory.get_item(item_id))
            except NotFound:
                continue
        if not records:
            return self._reply(
                "我还不知道「这些」指哪些物品。\n"
                "先让我列一份清单（「库存」「查 电容」「采购 <清单>」都行），再说要怎么办。",
                "dispose",
            )

        label = session.last_label or "上一批物品"
        names = "、".join(record.name for record in records[:6])
        total = sum(record.quantity for record in records)
        session.set_pending(
            PendingConfirmation(
                description=f"处理{label}（{len(records)} 种物品）",
                action="dispose",
                item_ids=[record.id for record in records],
            )
        )
        return self._reply(
            f"「这些」指的是{label}的 {len(records)} 种物品"
            f"（{names}" + ("…" if len(records) > 6 else "") + f"），共 {fmt_qty(total)} 件。\n\n"
            "要我怎么处理？\n"
            "  ① 出库清零 —— 数量归零，**记录保留**（之后还能用「清理零库存」删）\n"
            "  ② 删除记录 —— 记录直接删掉，**无法恢复**\n\n"
            "回复「出库」或「删除」，也可以回「取消」。",
            "dispose",
            {"count": len(records)},
        )

    def _request_bulk_delete(self, session: Session, operator: str) -> BotCommandResponse:
        """把会话里记着的那批物品**整批删掉**（记录真的没了，需要确认）。

        与 :meth:`_request_bulk_out` 的区别：出库是「数量归零、记录保留」，
        这里是「记录消失」。用户说「删除」时才走这条。
        """
        ids = list(session.last_item_ids)
        if not ids:
            return self._reply(
                "我还不知道「这些」指哪些物品。\n"
                "先让我列一份清单（「库存」「查 电容」「采购 <清单>」都行），"
                "再说「把清单里的物品删掉」。",
                "delete",
            )

        records: list[Any] = []
        for item_id in ids:
            try:
                records.append(self.inventory.get_item(item_id))
            except NotFound:
                continue
        if not records:
            return self._reply("这批物品都已经不在了。", "delete")

        label = session.last_label or "上一批物品"
        session.set_pending(
            PendingConfirmation(
                description=f"删除{label}（{len(records)} 条记录）",
                action="delete_item",
                item_ids=[record.id for record in records],
                payload={"name": f"{len(records)} 条记录"},
            )
        )
        names = "、".join(record.name for record in records[:8])
        return self._reply(
            f"⚠️ 将删除{label}的 {len(records)} 条记录：\n"
            f"涉及：{names}" + ("…" if len(records) > 8 else "") + "\n\n"
            "记录会被**真的删掉**（不是清零），出入库流水保留但无法恢复。\n"
            "回复「确认」执行，或「取消」放弃。",
            "delete",
            {"count": len(records)},
        )

    def _request_bulk_out(
        self,
        scope: str,
        category: str | None,
        operator: str,
        scene: str,
        session: Session,
    ) -> BotCommandResponse:
        if scope == "last":
            ids = list(session.last_item_ids)
            if not ids:
                return self._reply(
                    "我还不知道「这些」指哪些物品。\n"
                    "先让我列一份清单（「库存」「查 电容」「采购 <清单>」都行），"
                    "再说「这些东西都用掉」。",
                    "out",
                )
            records = [self.inventory.get_item(item_id) for item_id in ids]
            label = session.last_label or "上一批物品"
        elif scope == "category" and category:
            # 先看是不是真实存在的分类；不是就当成关键词去搜物品 ——
            # 「全部电容」没有对应分类，但能搜到一批电容，这条老用法不能丢。
            if self._known_category(category) is not None:
                records = list(self.inventory.repo.all_items(category=category))
                label = f"「{category_label(category)}」分类下"
            else:
                # 破坏性操作必须**全量** —— 截断会让「全部出库」漏掉一部分
                records = self.inventory.search_all(category)
                if not records:
                    known = "、".join(
                        sorted(category_label(c) for c in self.inventory.repo.category_names())
                    )
                    return self._reply(
                        f"没有「{category}」这个分类，也没搜到叫这个名字的物品。\n"
                        f"现有分类：{known or '（还没有分类）'}\n"
                        "如果是想清空全部库存，请说「出库全部」。",
                        "out",
                    )
                label = f"匹配「{category}」的物品"
        else:
            records = list(self.inventory.repo.all_items())
            label = "全部库存"

        records = [r for r in records if r and r.quantity > 0]
        if not records:
            return self._reply(f"{label}没有需要出库的物品（数量都已是 0）。", "out")

        total = sum(r.quantity for r in records)
        pending = PendingConfirmation(
            description=f"整批出库 {label}（{len(records)} 种，共 {fmt_qty(total)} 件）",
            action="out",
            item_ids=[r.id for r in records],
        )
        session.set_pending(pending)

        names = "、".join(r.name for r in records[:8])
        lines = [
            f"将执行整批出库：{label}，{len(records)} 种物品，共 {fmt_qty(total)} 件，"
            "执行后数量全部归零。",
            f"涉及：{names}" + ("…" if len(records) > 8 else ""),
            "",
            "回复「确认」执行，或「取消」放弃。",
        ]
        return self._reply(
            "\n".join(lines),
            "out",
            {"pending": pending.description, "count": len(records), "total": total},
        )

    async def _execute_pending(
        self, session: Session, operator: str, scene: str
    ) -> BotCommandResponse:
        pending = session.pending
        assert pending is not None  # noqa: S101 - 调用点已判断
        session.clear_pending()

        if pending.action == "tidy":
            if self.ai is None:
                return self._reply("AI 整理不可用。", "confirm")
            plan = TidyPlan(**pending.payload.get("plan", {}))
            applied = self.ai.apply_tidy(plan, operator=operator)
            return self._reply(f"已按建议整理 {applied} 条物品。", "confirm", {"applied": applied})

        if pending.action in {"import_text", "import_file"}:
            if self.importer is None:
                return self._reply("导入功能不可用。", "confirm")
            rows = [ImportRow(**row) for row in pending.payload.get("rows") or []]
            if not rows:
                return self._reply("待导入的内容已经失效，请重新发送。", "confirm")
            preview = ImportPreviewOut(
                filename=pending.payload.get("filename") or "qq-import",
                fmt=pending.payload.get("fmt") or "markdown",
                rows=rows,
                total=len(rows),
            )
            try:
                result = self.importer.commit(
                    preview,
                    mode="merge",
                    operator=operator,
                    source="qq" if scene.startswith("qq") else "api",
                )
            except WarehouseError as exc:
                return self._reply(f"导入失败：{exc.message}", "confirm")
            return self._reply(result.message, "confirm", result.model_dump())

        if pending.action in {"batch_out", "batch_set"}:
            action = StockAction.OUT if pending.action == "batch_out" else StockAction.SET
            verb = "出库" if action is StockAction.OUT else "盘点"
            done = 0
            failed: list[str] = []
            for entry in pending.payload.get("items") or []:
                try:
                    self.inventory.change_stock(
                        StockChangeRequest(
                            action=action,
                            item_id=int(entry["id"]),
                            quantity=float(entry["quantity"]),
                            operator=operator,
                            source="qq" if scene.startswith("qq") else "api",
                            note=f"按清单批量{verb}",
                        )
                    )
                    done += 1
                except WarehouseError as exc:
                    failed.append(exc.message)
            lines = [f"已按清单批量{verb} {done} 项。"]
            if failed:
                lines.append("")
                lines.append(f"有 {len(failed)} 项失败：")
                lines.extend(f"  · {message}" for message in failed[:5])
            return self._reply("\n".join(lines), "confirm", {"done": done, "failed": len(failed)})

        if pending.action == "merge":
            lines = ["已合并："]
            groups_done = 0
            for group in pending.payload.get("groups") or []:
                try:
                    result = self.inventory.merge_items(
                        int(group["target"]),
                        [int(item) for item in group.get("sources") or []],
                        operator=operator,
                        source="qq" if scene.startswith("qq") else "api",
                    )
                except WarehouseError as exc:
                    lines.append(f"  · 合并失败：{exc.message}")
                    continue
                groups_done += 1
                lines.append(f"  · {result.message}")
            session.merge_groups = []          # 分组已经过时，清掉
            return self._reply("\n".join(lines), "confirm", {"groups": groups_done})

        if pending.action in {"delete_item", "delete_all"}:
            deleted = 0
            for item_id in pending.item_ids:
                try:
                    self.inventory.delete_item(item_id, operator=operator)
                    deleted += 1
                except NotFound:
                    continue
            session.set_items([])
            if pending.action == "delete_item":
                name = pending.payload.get("name", "")
                if len(pending.item_ids) > 1:
                    return self._reply(
                        f"已删除 {deleted} 条记录。", "confirm", {"deleted": deleted}
                    )
                return self._reply(
                    f"已删除「{name}」。" if deleted else "这条记录已经不在了。",
                    "confirm",
                    {"deleted": deleted},
                )
            return self._reply(f"已清空 {deleted} 条物品记录。", "confirm", {"deleted": deleted})

        if pending.action == "delete_zero":
            deleted = 0
            for item_id in pending.item_ids:
                try:
                    self.inventory.delete_item(item_id, operator=operator)
                    deleted += 1
                except NotFound:
                    continue
            session.set_items([])
            return self._reply(
                f"已删除 {deleted} 条零库存记录。出库流水仍保留在审计里。",
                "confirm",
                {"deleted": deleted},
            )

        if pending.action != "out":
            return self._reply("暂不支持该待确认操作。", "confirm")

        result = self.inventory.bulk_out(
            pending.item_ids,
            operator=operator,
            source="qq" if scene.startswith("qq") else "api",
            note=pending.description,
        )
        session.set_items([])
        return self._reply(
            f"已执行：{pending.description}\n"
            f"实际清零 {result['count']} 种，共 {fmt_qty(result['total'])} 件。",
            "confirm",
            result,
        )

    # ------------------------------------------------------------------ 其它
    def _alias(self, rest: str, operator: str, session: Session) -> BotCommandResponse:
        if not rest:
            return self._reply("用法：查看 `别名 <名称>`；添加 `别名 <名称> +<新别名>`", "alias")
        parts = re.split(r"[+＋]", rest, maxsplit=1)
        if len(parts) < 2:
            # 只给了名称 → **看**别名（文档承诺过，以前只回一句用法提示）
            return self._alias_listing(rest.strip(), session)
        target, alias_text = parts[0].strip(), parts[1].strip()
        if not target or not alias_text:
            return self._reply("用法：查看 `别名 <名称>`；添加 `别名 <名称> +<新别名>`", "alias")

        try:
            record, _, candidates, matched_by = self.inventory.resolve(target)
        except Ambiguous as exc:
            return self._ambiguous_reply(exc, "alias", session, label=target)

        if matched_by == "weak-type":
            record = None   # 只是同类，不能把名字记成别名

        if record is None:
            lines = [f"没找到「{target}」"]
            if candidates:
                lines.append("你是不是想说：")
                lines.extend(f"  · {c.name}" for c in candidates[:5])
            return self._reply("\n".join(lines), "alias")

        aliases = [piece.strip() for piece in re.split(r"[,，、/\s]+", alias_text) if piece.strip()]
        updated = self.inventory.add_aliases(record.id, aliases, operator=operator)
        return self._reply(
            f"已给「{updated.name}」加上别名：{'、'.join(aliases)}\n"
            f"当前别名：{'、'.join(updated.aliases) or '（无）'}",
            "alias",
        )

    def _alias_listing(self, target: str, session: Session) -> BotCommandResponse:
        """``别名 NE555`` —— 查看某个物品当前的别名。"""
        try:
            record, _, candidates, matched_by = self.inventory.resolve(target)
        except Ambiguous as exc:
            return self._ambiguous_reply(exc, "alias", session, label=target)

        if matched_by == "weak-type":
            record = None
        if record is None:
            lines = [f"没找到「{target}」"]
            if candidates:
                lines.append("你是不是想说：")
                lines.extend(f"  · {c.name}" for c in candidates[:5])
            return self._reply("\n".join(lines), "alias")

        aliases = "、".join(record.aliases) or "（还没有别名）"
        return self._reply(
            f"「{record.name}」的别名：{aliases}\n"
            f"要加别名就发「别名 {record.name} +新别名」",
            "alias",
            {"id": record.id, "aliases": list(record.aliases)},
        )

    async def _nlq(self, raw: str, session: Session, operator: str = "", scene: str = "api") -> BotCommandResponse:
        # 自然语言口吻的库存变更（「我把杜邦线用掉两卷」）先让大模型判一下，
        # 免得被当成搜索词落到「没找到」。
        if self._llm_ready() and (self.settings.qq_parse_mode or "auto").lower() != "rules":
            if _STOCK_INTENT.search(raw):
                ai_response = await self._try_ai_stock(raw, operator, scene, session)
                if ai_response is not None:
                    return ai_response

        response = await self.nlq.answer(raw)
        payload = dict(response.data)
        items = payload.get("items") or []

        # 规则没看懂（unknown，或按名称找但一条都没命中）→ 让大模型做模糊指令匹配。
        # 「清除全部」「手头还有多少东西」这类说法规则认不出来，但人一眼就懂。
        # 注意：low_stock / list 这类「空结果本身就是答案」的意图不在此列。
        if response.intent == "unknown" or (
            not items and response.intent in _AI_INTENT_FALLBACK_INTENTS
        ):
            ai_response = await self._try_ai_intent(raw, session, operator, scene)
            if ai_response is not None:
                return ai_response

        candidates = build_candidates_from_hits(items)
        if candidates:
            session.set_candidates(candidates, label=raw)
        return self._reply(
            response.answer,
            f"nlq:{response.intent}",
            {"intent": response.intent, **payload},
            handled=response.intent != "unknown",
        )

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _key(scene: str, conversation: str) -> str:
        return f"{scene or 'api'}:{conversation or 'default'}"

    @staticmethod
    def _reply(
        text: str, command: str, data: dict | None = None, *, handled: bool = True
    ) -> BotCommandResponse:
        return BotCommandResponse(reply=text, command=command, handled=handled, data=data or {})
