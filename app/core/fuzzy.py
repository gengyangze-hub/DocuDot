"""分层模糊匹配。

匹配策略移植自 konamivrc6/component-inventory 的 ``_match_tags``：
分层递进、首个成功即返回，并且「确定为不等时不降级」，
避免 ``100nF`` 命中 ``100Ω``、``805`` 命中 ``0805`` 这类事故。

分层顺序（分数 → 含义）：

===== ==========================================================
1.0   整串归一化全等（名称 / 别名 / 规格）
1.0   物理量等价且两侧维度都明确（``0.1uF`` ≡ ``100nF``）
0.9   强类型归约相等（``电容`` ≡ ``C``）、介质/封装归约相等
0.7   物理量等价但维度靠 hint 补出、弱描述词归约
0.35~0.7  子串部分匹配（型号前缀、词界包含），按长度比打分
===== ==========================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .categories import DEFAULT_CATEGORY
from .categories import category_label as _category_label
from .categories import normalize_category_code
from .normalize import (
    TYPE_DIMENSION,
    Quantity,
    _has_word_boundary,
    canon_medium,
    canon_package,
    canon_type,
    canon_type_exact,
    classify_tags,
    extract_quantities,
    fold,
    normalize_text,
    parse_quantity,
    quantities_equal,
    tokenize_query,
)

# --------------------------------------------------------------------------- #
# 目标（一条库存记录的可检索面）
# --------------------------------------------------------------------------- #

#: 一级分类 → 便于人类检索的标签。
#: **不再有内置表** —— 分类名本身就是标签，所以 AI 新建的分类天然可检索。
#: ``未分类`` 不产出标签：它只表示「还没归类」，拿来检索只会污染结果。
def category_tags(category: str) -> tuple[str, ...]:
    """取某个分类的可检索标签；未分类不产出标签。"""
    label = _category_label(category)
    if not label or label == DEFAULT_CATEGORY:
        return ()
    return (label,)

_MULTI_LETTER_RE = re.compile(r"[A-Za-z]{2,}")


def _dedupe(values: Iterable[str], seen: set[str] | None = None) -> tuple[list[str], list[str]]:
    """按折叠键去重，返回 ``(原串列表, 折叠键列表)``。"""
    seen = set(seen or ())
    items: list[str] = []
    keys: list[str] = []
    for value in values:
        key = fold(value)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(value)
        keys.append(key)
    return items, keys


def _dedupe_quantities(values: Iterable[Quantity]) -> list[Quantity]:
    """按「维度 + 数值」去重，保留第一个（通常是置信度更高的那个）。"""
    result: list[Quantity] = []
    for quantity in values:
        if not quantity.dimension:
            continue
        if any(
            quantity.dimension == other.dimension and quantities_equal(quantity, other)
            for other in result
        ):
            continue
        result.append(quantity)
    return result


@dataclass
class MatchTarget:
    """被检索对象。``facet`` 是全部可检索字符串的并集。"""

    id: int
    name: str
    aliases: Sequence[str] = ()
    spec: str = ""
    category: str = "other"
    location: str = ""
    note: str = ""

    # 派生（``__post_init__`` 填充）
    #: 用户声明的可检索字段：名称 / 别名 / 分类标签 / 规格
    facets: list[str] = field(default_factory=list, init=False)
    folded_facets: list[str] = field(default_factory=list, init=False)
    #: 归约派生出的规范标签：类型码 / 介质 / 封装 / 安装方式 / 物理量原文。
    #: 它们**不参与「整串全等」层**，只用于归约层与子串层 ——
    #: 这样 ``SOP8`` 对 ``SOIC-8`` 才会得到「封装归约 0.9」而不是虚假的 1.0。
    derived_facets: list[str] = field(default_factory=list, init=False)
    all_facets: list[str] = field(default_factory=list, init=False)
    type_codes: set[str] = field(default_factory=set, init=False)
    medium: str | None = field(default=None, init=False)
    package: str | None = field(default=None, init=False)
    mount: str | None = field(default=None, init=False)
    dimension: str | None = field(default=None, init=False)
    #: 该条目携带的全部物理量（含从「100欧姆电阻」这类复合名称里抠出来的）
    quantities: list[Quantity] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.aliases = tuple(a for a in (self.aliases or ()) if a)
        self._build()

    def _build(self) -> None:
        category_tags_list = list(category_tags(self.category))

        # 把名称/规格/别名切成词，再统一归约成标签计划
        name_tokens = tokenize_query(self.name)
        spec_tokens = tokenize_query(self.spec)
        alias_tokens = [t for alias in self.aliases for t in tokenize_query(alias)]
        plan = classify_tags([*name_tokens, *spec_tokens, *alias_tokens])

        self.type_codes = set(plan.type_codes)
        self.medium = plan.medium
        self.package = plan.package
        self.mount = plan.mount

        # 物理量：分词得到的 + 从复合名称里抠出来的
        # （「100欧姆电阻」分词后是一个整体 token，parse_quantity 认不出来，必须单独抽）
        collected: list[Quantity] = list(plan.quantities)
        for candidate in (self.name, self.spec, *self.aliases):
            collected.extend(extract_quantities(candidate))
        self.quantities = _dedupe_quantities(collected)

        # 用户声明字段
        declared: list[str] = [self.name, *self.aliases, *category_tags_list]
        if self.spec:
            declared.append(self.spec)

        # 归约派生标签
        derived: list[str] = [quantity.raw for quantity in self.quantities if quantity.raw]
        if plan.medium:
            derived.append(plan.medium)
        if plan.package:
            derived.append(plan.package)
        if plan.mount:
            derived.append(plan.mount)
        derived.extend(sorted(self.type_codes))

        dimensions = [quantity.dimension for quantity in self.quantities if quantity.dimension]
        self.dimension = dimensions[0] if dimensions else None

        self.facets, self.folded_facets = _dedupe(declared)
        self.derived_facets, _ = _dedupe(derived, seen=set(self.folded_facets))
        self.all_facets = self.facets + self.derived_facets

    @property
    def searchable(self) -> str:
        return " ".join(self.all_facets)


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #

#: 子串层的最小长度（参考仓库：单字符子串禁用）
MIN_SUBSTRING_LEN = 2


def _is_word_bounded(haystack: str, needle: str) -> bool:
    """needle 是否以「词」的形式出现在 haystack 中。

    直接复用 :func:`app.core.normalize._has_word_boundary` ——
    这里原本另写了一份、且对非 ASCII 的 needle 直接放行，
    导致 ``100Ω`` 以 0.74 分命中 ``1100Ω``。两处逻辑必须只有一份。
    """
    return _has_word_boundary(haystack, needle)


def _substring_score(needle: str, haystack: str) -> float | None:
    """子串部分匹配打分。

    防护规则（沿用参考仓库思路）：

    * 短于 2 个字符不出分
    * 纯数字之间只认全等
    * **数字开头的 token 必须落在词界上** —— ``100nF`` 不得命中 ``1100nF``、
      ``805`` 不得命中 ``0805``
    * 型号类 token（字母开头）允许前缀/中段命中，但按长度比打折
    """
    a, b = fold(needle), fold(haystack)
    if len(a) < MIN_SUBSTRING_LEN or not b or a == b:
        return None

    # 整词命中：查询词恰好是目标里的一个独立词（``11P`` 之于「排母 11P」）。
    # 这是比「片段命中」强得多的证据，不该被长度比压到 0.7 以下 ——
    # 否则「排母 11P」这种多词名称永远够不到 0.9，采购比对会误判成「库存里没有」。
    needle_tokens = {token.casefold() for token in tokenize_query(needle) if token}
    haystack_tokens = {token.casefold() for token in tokenize_query(haystack) if token}
    if needle_tokens and needle_tokens <= haystack_tokens:
        return 0.92

    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short not in long:
        return None
    if short.isdigit() and long.isdigit():
        return None
    ratio = len(short) / len(long)
    if _is_word_bounded(long, short):
        return 0.50 + 0.30 * ratio
    if short[0].isdigit():
        # 数字开头却不在词界：判为不同物理量/不同封装，拒绝降级
        return None
    if long.startswith(short):
        return 0.50 + 0.30 * ratio  # 型号前缀（STM32F103 → STM32F103C8T6）
    return 0.35 + 0.25 * ratio  # 型号出现在中段


def match_token(query: str, target: MatchTarget) -> tuple[float, str | None]:
    """单个查询词对单个目标的匹配打分，返回 ``(分数, 依据)``。"""
    if not query:
        return 0.0, None
    query = normalize_text(query)
    if not query:
        return 0.0, None

    # ---- 第 1 层：整串全等 -------------------------------------------------
    folded = fold(query)
    if folded and folded in target.folded_facets:
        index = target.folded_facets.index(folded)
        return 1.0, f"完全匹配「{target.facets[index]}」"

    # ---- 第 2 层：物理量等价 ----------------------------------------------
    # 查询侧同时考虑「整串就是一个物理量」（100nF）和「复合名称里含物理量」
    # （100欧姆电阻 / 100Ω电阻）—— 这正是把两种写法认成同一个东西的关键。
    hint = target.dimension or _dimension_from_type(target)
    query_quantities: list[Quantity] = []
    direct = parse_quantity(query, hint=hint)
    if direct:
        query_quantities.append(direct)
    for extra in extract_quantities(query):
        if not any(
            extra.dimension == other.dimension and quantities_equal(extra, other)
            for other in query_quantities
        ):
            query_quantities.append(extra)

    if query_quantities and target.quantities:
        weak_score = 0.0
        weak_reason: str | None = None
        for quantity in query_quantities:
            if not quantity.dimension:
                continue
            for other in target.quantities:
                if quantity.dimension != other.dimension or not quantities_equal(quantity, other):
                    continue
                label = quantity.raw or query
                target_label = other.raw or other.dimension
                if quantity.confidence >= 1.0 and other.confidence >= 1.0:
                    return 1.0, f"{label} ≈ {target_label}"
                if weak_score < 0.7:
                    weak_score, weak_reason = 0.7, f"{label} ≈ {target_label}（同维度）"
        if weak_score:
            return weak_score, weak_reason
        if any(quantity.confidence >= 1.0 and quantity.dimension for quantity in query_quantities):
            # 查询是明确物理量却找不到等价标签 —— 直接判不等，不降级到子串
            return 0.0, None

    # ---- 第 3 层：强类型归约 ----------------------------------------------
    code = canon_type_exact(query)
    if code:
        if code in target.type_codes:
            return 0.9, f"{query} → 类型 {code}"
        return 0.0, None

    # ---- 第 4 层：介质 / 封装归约 ------------------------------------------
    medium = canon_medium(query)
    if medium and target.medium:
        return (0.9, f"{query} → 介质 {medium}") if medium == target.medium else (0.0, None)

    package = canon_package(query)
    if package and target.package and package == target.package:
        return 0.9, f"{query} → 封装 {package}"

    # ---- 第 5 层：字面出现在名称里 -----------------------------------------
    # 必须排在「弱类型」之前：``绿红`` 既是 LED 的颜色词（弱类型线索 0.7），
    # 又字面出现在「发光二极管 共阴 绿红」里（整词命中 0.92）。
    # 字面出现是强得多的证据，被弱类型盖掉会让多词名称整串掉到门槛以下。
    literal_score, literal_reason = _best_substring(query, target)
    if literal_score >= 0.9:
        return literal_score, literal_reason

    # ---- 第 6 层：弱类型回退（描述词） -------------------------------------
    # 分值必须**低于默认阈值**（fuzzy_threshold 默认 0.55）：
    # 「红茶」会命中「红色LED」的 LED 弱线索，0.7 分会让这种跨类型的结果
    # 直接出现在搜索结果里。弱线索只该在调用方显式放低门槛时才起作用。
    weak_code = canon_type(query)
    if weak_code and weak_code != code and weak_code in target.type_codes:
        return 0.45, f"{query} ≈ 类型 {weak_code}（弱证据）"

    # ---- 第 7 层：部分子串 -------------------------------------------------
    return literal_score, literal_reason


def _best_substring(query: str, target: MatchTarget) -> tuple[float, str | None]:
    """查询词在目标各个可检索面上的最佳子串得分。"""
    best_score = 0.0
    best_reason: str | None = None
    for facet in target.all_facets:
        score = _substring_score(query, facet)
        if score and score > best_score:
            best_score, best_reason = score, f"部分匹配「{facet}」"
    return best_score, best_reason


def _dimension_from_type(target: MatchTarget) -> str | None:
    for code in target.type_codes:
        if code in TYPE_DIMENSION:
            return TYPE_DIMENSION[code]
    return None


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #


@dataclass
class SearchHit:
    target_id: int
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)
    token_scores: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.target_id,
            "name": self.name,
            "score": round(self.score, 4),
            "reasons": self.reasons,
        }


def score_target(tokens: Sequence[str], target: MatchTarget, *, any_mode: bool = False) -> SearchHit | None:
    """对单个目标按全部查询词打分。默认 AND 语义：任一词不命中即淘汰。"""
    if not tokens:
        return None
    scores: list[float] = []
    reasons: list[str] = []
    for token in tokens:
        score, reason = match_token(token, target)
        if score <= 0:
            if not any_mode:
                return None
            scores.append(0.0)
            continue
        scores.append(score)
        if reason:
            reasons.append(f"{token}: {reason}")
    hits = [s for s in scores if s > 0]
    if not hits:
        return None
    combined = 0.7 * (sum(scores) / len(scores)) + 0.3 * min(scores)
    if any_mode:
        combined *= len(hits) / len(scores)
    return SearchHit(target_id=target.id, name=target.name, score=combined, reasons=reasons, token_scores=scores)


def search_targets(
    query: str,
    targets: Iterable[MatchTarget],
    *,
    threshold: float = 0.0,
    limit: int = 10,
    any_mode: bool = False,
) -> list[SearchHit]:
    """检索并按分数降序返回。``threshold <= 0`` 表示不做过滤。"""
    tokens = tokenize_query(query)
    if not tokens:
        return []
    hits: list[SearchHit] = []
    for target in targets:
        hit = score_target(tokens, target, any_mode=any_mode)
        if hit and hit.score > threshold:
            hits.append(hit)
    hits.sort(key=lambda h: (-h.score, h.target_id))
    return hits[:limit] if limit and limit > 0 else hits


def best_match(
    query: str,
    targets: Sequence[MatchTarget],
    *,
    threshold: float = 0.6,
    ambiguity_gap: float = 0.05,
) -> tuple[MatchTarget | None, float, list[SearchHit]]:
    """为「入库/出库」这类写操作解析唯一目标。

    返回 ``(命中的目标, 分数, 全部候选)``；若前两名分数过于接近，
    则把目标置为 ``None`` 让调用方要求用户消歧。
    """
    hits = search_targets(query, targets, threshold=threshold, limit=5)
    if not hits:
        return None, 0.0, []
    top = hits[0]
    if len(hits) > 1 and (top.score - hits[1].score) < ambiguity_gap:
        return None, top.score, hits
    target = next((t for t in targets if t.id == top.target_id), None)
    return target, top.score, hits


def explain(query: str, target: MatchTarget) -> dict:
    """调试辅助：展示每个查询词的分层命中情况。"""
    details = []
    for token in tokenize_query(query):
        score, reason = match_token(token, target)
        details.append({"token": token, "score": round(score, 4), "reason": reason})
    return {"target_id": target.id, "name": target.name, "tokens": details}
