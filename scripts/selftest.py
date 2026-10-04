#!/usr/bin/env python3
"""自检脚本：只用标准库，不需要安装任何依赖。

    python3 scripts/selftest.py

参考仓库 konamivrc6/component-inventory 用 ``--selftest`` 做同类验证；
本项目把「归一化 + 分层模糊匹配」的关键断言固化在这里，
任何一次改动都可以先跑它，秒级得到反馈。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.fuzzy import MatchTarget, best_match, match_token, search_targets  # noqa: E402
from app.core.normalize import (  # noqa: E402
    canon_medium,
    canon_package,
    canon_type_exact,
    canon_unit,
    fold,
    normalize_text,
    parse_quantity,
    quantities_equal,
    tokenize_query,
)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
    else:
        FAILED.append((label, detail))


def check_eq(label: str, actual: object, expected: object) -> None:
    check(label, actual == expected, f"期望 {expected!r}，实际 {actual!r}")


def check_close(label: str, actual: float, expected: float, tol: float = 1e-12) -> None:
    check(label, abs(actual - expected) <= tol * max(1.0, abs(expected)), f"期望 ≈{expected!r}，实际 {actual!r}")


# --------------------------------------------------------------------------- #
# 1. 文本归一化
# --------------------------------------------------------------------------- #
def test_normalize_text() -> None:
    check_eq("全角转半角", normalize_text("０.１ｕＦ"), "0.1uF")
    check_eq("micro sign 归一", normalize_text("10µF"), "10uF")
    check_eq("greek mu 归一", normalize_text("10μF"), "10uF")
    check_eq("ohm sign 归一", normalize_text("51\u2126"), "51\u03a9")
    check_eq("压缩空白", normalize_text("  A   B  "), "A B")
    check_eq("不折叠大小写", normalize_text("1M2"), "1M2")
    check_eq("fold 去分隔符", fold("STM32-F103/C8"), "stm32f103c8")


# --------------------------------------------------------------------------- #
# 2. 物理量解析（参考仓库的核心能力）
# --------------------------------------------------------------------------- #
def test_parse_quantity() -> None:
    check_close("0.1uF = 1e-7", parse_quantity("0.1uF").value, 1e-7)
    check_close("100nF = 1e-7", parse_quantity("100nF").value, 1e-7)
    check("0.1uF ≡ 100nF", quantities_equal(parse_quantity("0.1uF"), parse_quantity("100nF")))
    check("0.1uF 维度=电容", parse_quantity("0.1uF").dimension == "capacitance")
    check("0.1uF 不改写成 100nF（canon_unit 不动 uF）", canon_unit("0.1uF") == "0.1uF")

    check_close("4R7 = 4.7Ω", parse_quantity("4R7").value, 4.7)
    check("4R7 维度=电阻", parse_quantity("4R7").dimension == "resistance")
    check_close("R47 = 0.47Ω", parse_quantity("R47").value, 0.47)
    check_close("0R05 = 0.05Ω", parse_quantity("0R05").value, 0.05)
    check_close("1k2 = 1200", parse_quantity("1k2").value, 1200.0)
    check_close("2M2 = 2.2e6", parse_quantity("2M2").value, 2.2e6)
    check_close("4u7 = 4.7e-6", parse_quantity("4u7").value, 4.7e-6)

    check_close("51R = 51Ω", parse_quantity("51r").value, 51.0)
    check("51R 维度=电阻", parse_quantity("51r").dimension == "resistance")
    check_close("16V", parse_quantity("16v").value, 16.0)
    check("50V 维度=电压", parse_quantity("50V").dimension == "voltage")

    # 型号拦截
    for model in ("1N4148", "1N4007", "STM32F103C8T6", "LM358", "LQFP48", "0805", "0603"):
        check(f"型号不当物理量: {model}", parse_quantity(model) is None)

    # M / m 不折叠
    check_close("1M = 1e6（兆）", parse_quantity("1M", hint="resistance").value, 1e6)
    check_close("1m = 1e-3（毫）", parse_quantity("1m", hint="resistance").value, 1e-3)

    # EIA 三位码：仅在 hint 明确时启用
    check("104 无 hint 不解析", parse_quantity("104") is None)
    check_close("104 + 电容 hint = 100nF", parse_quantity("104", hint="capacitance").value, 1e-7)
    check_close("473 + 电容 hint = 47nF", parse_quantity("473", hint="capacitance").value, 47e-9)

    # 维度闸门
    check("100Ω ≢ 100nF", not quantities_equal(parse_quantity("100Ω"), parse_quantity("100nF")))


# --------------------------------------------------------------------------- #
# 3. 归约
# --------------------------------------------------------------------------- #
def test_canon() -> None:
    check_eq("电容 → C", canon_type_exact("电容"), "C")
    check_eq("Capacitor → C", canon_type_exact("Capacitor"), "C")
    check_eq("MLCC → C", canon_type_exact("MLCC"), "C")
    check_eq("单片机 → U", canon_type_exact("单片机"), "U")
    check_eq("排针 → J", canon_type_exact("排针"), "J")
    check_eq("晶振 → XTAL", canon_type_exact("晶振"), "XTAL")
    check("白卡 强归约不误判", canon_type_exact("白卡") is None)

    check_eq("瓷片电容 → MLCC", canon_medium("瓷片电容"), "MLCC")
    check_eq("铝电解 → electrolytic", canon_medium("铝电解电容"), "electrolytic")

    check_eq("5x11mm → 5X11", canon_package("5x11mm"), "5X11")
    check_eq("SOP-8 → SOP8", canon_package("SOP-8"), "SOP8")
    check_eq("lqfp-48 → LQFP48", canon_package("lqfp-48"), "LQFP48")

    check_eq("51r → 51Ω", canon_unit("51r"), "51Ω")
    check_eq("51ohm → 51Ω", canon_unit("51ohm"), "51Ω")
    check_eq("16v → 16V", canon_unit("16v"), "16V")


# --------------------------------------------------------------------------- #
# 4. 分词
# --------------------------------------------------------------------------- #
def test_tokenize() -> None:
    check_eq("基本分词", tokenize_query("查 STM32F103  0.1uF"), ["查", "STM32F103", "0.1uF"])
    check_eq("中文逗号切分", tokenize_query("电容，电阻"), ["电容", "电阻"])
    check_eq("去除尾部标点", tokenize_query("STM32?"), ["STM32"])


# --------------------------------------------------------------------------- #
# 5. 分层模糊匹配
# --------------------------------------------------------------------------- #
def build_targets() -> list[MatchTarget]:
    return [
        MatchTarget(
            id=1,
            name="STM32F103C8T6",
            aliases=["F103C8", "STM32F103"],
            spec="LQFP48",
            category="stm_component",
            location="A柜-1层-盒3",
        ),
        MatchTarget(
            id=2,
            name="0.1uF 50V MLCC 0805",
            aliases=["104", "100nF"],
            spec="0805",
            category="stm_component",
            location="A柜-2层",
        ),
        MatchTarget(
            id=3,
            name="Steam 50元充值卡",
            aliases=["50元卡"],
            category="steam_card",
            location="B柜-抽屉1",
        ),
        MatchTarget(
            id=4,
            name="LM358 双运放",
            spec="SOIC-8",
            category="stm_component",
            location="A柜-3层",
        ),
    ]


def test_matching() -> None:
    targets = build_targets()
    stm, cap, card, sop = targets

    # ---- 第 1 层：整串全等（名称 / 别名 / 规格字段） ----
    check_eq("名称全等 → 1.0", match_token("STM32F103C8T6", stm)[0], 1.0)
    check_eq("名称忽略大小写 → 1.0", match_token("stm32f103c8t6", stm)[0], 1.0)
    check_eq("别名全等 → 1.0", match_token("F103C8", stm)[0], 1.0)
    check_eq("规格字段全等 → 1.0", match_token("LQFP48", stm)[0], 1.0)
    check_eq("规格异形写法归一后全等 → 1.0", match_token("lqfp-48", stm)[0], 1.0)

    # ---- 第 5/6 层：子串部分匹配 ----
    check("型号前缀命中 ≥ 阈值", match_token("STM32F103", stm)[0] >= 0.55)

    # ---- 第 3 层：类型归约（搜「单片机」能找到 STM32） ----
    check_eq("单片机 → 类型 U", match_token("单片机", stm)[0], 0.9)
    check_eq("电容器 → 类型 C", match_token("电容器", cap)[0], 0.9)
    check_eq("电容 不得命中 MCU", match_token("电容", stm)[0], 0.0)

    # ---- 第 4 层：介质 / 封装归约 ----
    check_eq("陶瓷电容 → 介质 MLCC", match_token("陶瓷电容", cap)[0], 0.9)
    check_eq("SOIC-8 ≡ SOP8 → 0.9", match_token("SOP8", sop)[0], 0.9)

    # ---- 物理量等价 ----
    check_eq("0.1uF 全等 → 1.0", match_token("0.1uF", cap)[0], 1.0)
    check_eq("100nF ≡ 0.1uF → 1.0", match_token("100nF", cap)[0], 1.0)
    check_eq("EIA 104 别名全等 → 1.0", match_token("104", cap)[0], 1.0)
    check_eq("MLCC → 介质归约 0.9", match_token("MLCC", cap)[0], 0.9)
    check_eq("0805 规格字段全等 → 1.0", match_token("0805", cap)[0], 1.0)

    # ---- 防误匹配（参考仓库重点防护的场景） ----
    check("100Ω 不得命中电容", match_token("100Ω", cap)[0] == 0.0)
    check("805 不得命中 0805", match_token("805", cap)[0] == 0.0)
    check("100nF 不得命中 1100nF", match_token("100nF", MatchTarget(id=99, name="1100nF"))[0] == 0.0)
    check("1N4148 不得被当成 1.4148n", match_token("1N4148", MatchTarget(id=98, name="1N4148"))[0] == 1.0)

    # ---- Steam 卡 ----
    check_eq("游戏卡别名全等", match_token("50元卡", card)[0], 1.0)
    check("「充值卡」部分匹配 ≥ 阈值", match_token("充值卡", card)[0] >= 0.55)


def test_search() -> None:
    targets = build_targets()

    hits = search_targets("STM32", targets, threshold=0.55)
    check(
        "搜索 STM32 只命中 MCU 条目",
        len(hits) == 1 and hits[0].target_id == 1,
        f"实际 {[h.as_dict() for h in hits]}",
    )

    hits = search_targets("100nF 0805", targets, threshold=0.55)
    check(
        "AND 语义：100nF 0805 命中电容",
        len(hits) == 1 and hits[0].target_id == 2,
        f"实际 {[h.as_dict() for h in hits]}",
    )

    hits = search_targets("电容", targets, threshold=0.55)
    check("「电容」命中电容条目", any(h.target_id == 2 for h in hits), f"实际 {[h.as_dict() for h in hits]}")

    hits = search_targets("游戏卡", targets, threshold=0.55)
    check(
        "「游戏卡」命中 Steam 卡",
        len(hits) == 1 and hits[0].target_id == 3,
        f"实际 {[h.as_dict() for h in hits]}",
    )

    hits = search_targets("LM7805", targets, threshold=0.55)
    check("无关查询返回空", hits == [], f"实际 {[h.as_dict() for h in hits]}")

    target, score, _ = best_match("STM32F103C8T6", targets, threshold=0.6)
    check("best_match 唯一解析", target is not None and target.id == 1 and score >= 0.6)


def main() -> int:
    suites = {
        "文本归一化": test_normalize_text,
        "物理量解析": test_parse_quantity,
        "归约": test_canon,
        "分词": test_tokenize,
        "分层匹配": test_matching,
        "检索": test_search,
    }
    for name, func in suites.items():
        try:
            func()
        except Exception:  # noqa: BLE001
            FAILED.append((f"{name} 抛出异常", traceback.format_exc()))

    total = len(PASSED) + len(FAILED)
    print(f"自检结果：{len(PASSED)}/{total} 项通过")
    if FAILED:
        print("\n失败项：")
        for label, detail in FAILED:
            print(f"  ✗ {label}")
            if detail:
                for line in detail.strip().splitlines():
                    print(f"      {line}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
