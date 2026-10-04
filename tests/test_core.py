"""核心纯逻辑测试：归一化 + 分层模糊匹配（pytest 版，与 scripts/selftest.py 互补）。"""

from __future__ import annotations

import pytest

from app.core.fuzzy import MatchTarget, best_match, match_token, search_targets
from app.core.normalize import (
    canon_medium,
    canon_package,
    canon_type,
    canon_type_exact,
    canon_unit,
    classify_tags,
    extract_quantities,
    fold,
    infer_type_from_model,
    normalize_text,
    parse_quantity,
    quantities_equal,
    tokenize_query,
)


# --------------------------------------------------------------------------- #
# 文本归一化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("０.１ｕＦ", "0.1uF"),
        ("10µF", "10uF"),
        ("10μF", "10uF"),
        ("51\u2126", "51\u03a9"),
        ("  A   B  ", "A B"),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_fold_removes_separators() -> None:
    assert fold("STM32-F103/C8") == "stm32f103c8"


# --------------------------------------------------------------------------- #
# 物理量
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("token", "value", "dimension"),
    [
        ("0.1uF", 1e-7, "capacitance"),
        ("100nF", 1e-7, "capacitance"),
        ("4R7", 4.7, "resistance"),
        ("R47", 0.47, "resistance"),
        ("0R05", 0.05, "resistance"),
        ("1k2", 1200.0, None),
        ("2M2", 2.2e6, None),
        ("4u7", 4.7e-6, None),
        ("51r", 51.0, "resistance"),
        ("16v", 16.0, "voltage"),
    ],
)
def test_parse_quantity(token: str, value: float, dimension: str | None) -> None:
    quantity = parse_quantity(token)
    assert quantity is not None
    assert quantity.value == pytest.approx(value, rel=1e-9)
    if dimension:
        assert quantity.dimension == dimension


def test_absolute_equivalence() -> None:
    """参考仓库的核心语义：0.1uF ≡ 100nF。"""
    assert quantities_equal(parse_quantity("0.1uF"), parse_quantity("100nF"))
    assert not quantities_equal(parse_quantity("100Ω"), parse_quantity("100nF"))


@pytest.mark.parametrize("model", ["1N4148", "1N4007", "STM32F103C8T6", "LM358", "LQFP48", "0805"])
def test_model_numbers_are_not_quantities(model: str) -> None:
    assert parse_quantity(model) is None


def test_si_prefix_case_sensitive() -> None:
    assert parse_quantity("1M", hint="resistance").value == pytest.approx(1e6)
    assert parse_quantity("1m", hint="resistance").value == pytest.approx(1e-3)


def test_eia_code_requires_hint() -> None:
    assert parse_quantity("104") is None
    assert parse_quantity("104", hint="capacitance").value == pytest.approx(1e-7)
    assert parse_quantity("473", hint="capacitance").value == pytest.approx(47e-9)


# --------------------------------------------------------------------------- #
# 归约
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("token", "expected"),
    [("电容", "C"), ("Capacitor", "C"), ("MLCC", "C"), ("单片机", "U"), ("排针", "J"), ("晶振", "XTAL")],
)
def test_canon_type_exact(token: str, expected: str) -> None:
    assert canon_type_exact(token) == expected


def test_weak_reduction_is_separate() -> None:
    """弱证据回退不能污染强归约（白卡不应被当成 LED）。"""
    assert canon_type_exact("白卡") is None
    assert canon_type("白卡") == "LED"


def test_canon_medium_and_package() -> None:
    assert canon_medium("瓷片电容") == "MLCC"
    assert canon_package("5x11mm") == "5X11"
    assert canon_package("SOP-8") == "SOP8"
    assert canon_package("SOIC-8") == "SOP8"
    assert canon_package("lqfp-48") == "LQFP48"


def test_canon_unit() -> None:
    assert canon_unit("51r") == "51Ω"
    assert canon_unit("16v") == "16V"
    assert canon_unit("0.1uF") == "0.1uF"


def test_model_type_hint() -> None:
    assert infer_type_from_model("STM32F103C8T6") == "U"
    assert infer_type_from_model("1N4148") == "D"
    assert infer_type_from_model("2N2222") == "Q"
    assert infer_type_from_model("随便什么") is None


def test_classify_tags() -> None:
    plan = classify_tags(["0.1uF", "50V", "MLCC", "0805", "电容"])
    assert plan.type_code == "C"
    assert plan.medium == "MLCC"
    assert plan.package == "0805"
    assert any(q.dimension == "capacitance" for q in plan.quantities)


def test_tokenize_query() -> None:
    assert tokenize_query("查 STM32F103  0.1uF") == ["查", "STM32F103", "0.1uF"]
    assert tokenize_query("电容，电阻") == ["电容", "电阻"]


# --------------------------------------------------------------------------- #
# 复合名称里的物理量（「100欧姆电阻」≡「100Ω电阻」）
# --------------------------------------------------------------------------- #
def test_extract_quantities_from_composite_names() -> None:
    ohm_cn = extract_quantities("100欧姆电阻")
    ohm_symbol = extract_quantities("100Ω电阻")
    assert ohm_cn and ohm_symbol
    assert ohm_cn[0].dimension == "resistance"
    assert quantities_equal(ohm_cn[0], ohm_symbol[0])

    assert extract_quantities("100kΩ电阻")[0].value == pytest.approx(100_000)
    assert extract_quantities("16MHz晶振")[0].dimension == "frequency"


@pytest.mark.parametrize("token", ["STM32F103C8T6", "LQFP48", "AMS1117-3.3", "NE555", "0805", "八方旅人 随身包"])
def test_extract_quantities_ignores_model_numbers(token: str) -> None:
    assert extract_quantities(token) == []


def test_composite_quantity_matching(targets: list[MatchTarget]) -> None:
    """两种写法必须互相命中，且不能串到别的阻值上。"""
    cn = MatchTarget(101, "100欧姆电阻", category="stm_component")
    symbol = MatchTarget(102, "100Ω电阻", category="stm_component")

    assert match_token("100Ω电阻", cn)[0] == 1.0
    assert match_token("100欧姆电阻", symbol)[0] == 1.0
    assert match_token("100Ω", cn)[0] == 1.0
    assert match_token("100欧姆", symbol)[0] == 1.0

    other = MatchTarget(103, "10kΩ 1% 电阻", category="stm_component")
    assert match_token("100Ω电阻", other)[0] == 0.0
    assert match_token("100欧姆电阻", other)[0] == 0.0


def test_quantity_equivalence_unchanged(targets: list[MatchTarget]) -> None:
    """回归：原有的 0.1uF ≡ 100nF 不能被新逻辑破坏。"""
    cap = targets[1]
    assert match_token("0.1uF", cap)[0] == 1.0
    assert match_token("100nF", cap)[0] == 1.0
    assert match_token("100Ω", cap)[0] == 0.0


# --------------------------------------------------------------------------- #
# 分层匹配
# --------------------------------------------------------------------------- #
@pytest.fixture()
def targets() -> list[MatchTarget]:
    return [
        MatchTarget(1, "STM32F103C8T6", ["F103C8", "STM32F103"], "LQFP48", "stm_component", "A柜-1层"),
        MatchTarget(2, "0.1uF 50V MLCC 0805", ["104", "100nF"], "0805", "stm_component", "A柜-2层"),
        MatchTarget(3, "Steam 50元充值卡", ["50元卡"], "", "steam_card", "B柜-抽屉1"),
        MatchTarget(4, "LM358 双运放", (), "SOIC-8", "stm_component", "A柜-3层"),
    ]


def test_exact_match_is_one(targets: list[MatchTarget]) -> None:
    stm = targets[0]
    assert match_token("STM32F103C8T6", stm)[0] == 1.0
    assert match_token("stm32f103c8t6", stm)[0] == 1.0
    assert match_token("F103C8", stm)[0] == 1.0
    assert match_token("LQFP48", stm)[0] == 1.0
    assert match_token("lqfp-48", stm)[0] == 1.0


def test_quantity_equivalence_in_matching(targets: list[MatchTarget]) -> None:
    cap = targets[1]
    assert match_token("0.1uF", cap)[0] == 1.0
    assert match_token("100nF", cap)[0] == 1.0
    assert match_token("104", cap)[0] == 1.0


def test_reduction_tiers(targets: list[MatchTarget]) -> None:
    stm, cap, _, sop = targets
    assert match_token("单片机", stm)[0] == 0.9
    assert match_token("电容器", cap)[0] == 0.9
    assert match_token("陶瓷电容", cap)[0] == 0.9
    assert match_token("SOP8", sop)[0] == 0.9


def test_false_positive_guards(targets: list[MatchTarget]) -> None:
    cap = targets[1]
    assert match_token("100Ω", cap)[0] == 0.0      # 维度闸门
    assert match_token("805", cap)[0] == 0.0       # 前导零封装
    assert match_token("电容", targets[0])[0] == 0.0  # 类型不符不降级
    assert match_token("100nF", MatchTarget(9, "1100nF"))[0] == 0.0


def test_partial_model_prefix(targets: list[MatchTarget]) -> None:
    score, reason = match_token("STM32F103", targets[0])
    assert score >= 0.55
    assert reason


def test_search_and_resolve(targets: list[MatchTarget]) -> None:
    hits = search_targets("STM32", targets, threshold=0.55)
    assert [h.target_id for h in hits] == [1]

    hits = search_targets("100nF 0805", targets, threshold=0.55)
    assert [h.target_id for h in hits] == [2]

    best, score, _ = best_match("STM32F103C8T6", targets)
    assert best is not None and best.id == 1 and score == 1.0


def test_search_returns_empty_for_unrelated(targets: list[MatchTarget]) -> None:
    assert search_targets("LM7805", targets, threshold=0.55) == []


# --------------------------------------------------------------------------- #
# 类型码由物理量维度补出（实测 bug）
# --------------------------------------------------------------------------- #
def test_unit_only_name_still_gets_a_type_code() -> None:
    """``10kΩ 0805`` 里没有「电阻 / R」字样，但 Ω 已经说明它是电阻。

    不把物理量维度提升成类型码，``查 电阻`` / ``查 R`` 就找不到它 ——
    用户实测反馈的「10kΩ / R / 电阻 三个东西没归到一块」就是这个问题。
    """
    items = [
        MatchTarget(id=1, name="R 10kΩ 0805", spec="0805"),
        MatchTarget(id=2, name="C 1uF 16V MLCC 0805", spec="MLCC 0805"),
        MatchTarget(id=3, name="排母 3p 2.54", spec="2.54"),
    ]

    # 「电阻」「R」「欧姆」三种说法都要命中同一条
    for query in ("电阻", "R", "欧姆"):
        hits = search_targets(query, items, threshold=0.8)
        assert [h.target_id for h in hits] == [1], f"{query} 应该只命中电阻"

    # 电容同理，且不能串到电阻上
    assert [h.target_id for h in search_targets("电容", items, threshold=0.8)] == [2]

    # 数值 + 类型组合（AND 语义）
    assert [h.target_id for h in search_targets("电阻 10k", items, threshold=0.6)] == [1]

    # 名称里没有 R/电阻 前缀时同样成立
    bare = [MatchTarget(id=9, name="10kΩ 0805", spec="0805")]
    assert [h.target_id for h in search_targets("电阻", bare, threshold=0.8)] == [9]


def test_literal_occurrence_beats_weak_type() -> None:
    """实测 bug：``绿红`` 既是 LED 颜色词（弱类型 0.7）又字面在名称里（0.92）。

    弱类型层排在字面匹配之前，把多词名称的整串得分压到 0.9 以下，
    导致「发光二极管 共阴 绿红」明明在库却被判定为「库存里没有」。
    """
    from app.core.fuzzy import MatchTarget, match_token, search_targets

    target = MatchTarget(id=1, name="发光二极管 共阴 绿红", spec="2.54mm")
    assert match_token("绿红", target)[0] >= 0.9

    hits = search_targets("发光二极管 共阴 绿红", [target], threshold=0.9)
    assert [h.target_id for h in hits] == [1]

    # 没有字面命中时，弱类型仍然生效，但分值必须**低于默认阈值** ——
    # 「红茶」不该因为「红」而把「红色LED」搜出来（0.7 会，0.45 不会）
    other = MatchTarget(id=2, name="贴片LED", spec="0805")
    assert match_token("绿红", other)[0] == 0.45
    # 强类型仍然走 0.9（``排针`` → 连接器）
    assert match_token("排针", MatchTarget(id=3, name="杜邦线", spec=""))[0] == 0.9


def test_whole_word_substring_scores_high() -> None:
    """``11P`` 是「排母 11P」里的一个**独立词** —— 不该被长度比压到 0.7 以下。

    实测 bug：采购比对要求 0.9 分，而「排母 11P」只能拿到 0.76，
    于是明明有货却被报成「库存里没有」。
    """
    from app.core.fuzzy import MatchTarget, _substring_score, search_targets

    assert (_substring_score("11P", "排母 11P") or 0) >= 0.9

    # 仍然不能串到别的针数
    assert _substring_score("11P", "排母 22P") is None

    items = [
        MatchTarget(id=1, name="排母 11P", spec="2.54"),
        MatchTarget(id=2, name="排母 22P", spec="2.54"),
    ]
    assert [h.target_id for h in search_targets("排母 11P", items, threshold=0.9)] == [1]
    assert [h.target_id for h in search_targets("排母 22P", items, threshold=0.9)] == [2]

    # 防误匹配守卫不受影响
    assert _substring_score("100nF", "1100nF") is None
    assert _substring_score("805", "0805") is None
    assert _substring_score("100Ω", "1100Ω") is None


def test_type_from_dimension_does_not_fire_on_other_units() -> None:
    """电压 / 功率 / 封装码不该被当成元件类型。"""
    from app.core.normalize import classify_tags

    assert classify_tags(["0.25W", "直插"]).type_code is None
    assert classify_tags(["16V"]).type_code is None
    assert classify_tags(["0805"]).type_code is None
    assert classify_tags(["2.54"]).type_code is None
    # 介质名依然能给出类型
    assert classify_tags(["MLCC"]).type_code == "C"


# --------------------------------------------------------------------------- #
# 分类：不预设任何内置分类
# --------------------------------------------------------------------------- #
def test_there_are_no_builtin_categories() -> None:
    """分类完全由数据和 AI 决定；只有「未分类」这一个保留值。"""
    from app.core import categories

    assert categories.DEFAULT_CATEGORY == "未分类"
    assert not hasattr(categories, "BUILTIN_CATEGORIES")
    assert not hasattr(categories, "CATEGORY_ALIASES")


def test_guess_category_never_guesses() -> None:
    """「名字猜分类」这件事彻底交给 AI —— 规则只保证有个分类。"""
    from app.core.categories import DEFAULT_CATEGORY, guess_category

    for name in ("NE555", "Steam 50元充值卡", "10kΩ 0805", "杜邦线", "主角 书架"):
        assert guess_category(name) == DEFAULT_CATEGORY


def test_category_names_are_capped_to_what_the_user_says() -> None:
    """AI / 用户报什么分类名就存什么，不做别名折叠。"""
    from app.core.categories import normalize_category_code

    assert normalize_category_code("耗材") == "耗材"
    assert normalize_category_code("开发板") == "开发板"
    assert normalize_category_code(" 传感器 ") == "传感器"
    assert normalize_category_code("") == "未分类"


def test_legacy_category_codes_map_to_names() -> None:
    """旧库里的分类代码显示/迁移成中文名。"""
    from app.core.categories import category_label, normalize_category_code

    assert normalize_category_code("stm_component") == "STM元器件"
    assert normalize_category_code("steam_card") == "Steam游戏卡"
    assert normalize_category_code("other") == "未分类"
    assert category_label("stm_component") == "STM元器件"
    assert category_label("耗材") == "耗材"


def test_category_hint_uses_the_users_own_words() -> None:
    """「这是游戏卡」→ 直接建「游戏卡」这个分类，不靠预设词表。"""
    from app.core.categories import detect_category_hint

    assert detect_category_hint("这没有封装啊，这是游戏卡") == "游戏卡"
    assert detect_category_hint("这些应该归到耗材") == "耗材"
    assert detect_category_hint("我随便说点什么") is None
    assert detect_category_hint("这是个什么东西") is None


def test_category_from_heading_is_data_driven() -> None:
    from app.core.categories import category_from_heading

    assert category_from_heading("耗材") == "耗材"
    assert category_from_heading("Steam游戏卡") == "Steam游戏卡"
    assert category_from_heading("## 开发板") == "开发板"
    assert category_from_heading("") is None
    assert category_from_heading("---") is None


def test_legacy_categories_are_migrated(tmp_path) -> None:
    """v1 库里的旧分类代码，在初始化时会被改名成中文分类名。"""
    from app.db import Database

    path = tmp_path / "old.db"
    database = Database(path)
    database.initialize()
    with database.connection() as conn:
        conn.execute(
            "INSERT INTO items (name, name_key, category, quantity, unit, location, "
            "location_key, spec, spec_key, note, created_at, updated_at, created_by, updated_by) "
            "VALUES ('旧零件', '旧零件', 'stm_component', 5, '', '', '', '', '', '', "
            "'2026-01-01', '2026-01-01', '', '')"
        )
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")

    reopened = Database(path)
    reopened.initialize()
    assert reopened.query_one("SELECT category FROM items")["category"] == "STM元器件"

    # 幂等：再开一次不会重复处理，也不会改动已经改好的名字
    again = Database(path)
    again.initialize()
    assert again.query_one("SELECT category FROM items")["category"] == "STM元器件"


def test_category_tags_come_from_the_name() -> None:
    """分类名本身就是检索标签；「未分类」不该产出标签。"""
    from app.core.fuzzy import category_tags

    assert category_tags("耗材") == ("耗材",)
    assert category_tags("未分类") == ()


@pytest.mark.parametrize("word", ["排母", "排针", "插排", "插针", "针座", "母座", "接线端子"])
def test_connector_synonyms_share_one_type(word: str) -> None:
    """实测 bug：用户说「插排」，系统认不出来 —— 它和排母/排针是同一类连接器。"""
    from app.core.normalize import canon_type_exact

    assert canon_type_exact(word) == "J"


def test_search_finds_connectors_by_any_synonym() -> None:
    from app.core.fuzzy import MatchTarget, search_targets

    items = [MatchTarget(id=1, name="排母 11P", spec="2.54")]
    for word in ("插排", "排母", "排针"):
        assert [h.target_id for h in search_targets(word, items, threshold=0.8)] == [1], word
