"""统计报表。"""

from __future__ import annotations

from ..config import Settings
from ..models import CategoryStat, ItemOut, LocationStat, MovementOut, OverviewOut
from ..repository import Repository
from ..utils import fmt_qty, now_iso
from .categorize import category_label


class AnalysisService:
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    # ------------------------------------------------------------------ 总览
    def overview(self, *, low_stock_threshold: float = 0, recent_limit: int = 10) -> OverviewOut:
        counts = self.repo.overview_counts()
        _, movement_rows = self.repo.list_movements(limit=recent_limit)
        low = self.repo.low_stock(low_stock_threshold, limit=10) if low_stock_threshold > 0 else []
        return OverviewOut(
            generated_at=now_iso(),
            item_count=counts["item_count"],
            total_quantity=counts["total_quantity"],
            location_count=counts["location_count"],
            category_count=counts["category_count"],
            alias_count=counts["alias_count"],
            movement_count=counts["movement_count"],
            categories=self.categories(),
            top_locations=self.locations(limit=10),
            recent_movements=[MovementOut(**dict(row)) for row in movement_rows],
            low_stock=[record.to_out() for record in low],
        )

    # ------------------------------------------------------------------ 分类
    def categories(self) -> list[CategoryStat]:
        result: list[CategoryStat] = []
        for row in self.repo.category_stats():
            raw = row["category"]
            result.append(
                CategoryStat(
                    category=raw,
                    label=category_label(raw),
                    item_count=int(row["item_count"]),
                    total_quantity=float(row["total_quantity"]),
                    distinct_locations=int(row["distinct_locations"]),
                )
            )
        return result

    # ------------------------------------------------------------------ 位置
    def locations(self, limit: int = 20) -> list[LocationStat]:
        result: list[LocationStat] = []
        for row in self.repo.location_stats(limit=limit):
            location = row["location"] or "（未指定位置）"
            categories = self.repo.db.query(
                "SELECT category, COUNT(*) AS n FROM items WHERE location = ? GROUP BY category",
                (row["location"],),
            )
            result.append(
                LocationStat(
                    location=location,
                    item_count=int(row["item_count"]),
                    total_quantity=float(row["total_quantity"]),
                    categories={c["category"]: int(c["n"]) for c in categories},
                )
            )
        return result

    # ------------------------------------------------------------------ 低库存
    def low_stock(self, threshold: float, limit: int = 20) -> list[ItemOut]:
        return [record.to_out() for record in self.repo.low_stock(threshold, limit=limit)]
    # ------------------------------------------------------------------ 在库
    def in_stock_records(self) -> list:
        """在库记录：数量 > 0。已出库的条目不算「库存」。"""
        return [record for record in self.repo.all_items() if record.quantity > 0]

    def live_category_summary(self) -> list[tuple[str, str, int, float]]:
        """按分类汇总**在库**物品：``(分类, 显示名, 种数, 总数)``。

        分类不预设 —— 有什么分类就汇总什么，按「种数 → 数量」降序。
        """
        buckets: dict[str, list] = {}
        for record in self.in_stock_records():
            buckets.setdefault(record.category, []).append(record)
        summary = [
            (code, category_label(code), len(group), sum(r.quantity for r in group))
            for code, group in buckets.items()
        ]
        summary.sort(key=lambda row: (-row[2], -row[3], row[0]))
        return summary

    # ------------------------------------------------------------------ 摘要文本
    def overview_text(self, *, limit: int = 30) -> str:
        """总览 = 汇总数字 + **具体物品清单**（默认只看在库）。

        只给「8 种 / 1857 件」这种汇总是没法用的 —— 用户看不到到底有什么。
        """
        live = self.in_stock_records()
        zero_count = self.repo.overview_counts()["item_count"] - len(live)
        lines = [
            f"库存总览：在库 {len(live)} 种，共 {fmt_qty(sum(r.quantity for r in live))} 件，"
            f"{len({r.location for r in live if r.location})} 个位置"
        ]
        for _, label, count, quantity in self.live_category_summary():
            lines.append(f"  · {label}：{count} 种 / {fmt_qty(quantity)} 件")
        if zero_count:
            lines.append(f"  （另有 {zero_count} 种已清零，回复「零库存」查看）")
        listing = self.format_records(live, limit=limit)
        if listing:
            lines.append("")
            lines.append("物品清单：")
            lines.extend(listing)
        return "\n".join(lines)

    @staticmethod
    def format_records(records, *, limit: int = 30, start: int = 1) -> list[str]:
        """把一批物品渲染成带序号的清单行（可回溯序号选择）。"""
        lines: list[str] = []
        for index, record in enumerate(records[:limit], start=start):
            line = f"  {index}. {record.name}"
            spec = getattr(record, "spec", "")
            if spec:
                line += f"（{spec}）"
            line += f" × {fmt_qty(record.quantity)}"
            location = getattr(record, "location", "")
            if location:
                line += f" @ {location}"
            lines.append(line)
        if len(records) > limit:
            lines.append(f"  …… 还有 {len(records) - limit} 项")
        return lines
