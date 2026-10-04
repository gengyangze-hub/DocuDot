#!/usr/bin/env python3
"""性能基准：在本地临时库上量常见指令的耗时。

用法：

    python3 scripts/bench.py [条目数] [每项重复次数]

脚本自建临时库（不碰线上数据），灌入 N 条物品后逐项计时，输出中位数。
拿来对比优化前后，而不是当绝对指标 —— 机器负载会影响绝对值。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import ItemCreate  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
REPEAT = int(sys.argv[2]) if len(sys.argv) > 2 else 5

CATEGORIES = ["STM元器件", "耗材", "工具", "五金件", "文具", "线材"]
LOCATIONS = [f"{cab}柜-{layer}层" for cab in "ABCDE" for layer in range(1, 5)]
SPECS = ["0805", "0603", "LQFP48", "DIP-8", "SOT-223", "直插"]


def build_app() -> tuple:
    tmp = tempfile.mkdtemp(prefix="bench-")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=f"{tmp}/bench.db",
        data_dir=tmp,
        bootstrap_api_key="bench-key",
        llm_enabled=False,
        qq_bot_enabled=False,
    )
    return create_app(settings), tmp


def seed(app, n: int) -> float:
    inventory = app.state.ctx.inventory
    started = time.perf_counter()
    for index in range(n):
        inventory.create_item(
            ItemCreate(
                name=f"元件{index:05d}",
                category=CATEGORIES[index % len(CATEGORIES)],
                quantity=100 + index % 500,
                location=LOCATIONS[index % len(LOCATIONS)],
                spec=SPECS[index % len(SPECS)],
                aliases=[f"E{index:05d}", f"别名{index % 100:03d}"],
            )
        )
    return time.perf_counter() - started


def timeit(func, repeat: int = REPEAT) -> float:
    """返回中位数耗时（毫秒）。"""
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        func()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main() -> int:
    print(f"灌入 {N} 条物品…")
    app, tmp = build_app()
    seed_seconds = seed(app, N)
    print(f"  用时 {seed_seconds:.1f}s（{seed_seconds / N * 1000:.1f} ms/条）\n")

    router = app.state.ctx.commands

    def say(text: str) -> None:
        asyncio.run(router.handle(text, operator="bench", scene="qq-c2c", conversation="bench"))

    cases: list[tuple[str, object]] = [
        ("帮助", lambda: say("帮助")),
        ("库存（总览+清单）", lambda: say("库存")),
        ("查 关键词（模糊检索）", lambda: say("查 元件")),
        ("查 型号开头", lambda: say("查 元件01")),
        ("入库存量物品（热路径）", lambda: say("入库 元件00001 5")),
        ("入库新物品（建档）", lambda: say(f"入库 新物品{time.time_ns()} 5 @Z柜")),
        ("分类 清单", lambda: say("分类 耗材")),
        ("位置 清单", lambda: say("位置 A柜-1层")),
        ("采购比对（3 项）", lambda: say("采购\n| 名称 | 数量 |\n| --- | --- |\n"
                                        "| 元件00001 | 10 |\n| 元件00002 | 10 |\n| 不存在XYZ | 5 |")),
        ("NLQ 提问", lambda: say("总共有多少种物品")),
        ("零库存", lambda: say("零库存")),
    ]

    # 分类计数用 prepare：把全量记录读一遍的成本
    rows: list[tuple[str, float]] = []
    for label, func in cases:
        rows.append((label, timeit(func)))  # type: ignore[arg-type]

    print(f"{'用例':<26}{'中位数':>10}")
    print("-" * 38)
    for label, ms in rows:
        print(f"{label:<26}{ms:>8.1f} ms")
    print("-" * 38)
    worst = max(ms for _l, ms in rows)
    print(f"{'最慢':<26}{worst:>8.1f} ms")
    print(f"\n临时库：{tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
