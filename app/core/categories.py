"""分类体系：**不预设任何内置分类**，完全由数据和 AI 决定。

分类从哪来：

* AI 归类时提出什么就新建什么（``AIService.analyze_preview`` / ``parse_stock`` / ``propose_tidy``）
* 用户手动建：``归类 <名称> <新分类>``
* 导入时的 Markdown 标题 / Excel 工作表名直接当分类名
* 用户纠正分类时的说法（「这是游戏卡」）直接当分类名

唯一的保留值是 :data:`DEFAULT_CATEGORY`（``未分类``）—— 还没被归类的东西先放这儿，
之后跑一次「整理」或重新导入就能让 AI 归好。
"""

from __future__ import annotations

import re

from .normalize import normalize_text

#: 还没被归类的记录放这里。除此之外**没有任何内置分类**。
DEFAULT_CATEGORY = "未分类"

#: 老版本用过的分类代码 → 现在的中文名。
#: 既用于一次性数据迁移（``app/db.py``），也保证没迁移的旧库仍能正常显示。
LEGACY_CATEGORY_CODES: dict[str, str] = {
    "stm_component": "STM元器件",
    "steam_card": "Steam游戏卡",
    "other": DEFAULT_CATEGORY,
}

#: 分类名允许的样子：中英文/数字/空格/常见分隔符，1~24 字符
_CODE_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff ·\-/（）()]{0,23}$")

#: 什么都能当分类名，但这几个词没有信息量，别拿它们建分类
_MEANINGLESS = frozenset({"什么", "啥", "东西", "物品", "这个", "那个", "它", "它们"})

#: 疑问句不是在纠正分类：「这是个什么东西」不该建出「个什么东西」
_QUESTION_WORDS = ("什么", "啥", "哪", "怎么", "多少", "吗", "呢", "？", "?")

#: 「这是 X」这类分类纠正
_CATEGORY_HINT_PATTERNS = (
    re.compile(r"(?:这|那)(?:是|属于|算)\s*(?P<name>[^\s，,。；;！!？?、]{1,12})"),
    re.compile(r"(?:应该|要|得)(?:算|归|放|分)(?:到|为|入|成)?\s*(?P<name>[^\s，,。；;！!？?、]{1,12})"),
)


def normalize_category_code(value: object, *, default: str = DEFAULT_CATEGORY) -> str:
    """把用户 / AI 给的分类写法规整成可存储的分类名。

    * 老分类代码 → 现在的中文名（``stm_component`` → ``STM元器件``）
    * 合法字符串 → 原样返回（**这就是「新分类自动创建」的入口**）
    * 空 / 不合法 → :data:`DEFAULT_CATEGORY`
    """
    raw = getattr(value, "value", value)
    text = re.sub(r"\s+", " ", normalize_text(str(raw or ""))).strip(" #-·")
    if not text:
        return default
    legacy = LEGACY_CATEGORY_CODES.get(text.casefold())
    if legacy:
        return legacy
    if not _CODE_RE.match(text):
        return default
    return text


def category_code(value: object) -> str:
    """``normalize_category_code`` 的历史别名。"""
    return normalize_category_code(value)


def category_label(code: object) -> str:
    """分类 → 展示名。

    分类名本身就是人话，所以基本原样返回；只对老代码做一次映射，
    这样没跑过迁移的旧库也不会把 ``stm_component`` 直接显示出来。
    """
    raw = getattr(code, "value", code)
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_CATEGORY
    return LEGACY_CATEGORY_CODES.get(text.casefold(), text)


def guess_category(name: str) -> str:
    """**不做任何内置归类** —— 「这个名字该归哪一类」交给 AI 判断。

    这里只保证「一定有个分类」，实际归类由
    ``AIService.analyze_preview``（导入）/ ``parse_stock``（入库）/ ``propose_tidy``（整理）决定。
    """
    return DEFAULT_CATEGORY


def category_from_heading(heading: object) -> str | None:
    """Markdown 标题 / Excel 工作表名直接当分类名：``## 耗材`` → ``耗材``。"""
    raw = str(heading or "").strip().strip("#").strip()
    if not raw or len(raw) > 24:
        return None
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", raw):
        return None
    candidate = normalize_category_code(raw)
    return None if candidate == DEFAULT_CATEGORY else candidate


def match_category(text: object) -> str:
    """用户写的分类说法 → 分类名。

    没有别名表了，原样规整即可。**是否真的存在**这个分类由调用方查库确认
    （``CommandRouter._known_category`` / ``_request_bulk_out``），
    这样「有个不存在的分类」会被如实告知，而不是静默当成某个内置分类。
    """
    return normalize_category_code(text)


def detect_category_hint(text: str) -> str | None:
    """识别「这没有封装啊，这是游戏卡」这类**分类纠正**。

    取「这是 X」里的 X 直接当分类名 —— 用户怎么叫就怎么建，不依赖预设词表。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return None
    if any(word in raw for word in _QUESTION_WORDS):
        return None
    for pattern in _CATEGORY_HINT_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        name = match.group("name").strip(" 的个些种，,。！!？?")
        if not name or name in _MEANINGLESS:
            continue
        candidate = normalize_category_code(name)
        if candidate != DEFAULT_CATEGORY:
            return candidate
    return None


__all__ = [
    "DEFAULT_CATEGORY",
    "LEGACY_CATEGORY_CODES",
    "category_code",
    "category_from_heading",
    "category_label",
    "detect_category_hint",
    "guess_category",
    "match_category",
    "normalize_category_code",
]
