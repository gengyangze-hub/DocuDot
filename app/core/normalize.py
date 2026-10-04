"""文本 / 物理量归一化。

语义移植自公开仓库 konamivrc6/component-inventory（标签式电子元件库存 CLI），
并按本项目的「名称 + 别名 + 规格 + 分类」模型做了适配：

* :func:`normalize_text`  —— NFKC 全角转半角、``µ``/``μ`` → ``u``、``Ω``(U+2126) → ``Ω``(U+03A9)
* :func:`parse_quantity`  —— SI 前缀 + 单位后缀 + 中缀小数（``4R7`` / ``1k2`` / ``0R05``）
* :func:`quantities_equal`—— 维度闸门 + 相对误差
* :func:`canon_type` / :func:`canon_medium` / :func:`canon_package` / :func:`canon_unit`

相对参考仓库的扩展（参考仓库明确不解析的部分）：

* EIA 三位码 ``104`` → ``100nF``，仅在维度 hint 明确为电容时启用，避免误伤型号。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

# --------------------------------------------------------------------------- #
# 维度
# --------------------------------------------------------------------------- #

DIM_RESISTANCE = "resistance"
DIM_CAPACITANCE = "capacitance"
DIM_INDUCTANCE = "inductance"
DIM_FREQUENCY = "frequency"
DIM_VOLTAGE = "voltage"
DIM_CURRENT = "current"
DIM_POWER = "power"

DIMENSIONS = (
    DIM_RESISTANCE,
    DIM_CAPACITANCE,
    DIM_INDUCTANCE,
    DIM_FREQUENCY,
    DIM_VOLTAGE,
    DIM_CURRENT,
    DIM_POWER,
)

#: 单位后缀 → 维度。**必须按长度降序匹配**（``hz`` 先于 ``h``、``ohms`` 先于 ``ohm``）。
UNIT_SUFFIXES: tuple[tuple[str, str], ...] = (
    # 电阻
    ("ohms", DIM_RESISTANCE),
    ("ohm", DIM_RESISTANCE),
    ("欧姆", DIM_RESISTANCE),
    ("Ω", DIM_RESISTANCE),
    ("欧", DIM_RESISTANCE),
    ("r", DIM_RESISTANCE),
    # 电容
    ("farad", DIM_CAPACITANCE),
    ("法拉", DIM_CAPACITANCE),
    ("法", DIM_CAPACITANCE),
    ("f", DIM_CAPACITANCE),
    # 电感
    ("henry", DIM_INDUCTANCE),
    ("亨利", DIM_INDUCTANCE),
    ("亨", DIM_INDUCTANCE),
    ("h", DIM_INDUCTANCE),
    # 频率
    ("hertz", DIM_FREQUENCY),
    ("赫兹", DIM_FREQUENCY),
    ("赫", DIM_FREQUENCY),
    ("hz", DIM_FREQUENCY),
    # 电压
    ("volt", DIM_VOLTAGE),
    ("伏特", DIM_VOLTAGE),
    ("伏", DIM_VOLTAGE),
    ("v", DIM_VOLTAGE),
    # 电流
    ("ampere", DIM_CURRENT),
    ("amps", DIM_CURRENT),
    ("amp", DIM_CURRENT),
    ("安培", DIM_CURRENT),
    ("安", DIM_CURRENT),
    ("a", DIM_CURRENT),
    # 功率
    ("watt", DIM_POWER),
    ("瓦特", DIM_POWER),
    ("瓦", DIM_POWER),
    ("w", DIM_POWER),
)

#: SI 前缀倍率。``m`` 与 ``M`` **永不互兜**（毫 vs 兆）。
SI_PREFIX: dict[str, float] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}

#: 中缀小数允许的字符（``4R7``、``1k2``、``0R05``、``4u7``）。
_INFIX_CHARS = "RrKkMmGgUuNnPp"

_INFIX_RE = re.compile(rf"^(?P<a>\d*)(?P<mid>[{_INFIX_CHARS}])(?P<b>\d+)(?P<rest>.*)$")
_PLAIN_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<rest>.*)$")

#: 形如 ``1N4148`` / ``1N4007`` 的常见二极管型号，禁止当成物理量。
_MODEL_RE = re.compile(r"^1N\d{3,}[A-Z]*$")
#: 形如 ``0805`` / ``0603`` 的封装码（前导零）禁止当成电阻值。
_LEADING_ZERO_RE = re.compile(r"^0\d{2,}$")
#: 两段以上连续字母 —— 型号特征（``LQFP48`` 不会被解析成物理量）。
_MULTI_LETTER_RE = re.compile(r"[A-Za-z]{2,}")


def normalize_text(value: str) -> str:
    """全角转半角、统一微符号与欧姆符号、压缩空白。不做 casefold。

    刻意不折叠大小写：``M``（兆）与 ``m``（毫）语义完全不同。
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00b5", "u").replace("\u03bc", "u")  # µ / μ → u
    text = text.replace("\u2126", "\u03a9")  # Ω ohm sign → Ω greek capital omega
    text = text.replace("\u3000", " ")  # 全角空格
    return re.sub(r"\s+", " ", text).strip()


def fold(value: str) -> str:
    """用于「精确相等」比较的折叠键：归一化 + 转小写 + 去掉常见分隔符。

    注意：**不要**用这个结果做物理量比较，``m``/``M`` 会被折叠掉。
    """
    text = normalize_text(value).casefold()
    return re.sub(r"[\s\-_/·・]+", "", text)


# --------------------------------------------------------------------------- #
# 物理量
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Quantity:
    """一个带维度的物理量。

    * ``confidence == 1.0``：token 自身明确了维度（``0.1uF``、``51Ω``）
    * ``confidence == 0.7``：维度由 hint 补出（``100n`` + 电容 hint）
    """

    value: float
    dimension: str | None
    confidence: float = 1.0
    raw: str = ""

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Quantity({self.value:g}, {self.dimension}, {self.confidence})"


def _looks_like_model(token: str) -> bool:
    """型号拦截：``1N4148``、``LQFP48``、``STM32F103`` 等不是物理量。"""
    upper = token.upper()
    if _MODEL_RE.match(upper):
        return True
    if _LEADING_ZERO_RE.match(token):
        return True
    # 去掉末尾可能存在的单位后缀后，若仍有连续 2 个以上字母，判为型号
    body = _strip_unit_suffix(token)[0]
    return bool(_MULTI_LETTER_RE.search(body))


#: 单位后缀的大小写折叠表。必须用 casefold 而不是 lower：
#: ``"Ω".lower()`` 得到的是 ``"ω"``（U+03C9），直接比较会永远失败。
_UNIT_SUFFIX_FOLDED: tuple[tuple[str, str], ...] = tuple(
    sorted(((suffix.casefold(), dim) for suffix, dim in UNIT_SUFFIXES), key=lambda kv: -len(kv[0]))
)


def _strip_unit_suffix(token: str) -> tuple[str, str | None]:
    """剥离单位后缀，返回 ``(剩余部分, 维度)``。后缀按长度降序尝试。

    注意 ``>=``：单位后缀可能**就是整个 token**（``parse_quantity`` 已经
    先把数字部分切走了，剩下的 ``"r"`` / ``"v"`` 仍需被识别）。
    """
    folded = token.casefold()
    for suffix, dim in _UNIT_SUFFIX_FOLDED:
        if folded.endswith(suffix) and len(token) >= len(suffix):
            return token[: len(token) - len(suffix)], dim
    return token, None


def _split_prefix(body: str) -> tuple[float, str]:
    """剥离 SI 前缀，返回 ``(倍率, 剩余数字串)``。"""
    if body and body[0] in SI_PREFIX and len(body) > 1 and body[1].isdigit():
        return SI_PREFIX[body[0]], body[1:]
    return 1.0, body


def parse_quantity(token: str, hint: str | None = None, *, allow_eia: bool = True) -> Quantity | None:
    """把 token 解析成物理量。

    >>> parse_quantity("0.1uF").value == 1e-7
    True
    >>> parse_quantity("100nF").value == 1e-7
    True
    >>> parse_quantity("4R7").value
    4.7
    >>> parse_quantity("1k2").value
    1200.0
    >>> parse_quantity("STM32F103") is None
    True
    """
    if not token:
        return None
    text = normalize_text(token).replace(" ", "")
    if not text or _looks_like_model(text):
        return None

    # ---- 1. 中缀小数：4R7 / 1k2 / 0R05 / 2M2 / 4u7 --------------------------
    m = _INFIX_RE.match(text)
    if m:
        a, mid, b, rest = m.group("a"), m.group("mid"), m.group("b"), m.group("rest")
        rest_value, rest_dim = _strip_unit_suffix(rest) if rest else ("", None)
        if rest and rest_value:
            # 中缀后面还跟着别的东西（例如 `1k2abc`），不是干净的物理量
            return None
        if mid in "Rr":
            value = float(f"{a or '0'}.{b}")
            dimension = DIM_RESISTANCE
        else:
            value = float(a or "0") + int(b) / (10 ** len(b))
            value *= SI_PREFIX[mid]
            dimension = rest_dim
        confidence = 1.0 if dimension else 0.7
        if dimension is None:
            dimension = hint
            confidence = 0.7 if hint else 1.0
        return Quantity(value=value, dimension=dimension, confidence=confidence, raw=token)

    # ---- 2. 常规形式：100 / 100nF / 51Ω / 16V ------------------------------
    m = _PLAIN_RE.match(text)
    if not m:
        return None
    number = float(m.group("num"))
    rest = m.group("rest")

    if not rest:
        # 纯数字：仅当 hint 存在，或命中 EIA 三位码时才成立
        if hint and allow_eia and _is_eia_code(text):
            return Quantity(value=_eia_to_farad(text), dimension=DIM_CAPACITANCE, confidence=0.7, raw=token)
        if hint:
            return Quantity(value=number, dimension=hint, confidence=0.7, raw=token)
        return None

    body, dimension = _strip_unit_suffix(rest)
    if dimension is None:
        # rest 以 SI 前缀开头（`100n`、`4u7` 的另一种写法）
        if body and body[0] in SI_PREFIX and len(body) == 1:
            if not hint:
                return Quantity(value=number * SI_PREFIX[body[0]], dimension=None, confidence=0.7, raw=token)
            return Quantity(
                value=number * SI_PREFIX[body[0]], dimension=hint, confidence=0.7, raw=token
            )
        return None
    if body:
        # 例如 `10kR`：前缀在维度之内
        if len(body) == 1 and body[0] in SI_PREFIX:
            number *= SI_PREFIX[body[0]]
        else:
            return None
    return Quantity(value=number, dimension=dimension, confidence=1.0, raw=token)


def _is_eia_code(text: str) -> bool:
    """EIA 三位码：``104`` / ``473``。首位非 0，恰好三位数字。"""
    return len(text) == 3 and text.isdigit() and text[0] != "0"


def _eia_to_farad(text: str) -> float:
    """``104`` → 10 × 10⁴ pF = 100 nF。"""
    return int(text[:2]) * (10 ** int(text[2])) * 1e-12


#: 复合名称里的物理量：``100欧姆电阻``、``100Ω电阻``、``100kΩ``、``16MHz``。
#: 注意 ``hertz/hz`` 必须排在单字符分支之前，否则 ``16MHz`` 会被吃成 16 兆亨。
_EMBEDDED_QUANTITY_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*"
    r"(?:ohms|ohm|欧姆|Ω|欧|farad|法拉|法|henry|亨利|亨|hertz|赫兹|赫|"
    r"volt|伏特|伏|ampere|amps|amp|安培|安|watt|瓦特|瓦|"
    r"[pnumkKMGT]?hz|[pnumkKMGT]?[fFvVaAwWhHrRΩ])",
    re.IGNORECASE,
)


def extract_quantities(text: str) -> list[Quantity]:
    """从复合名称里抠出物理量。

    ``100欧姆电阻`` 和 ``100Ω电阻`` 描述的是同一个东西，
    但字符串完全不同 —— 必须先各自解析出「100 欧姆」才有机会判等。

    >>> [q.value for q in extract_quantities("100欧姆电阻")]
    [100.0]
    >>> quantities_equal(extract_quantities("100欧姆电阻")[0], extract_quantities("100Ω电阻")[0])
    True
    """
    if not text:
        return []
    found: list[Quantity] = []
    for match in _EMBEDDED_QUANTITY_RE.finditer(normalize_text(text)):
        quantity = parse_quantity(match.group(0).strip())
        if not quantity or not quantity.dimension:
            continue
        if any(
            quantity.dimension == other.dimension and quantities_equal(quantity, other) for other in found
        ):
            continue
        found.append(quantity)
    return found


def quantities_equal(a: Quantity, b: Quantity, rel_tol: float = 1e-9) -> bool:
    """带维度闸门的数值比较。"""
    if a.dimension and b.dimension and a.dimension != b.dimension:
        return False
    if a.value == 0 and b.value == 0:
        return True
    return math.isclose(a.value, b.value, rel_tol=rel_tol, abs_tol=0.0)


#: 类型码 → 默认物理量维度（用于给纯数字 token 补维度，如 ``100`` + ``C`` → 100pF? 否 —— 只补维度不补量级）
TYPE_DIMENSION: dict[str, str] = {
    "R": DIM_RESISTANCE,
    "C": DIM_CAPACITANCE,
    "L": DIM_INDUCTANCE,
    "XTAL": DIM_FREQUENCY,
}


#: 物理量维度 → 类型码。被动元件只看「数值 + 单位」就能定类型（``10kΩ`` → R），
#: 这样 ``查 电阻`` / ``查 R`` 也能命中名称里只写了单位、没写「电阻」的条目。
#: 刻意**不含 frequency** —— ``72MHz`` 可能只是芯片主频，不能据此判定是晶振。
DIMENSION_TYPE: dict[str, str] = {
    DIM_RESISTANCE: "R",
    DIM_CAPACITANCE: "C",
    DIM_INDUCTANCE: "L",
}


def infer_dimension(tags: Iterable[str]) -> str | None:
    """从一组标签推断物理量维度。

    优先取「已经带明确单位的标签」的维度，其次由类型码兜底。
    """
    saw_number_without_dimension = False
    for tag in tags:
        quantity = parse_quantity(tag)
        if quantity and quantity.dimension:
            return quantity.dimension
        if quantity and quantity.dimension is None:
            saw_number_without_dimension = True
    for tag in tags:
        code = canon_type_exact(tag)
        if code and code in TYPE_DIMENSION:
            return TYPE_DIMENSION[code]
    return DIM_CAPACITANCE if saw_number_without_dimension and any(
        canon_type_exact(t) == "C" for t in tags
    ) else None


# --------------------------------------------------------------------------- #
# 类别 / 介质 / 封装 / 单位归约
# --------------------------------------------------------------------------- #

#: 元器件类型码 → 别名。移植自参考仓库的 TYPE_ALIASES（19 个类型码）。
TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "R": ("电阻", "电阻器", "欧姆", "res", "resistor"),
    "C": ("电容", "电容器", "cap", "capacitor"),
    "L": ("电感", "电感器", "ind", "inductor"),
    "D": ("二极管", "diode", "肖特基", "整流管", "稳压管", "齐纳"),
    "Q": ("三极管", "晶体管", "transistor", "bjt", "mos", "场效应管", "mosfet"),
    "U": ("芯片", "ic", "集成电路", "运放", "mcu", "单片机", "稳压器"),
    "J": (
        "连接器", "接插件", "connector", "插座", "排母", "排针", "端子", "header",
        # 口语同义词（实测「插排」认不出来）
        "插排", "插针", "针座", "母座", "公座", "接线端子",
    ),
    "SW": ("开关", "switch", "按键", "轻触开关", "按钮", "拨动开关"),
    "XTAL": ("晶振", "晶体", "谐振器", "crystal", "resonator"),
    "LED": ("发光二极管", "led", "指示灯", "灯珠"),
    "OPTO": ("光耦", "光电耦合器", "optocoupler"),
    "FUSE": ("保险丝", "熔断器", "fuse", "自恢复保险丝"),
    "POT": ("电位器", "可调电阻", "potentiometer", "trimpot", "微调电阻"),
    "RELAY": ("继电器", "relay"),
    "BZ": ("蜂鸣器", "buzzer", "扬声器", "喇叭"),
    "ANT": ("天线", "antenna"),
    "BAT": ("电池", "电池座", "battery"),
    "TP": ("测试点", "testpoint"),
    "X": ("跳线", "跳帽", "短接"),
}

#: 电容介质别名。
MEDIUM_ALIASES: dict[str, tuple[str, ...]] = {
    "MLCC": ("mlcc", "陶瓷", "瓷片", "陶瓷电容", "瓷片电容", "独石", "片式陶瓷"),
    "electrolytic": ("电解", "铝电解", "电解电容", "铝电解电容", "铝电解电容器"),
    "tantalum": ("钽", "钽电容", "钽电解"),
    "film": ("薄膜", "薄膜电容", "涤纶", "聚酯", "涤纶电容"),
    "mica": ("云母", "云母电容"),
}

#: 弱证据描述词 → 类型码。只在没有更强证据时用于补全类型。
DESCRIPTOR_TYPE: dict[str, str] = {
    "色环": "R",
    "金属膜": "R",
    "碳膜": "R",
    "水泥": "R",
    "工字": "L",
    "磁环": "L",
    "磁珠": "L",
    "轻触": "SW",
    "拨动": "SW",
    "船型": "SW",
    "微动": "SW",
    "钮子": "SW",
    "牛角": "J",
    "杜邦": "J",
    "香蕉": "J",
    "微调": "POT",
    "纽扣": "BAT",
    "锂电": "BAT",
    "红": "LED",
    "绿": "LED",
    "蓝": "LED",
    "黄": "LED",
    "白": "LED",
    "橙": "LED",
    "紫": "LED",
    "暖白": "LED",
    "冷白": "LED",
    "rgb": "LED",
    "三色": "LED",
    "七彩": "LED",
}

#: 型号前缀 → 类型码。用于「搜『电容』能找到 C」的同类能力：
#: 光看 ``STM32F103C8T6`` 这个型号名，就能知道它是 MCU（U）。
MODEL_TYPE_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:STM|GD|APM|CH|HC|AT|ESP|RP|NRF|PIC|TMS|MSP|N76|BL|W25Q|W25X)\d", re.IGNORECASE), "U"),
    (re.compile(r"^(?:LM|AMS|MP|TPS|XC|RT|SY|MT)\d{3,}", re.IGNORECASE), "U"),
    (re.compile(r"^NE555", re.IGNORECASE), "U"),
    (re.compile(r"^(?:1N|SS|SR|MUR|ES1|US1|BAT)\d", re.IGNORECASE), "D"),
    (re.compile(r"^(?:2N|BC|BD|IRF|IRL|AO|SI|MMBT)\d", re.IGNORECASE), "Q"),
)

#: 贴片封装（SMD）
SMD_PACKAGES = frozenset(
    {
        "0201", "0402", "0603", "0805", "1206", "1210", "1218", "2010", "2512",
        "sot23", "sot223", "sot89", "sod123", "sod323", "sod523",
        "sma", "smb", "smc", "do214", "do214ab", "do214ac",
        "qfn", "dfn", "lqfp", "tqfp", "qfp", "soic", "sop", "sot", "msop", "tssop",
        "bga", "csp", "smd", "chip",
    }
)

#: 直插封装（THT）
THT_PACKAGES = frozenset(
    {
        "dip", "pdip", "to220", "to92", "to247", "to3", "axial", "radial",
        "tht", "throughhole", "插件",
    }
)

_TYPE_LOOKUP: dict[str, str] = {
    alias.casefold(): code for code, aliases in TYPE_ALIASES.items() for alias in aliases
}
_TYPE_LOOKUP.update({code.casefold(): code for code in TYPE_ALIASES})
_MEDIUM_LOOKUP: dict[str, str] = {
    alias.casefold(): name for name, aliases in MEDIUM_ALIASES.items() for alias in aliases
}
_MEDIUM_LOOKUP.update({name.casefold(): name for name in MEDIUM_ALIASES})


def _has_word_boundary(haystack: str, needle: str) -> bool:
    """ASCII 子串要求词边界，避免 ``100nF`` 命中 ``1100nF``。

    非 ASCII 的 token（``100Ω``）不能整个跳过检查 ——
    那样它就会以 0.74 分命中 ``1100Ω``。改为**只看紧邻的字符**：
    首字符是 ASCII 就要求左边界，末字符是 ASCII 就要求右边界。
    """
    if not needle:
        return False
    prefix = r"(?<![0-9A-Za-z])" if needle[0].isascii() else ""
    suffix = r"(?![0-9A-Za-z])" if needle[-1].isascii() else ""
    if not prefix and not suffix:
        return needle in haystack
    pattern = rf"{prefix}{re.escape(needle)}{suffix}"
    return re.search(pattern, haystack, re.IGNORECASE) is not None


def canon_type_exact(token: str) -> str | None:
    """**强证据**归约：整串恰好等于类型码、类型别名或电容介质名。

    >>> canon_type_exact("电容"), canon_type_exact("Capacitor"), canon_type_exact("MLCC")
    ('C', 'C', 'C')
    >>> canon_type_exact("白卡") is None
    True
    """
    if not token:
        return None
    folded = normalize_text(token).strip().casefold()
    if not folded:
        return None
    if folded in _TYPE_LOOKUP:
        return _TYPE_LOOKUP[folded]
    medium = _MEDIUM_LOOKUP.get(folded)
    if medium:
        return "C"
    return None


def infer_type_from_model(token: str) -> str | None:
    """由型号前缀推断类型码（``STM32F103C8T6`` → ``U``、``1N4148`` → ``D``）。"""
    if not token:
        return None
    text = normalize_text(token).strip()
    for pattern, code in MODEL_TYPE_HINTS:
        if pattern.match(text):
            return code
    return None


def canon_type(token: str) -> str | None:
    """归约到类型码，含子串与描述词回退。

    ⚠️ 回退路径属**弱证据**（例如 ``白卡`` 会因描述词 ``白`` 归约成 ``LED``），
    调用方应据此降低匹配置信度，不要等同于 :func:`canon_type_exact`。
    """
    exact = canon_type_exact(token)
    if exact:
        return exact
    if not token:
        return None
    text = normalize_text(token).strip()
    folded = text.casefold()
    # 子串回退：别名至少 2 字符；ASCII 需要词边界
    for alias, code in _TYPE_LOOKUP.items():
        if len(alias) >= 2 and alias in folded and _has_word_boundary(text, alias):
            return code
    for descriptor, code in DESCRIPTOR_TYPE.items():
        if descriptor in folded:
            return code
    return None


def canon_medium(token: str) -> str | None:
    """归约电容介质。"""
    if not token:
        return None
    text = normalize_text(token).strip()
    folded = text.casefold()
    if folded in _MEDIUM_LOOKUP:
        return _MEDIUM_LOOKUP[folded]
    for alias, name in _MEDIUM_LOOKUP.items():
        if len(alias) >= 2 and alias in folded:
            return name
    return None


def canon_package(token: str) -> str | None:
    """封装归约：去掉分隔符与 ``mm`` 后缀、统一大写、合并常见别名。

    >>> canon_package("5x11mm"), canon_package("SOP-8"), canon_package("lqfp-48")
    ('5X11', 'SOP8', 'LQFP48')
    """
    if not token:
        return None
    text = normalize_text(token).strip()
    if not text:
        return None
    text = re.sub(r"(?i)(?:mm|毫米)$", "", text)
    text = re.sub(r"[\s\-_/]+", "", text)
    text = text.upper()
    if not text:
        return None
    # 统一常见同义写法
    for prefix, canonical in (
        ("SOIC", "SOP"),
        ("TSSOP", "TSSOP"),
        ("PDIP", "DIP"),
        ("TO-", "TO"),
    ):
        if text.startswith(prefix.replace("-", "")):
            text = canonical + text[len(prefix.replace("-", "")) :]
            break
    return text or None


def package_mount(package: str | None) -> str | None:
    """由封装推断安装方式：``smd``（贴片）/ ``tht``（直插）。"""
    if not package:
        return None
    key = re.sub(r"[^a-z0-9]", "", package.casefold())
    if not key:
        return None
    for known in SMD_PACKAGES:
        if key.startswith(known):
            return "smd"
    for known in THT_PACKAGES:
        if key.startswith(known):
            return "tht"
    return None


def canon_unit(token: str) -> str:
    """把「数字 + ASCII 单位简写」写成规范符号。

    仅处理不歧义的写法：``51r`` / ``51ohm`` → ``51Ω``、``16v`` → ``16V``、``0.25w`` → ``0.25W``。
    **不会**把 ``0.1uF`` 改写成 ``100nF``（浮点往返不做）。
    """
    if not token:
        return token
    text = normalize_text(token).strip()
    m = re.match(r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<rest>[A-Za-zΩ]+)$", text)
    if not m:
        return text
    num, rest = m.group("num"), m.group("rest")
    lowered = rest.casefold()
    if lowered in {"r", "ohm", "ohms"}:
        return f"{num}Ω"
    if lowered in {"v", "volt", "volts"}:
        return f"{num}V"
    if lowered in {"a", "amp", "amps"}:
        return f"{num}A"
    if lowered in {"w", "watt", "watts"}:
        return f"{num}W"
    if lowered == "f":
        return f"{num}F"
    if lowered == "h":
        return f"{num}H"
    return text


@dataclass
class TagPlan:
    """一组标签的归约结果。"""

    raw: list[str] = field(default_factory=list)
    canonical: list[str] = field(default_factory=list)
    type_code: str | None = None
    #: **全部**识别出的类型码（``type_code`` 只留第一个，用作物理量提示）。
    #: 「开关 保险丝」这种名字里有两个类型词，只留第一个的话，
    #: 第二个类型的查询会在 fuzzy 第 3 层被直接判 0.0 —— 明明字面就在名称里。
    type_codes: set[str] = field(default_factory=set)
    medium: str | None = None
    package: str | None = None
    mount: str | None = None
    quantities: list[Quantity] = field(default_factory=list)
    descriptors: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def classify_tags(tags: Sequence[str]) -> TagPlan:
    """把一组标签拆成「类型 / 介质 / 封装 / 物理量 / 描述词 / 未知」。"""
    plan = TagPlan(raw=list(tags))
    for tag in tags:
        if not tag:
            continue
        medium = canon_medium(tag)
        if medium:
            plan.medium = plan.medium or medium
            if medium in {"MLCC", "electrolytic", "tantalum", "film", "mica"}:
                plan.type_code = plan.type_code or "C"
            continue
        package = canon_package(tag)
        if package and re.fullmatch(r"[A-Z0-9]{2,12}", package or ""):
            mount = package_mount(package)
            if mount:
                plan.package = plan.package or package
                plan.mount = plan.mount or mount
                continue
        code = canon_type(tag)
        if code:
            plan.type_code = plan.type_code or code
            continue
        quantity = parse_quantity(tag, hint=plan.type_code and (
            "capacitance" if plan.type_code == "C" else "resistance" if plan.type_code == "R" else None
        ))
        if quantity:
            plan.quantities.append(quantity)
            continue
        for descriptor, code in DESCRIPTOR_TYPE.items():
            if descriptor in tag.casefold():
                plan.descriptors.append(descriptor)
                plan.type_code = plan.type_code or code
                break
        else:
            model_code = infer_type_from_model(tag)
            if model_code:
                plan.type_code = plan.type_code or model_code
                plan.canonical.append(tag)
            else:
                plan.unknown.append(tag)

    # 兜底：类型没被任何 token 直接标明时，由物理量维度补出。
    # 「10kΩ 0805」里没有任何「电阻 / R」字样，但 Ω 已经说明它是电阻 ——
    # 不补这一步，查「电阻」或「R」就找不到它。
    if plan.type_code is None:
        for quantity in plan.quantities:
            code = DIMENSION_TYPE.get(quantity.dimension or "")
            if code:
                plan.type_code = code
                break

    # 收集**全部**类型码：一个名称里可能同时出现多个类型词。
    for tag in tags:
        if not tag:
            continue
        code = canon_type_exact(tag)
        if code:
            plan.type_codes.add(code)
    if plan.type_code:
        plan.type_codes.add(plan.type_code)
    return plan


# --------------------------------------------------------------------------- #
# 查询分词
# --------------------------------------------------------------------------- #

#: 视为分隔符的字符（参考仓库刻意不切逗号；本服务面向 QQ 自然语言输入，改为切分）
_SEPARATORS = re.compile(r"[\s,，、;；/|]+")
_TRIM = "。！？!?：:（）()【】[]{}“”\"'`*#@+~"


def tokenize_query(query: str) -> list[str]:
    """把用户输入切成检索词。

    >>> tokenize_query("查 STM32F103  0.1uF")
    ['查', 'STM32F103', '0.1uF']
    """
    if not query:
        return []
    text = normalize_text(query)
    tokens: list[str] = []
    for chunk in _SEPARATORS.split(text):
        token = chunk.strip(_TRIM)
        if token:
            tokens.append(token)
    return tokens


def iter_quantity_like(tokens: Iterable[str], hint: str | None = None) -> Iterator[Quantity]:
    for token in tokens:
        quantity = parse_quantity(token, hint=hint)
        if quantity:
            yield quantity
