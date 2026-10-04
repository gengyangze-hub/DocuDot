#!/usr/bin/env python3
"""指令面验收：把 docs/commands.md 里承诺的每条指令**真跑一遍**。

用法（需要一个**可丢弃的**实例，脚本会写入并删除数据）：

    python3 scripts/check_commands.py [base_url] [api_key]

脚本自己灌入所需的种子数据，所以结果和外部库状态无关。
每个用例用独立的 conversation，互不干扰；任何一条回落到「没找到…」都算失败 ——
那说明这条指令没被接住。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/v1"
KEY = sys.argv[2] if len(sys.argv) > 2 else "dev-admin-key"

#: 落回这些文案就说明指令没被接住
FALLBACK_MARKERS = ("没找到和", "没解析出物品名称", "换个写法试试")

#: 用例依赖的种子数据（幂等：已存在就跳过）
SEED: list[dict] = [
    {"name": "STM32F103C8T6", "category": "STM元器件", "quantity": 25,
     "location": "A柜-1层", "spec": "LQFP48", "aliases": ["F103C8"]},
    {"name": "0.1uF 50V MLCC", "category": "STM元器件", "quantity": 500,
     "location": "A柜-2层", "spec": "0805", "aliases": ["104"]},
    {"name": "NE555", "category": "STM元器件", "quantity": 30,
     "location": "C柜-1层", "spec": "DIP-8"},
    {"name": "杜邦线", "category": "线材", "quantity": 100, "location": "C库"},
    {"name": "排针", "category": "接插件", "quantity": 50, "location": "D柜"},
    {"name": "Steam 50元充值卡", "category": "Steam游戏卡", "quantity": 10, "location": "B柜"},
]

#: (用例名, 输入, 期望包含的子串)
CASES: list[tuple[str, str, str]] = [
    # ---- 查询与统计 ----
    ("帮助", "帮助", "入库"),
    ("菜单别名", "菜单", "入库"),
    ("在库清单", "库存", "库存总览"),
    ("总览", "总览", "库存总览"),
    ("概况别名", "概况", "库存总览"),
    ("全部条目", "清单", "在库共"),
    ("所有物品别名", "所有物品", "在库共"),
    ("查", "查 STM32", "找到"),
    ("搜索别名", "搜索 电容", "找到"),
    ("找别名", "找 杜邦线", "找到"),
    ("按分类", "分类 STM元器件", "STM元器件"),
    ("类别别名", "类别 STM元器件", "STM元器件"),
    ("按位置", "位置 A柜", "A柜"),
    ("库位别名", "库位 C库", "C库"),
    ("零库存", "零库存", ""),
    ("空库存别名", "空库存", ""),
    ("已清零别名", "已清零", ""),
    ("低库存", "库存不足", ""),
    ("低库存别名", "低库存", ""),
    ("缺货别名", "缺货", ""),
    ("补货别名", "补货", ""),
    ("看别名", "别名 STM32F103C8T6", "的别名"),
    # ---- 出入库 ----
    ("入库", "入库 NE555 30", "入库成功"),
    ("进货别名", "进货 NE555 5", "入库成功"),
    ("收别名", "收 NE555 5", "入库成功"),
    ("出库", "出库 NE555 5", "出库成功"),
    ("领用别名", "领用 NE555 5", "出库成功"),
    ("用掉别名", "用掉 NE555 5", "出库成功"),
    ("盘点", "盘点 NE555 42", ""),
    ("设为别名", "设为 NE555 42", ""),
    ("更正别名", "更正 NE555 42", ""),
    # ---- 清单类 ----
    ("采购", "采购 STM32F103C8T6 10", "对照采购清单"),
    ("对单别名", "对单 STM32F103C8T6 10", "对照采购清单"),
    ("缺料别名", "缺料 STM32F103C8T6 10", "对照采购清单"),
    ("要买什么别名", "要买什么 STM32F103C8T6 10", "对照采购清单"),
    ("导入", "导入", "清单"),
    # ---- 归类 / 合并 ----
    ("归类", "归类 杜邦线 耗材", "改到"),
    ("归入别名", "杜邦线归入stm元器件", "改到"),
    ("合并无候选", "合并", "可以合并的候选"),
    # ---- 删除 ----
    ("删除", "删除 NE555", "将删除"),
    ("删除全部", "删除全部", "全部"),
    ("清理零库存", "清理零库存", ""),
    ("删除零库存别名", "删除零库存", ""),
    # ---- 批量 ----
    ("整批出库", "出库全部", "整批出库"),
    ("清除全部", "清除全部", "整批出库"),
    ("清空库存别名", "清空库存", "整批出库"),
    # ---- AI 兜底（这几条规则认不出来，必须配好 LLM 才能过）----
    ("口语整批出库", "全部不要了", "整批出库"),
    ("口语总览", "手头还有多少东西", "总览"),
    ("口语入库", "进了 200 个排针放 D柜", "入库成功"),
    ("口语入库撞位置", "进了 200 个杜邦线放 D柜", "已经有一条记录了"),
    ("指代不明不乱执行", "那个东西呢", "没找到"),
]

#: 这些用例依赖大模型。没配 LLM 时**跳过**而不是报失败 ——
#: 否则在全新部署（还没填 LLM_API_KEY）上跑会看到一堆莫名其妙的红。
NEEDS_LLM: frozenset[str] = frozenset({
    "口语整批出库", "口语总览", "口语入库", "口语入库撞位置", "指代不明不乱执行",
})


def call(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def seed() -> None:
    """灌入用例依赖的数据；已存在（422）就跳过，保证可重复运行。"""
    for payload in SEED:
        try:
            call("/items", payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise
    print(f"已就绪种子数据（{len(SEED)} 条）\n")


def llm_ready() -> bool:
    """问一下服务有没有配好大模型（没配就跳过依赖 AI 的用例）。"""
    try:
        request = urllib.request.Request(
            BASE.rsplit("/api/v1", 1)[0] + "/health",
            headers={"X-API-Key": KEY},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return bool(json.load(response).get("llm_ready"))
    except Exception:  # noqa: BLE001 - 探测失败就当作没有
        return False


def main() -> int:
    try:
        seed()
    except urllib.error.URLError as exc:
        print(f"连不上服务 {BASE}：{exc}")
        return 2

    has_llm = llm_ready()
    if not has_llm:
        print("提示：服务未配置大模型（llm_ready=false），依赖 AI 的用例会跳过\n")

    passed = 0
    skipped: list[str] = []
    failures: list[str] = []

    for index, (label, text, expected) in enumerate(CASES, start=1):
        if label in NEEDS_LLM and not has_llm:
            skipped.append(label)
            print(f"  – {label:<16} 跳过（需要大模型）")
            continue
        try:
            reply = call(
                "/bot/command",
                {"text": text, "conversation": f"check-{index}", "operator": "check"},
            )["reply"]
        except urllib.error.HTTPError as exc:  # pragma: no cover - 验收脚本
            failures.append(f"{label}（{text}）：HTTP {exc.code}")
            print(f"  ✗ {label:<16} HTTP {exc.code}")
            continue

        first = reply.splitlines()[0] if reply else ""
        if expected and expected not in reply:
            failures.append(f"{label}（{text}）：期望包含 {expected!r}，实际 {first[:40]!r}")
            print(f"  ✗ {label:<16} {first[:52]}")
            continue
        if not expected and any(marker in reply for marker in FALLBACK_MARKERS):
            failures.append(f"{label}（{text}）：回落到了「没找到」")
            print(f"  ✗ {label:<16} {first[:52]}")
            continue

        passed += 1
        print(f"  ✓ {label:<16} {first[:52]}")

    print()
    total = len(CASES)
    tail = f"，{len(skipped)} 项跳过（需大模型）" if skipped else ""
    if failures:
        print(f"指令面验收：{passed}/{total} 项通过，{len(failures)} 项失败{tail}")
        for failure in failures:
            print(f"  · {failure}")
        return 1
    print(f"指令面验收：{passed}/{total} 项通过 ✓{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
