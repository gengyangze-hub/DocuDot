"""自然语言问答（规则优先，大模型兜底）。

QQ 场景里用户会说「STM32 还有多少」「A柜里有什么」，
这里把这类话解析成结构化意图，再落到既有的检索/统计能力上。
规则解析不需要联网、延迟低，因此作为默认路径；
只有规则完全答不出来、且调用方显式 ``use_llm=True`` 时才交给大模型。

两点约定：

* 凡是「列东西」的回答，正文里都带序号清单，``data["items"]`` 里带
  ``id/name/quantity/location/spec``，上层据此登记上下文，
  于是用户回一句「2」就能选中第 2 项。
* 只给汇总数字不列清单是没用的（用户看不到到底有什么），因此
  ``total`` / ``overview`` / ``category_stats`` 都会附上具体条目。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from ..config import Settings
from ..models import NLQueryResponse
from ..repository import Repository
from ..utils import fmt_qty, truncate
from .analysis import AnalysisService
from .categorize import category_label, match_category
from .inventory import InventoryService
from .llm import LLMClient

logger = logging.getLogger(__name__)

#: 单条回复最多列多少项，避免 QQ 消息过长
ITEM_LIMIT = 30

#: 句子开头的礼貌用语/动词，解析前先剥掉
_LEADING_NOISE = re.compile(
    r"^(?:请问|请|帮我|帮忙|麻烦|我想知道|我想问一下|我想问|问一下|查一下|查询|查查|查|看看|看一下|搜一下|搜索|搜|找一下|找找|找)\s*"
)
_TRAILING_NOISE = re.compile(r"[。？！?!~～\s]+$")

#: 意图模式（**顺序敏感**：越具体的越靠前）
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("low_stock", re.compile(r"(?:低库存|库存不足|快没|缺货|要补货|该补货|需要补货|不够了|告急)")),
    (
        "total",
        re.compile(
            r"(?:总共有?多少种|一共多少种|共有多少种|多少种物品|多少种东西|有几种物品|种类总数|"
            r"总共多少|一共多少|总计多少|总计|总共|一共|合计)"
        ),
    ),
    ("category_stats", re.compile(r"(?:分类统计|各类库存|各类|按分类|分类情况|有多少种|有几种|种类统计)")),
    ("overview", re.compile(r"^(?:总览|概况|库存概况|库存总览|总体情况|总体|统计一下|都有什么|都有啥)$")),
    (
        "location_list",
        re.compile(
            r"(?:^|(?<=[\s，,]))(?P<loc>[^\s，,。？?]+?)\s*(?:里|中|内|里面|里边)?\s*(?:都)?(?:有|放|存|摆)(?:什么|哪些|啥|了哪些)"
        ),
    ),
    ("where", re.compile(r"(?P<kw>.+?)(?:在哪|在哪儿|在哪里|放在哪|放在哪儿|放在哪里|存放位置|位置在哪|放哪儿|放哪)")),
    ("spec", re.compile(r"(?P<kw>.+?)的(?:规格|封装|参数|型号规格)")),
    ("alias", re.compile(r"(?P<kw>.+?)的(?:别名|别称|标签)")),
    ("count", re.compile(r"(?P<kw>.+?)(?:还有多少|还剩多少|还有几个|还剩几个|有多少|剩多少|多少|库存量|库存|数量)")),
    ("list", re.compile(r"^(?:清单|列表|所有|全部|所有物品|全部物品|所有东西|库存清单)$")),
)


def _payload(records: Sequence[Any]) -> list[dict]:
    """把记录统一成 ``data["items"]`` 需要的形状。"""
    result: list[dict] = []
    for record in records:
        record_id = getattr(record, "id", None) or (
            record.get("id") if isinstance(record, dict) else None
        )
        if not record_id:
            continue
        get = (lambda key, default="": record.get(key, default)) if isinstance(record, dict) else (
            lambda key, default="": getattr(record, key, default)
        )
        result.append(
            {
                "id": int(record_id),
                "name": get("name", ""),
                "quantity": float(get("quantity", 0) or 0),
                "location": get("location", "") or "",
                "spec": get("spec", "") or "",
            }
        )
    return result


def _numbered(records: Sequence[Any], *, limit: int = ITEM_LIMIT) -> list[str]:
    lines: list[str] = []
    for index, record in enumerate(records[:limit], start=1):
        if isinstance(record, dict):
            name, spec = record.get("name", ""), record.get("spec", "")
            quantity, location = record.get("quantity", 0), record.get("location", "")
        else:
            name, spec = getattr(record, "name", ""), getattr(record, "spec", "")
            quantity, location = getattr(record, "quantity", 0), getattr(record, "location", "")
        line = f"  {index}. {name}"
        if spec:
            line += f"（{spec}）"
        line += f" × {fmt_qty(quantity)}"
        if not quantity:
            line += "（已清零）"
        if location:
            line += f" @ {location}"
        lines.append(line)
    if len(records) > limit:
        lines.append(f"  …… 还有 {len(records) - limit} 项")
    return lines


def _quantity_of(record: Any) -> float:
    if isinstance(record, dict):
        return float(record.get("quantity", 0) or 0)
    return float(getattr(record, "quantity", 0) or 0)


def _split_stock(records: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    """按数量拆成「在库」与「已清零」两部分。"""
    live = [record for record in records if _quantity_of(record) > 0]
    empty = [record for record in records if _quantity_of(record) <= 0]
    return live, empty


class NLQService:
    def __init__(
        self,
        inventory: InventoryService,
        analysis: AnalysisService,
        repo: Repository,
        settings: Settings,
        llm: LLMClient | None = None,
    ) -> None:
        self.inventory = inventory
        self.analysis = analysis
        self.repo = repo
        self.settings = settings
        self.llm = llm

    # ------------------------------------------------------------------ 入口
    async def answer(
        self,
        question: str,
        *,
        use_llm: bool = False,
        low_stock_threshold: float = 3,
    ) -> NLQueryResponse:
        text = self._normalize(question)
        intent, slots = self.parse(text)
        answer, data = self._dispatch(intent, slots, text, low_stock_threshold)
        used_llm = False

        if use_llm and self.llm and self.llm.ready and intent in {"unknown", "search"} and not data.get("items"):
            llm_answer = await self._ask_llm(text)
            if llm_answer:
                answer, used_llm = llm_answer, True

        return NLQueryResponse(
            question=question,
            intent=intent,
            answer=answer,
            data=data,
            used_llm=used_llm,
        )

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _normalize(question: str) -> str:
        text = (question or "").strip()
        text = _TRAILING_NOISE.sub("", text)
        text = _LEADING_NOISE.sub("", text)
        return text.strip()

    def parse(self, text: str) -> tuple[str, dict[str, str]]:
        for intent, pattern in PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            groups = match.groupdict()
            keyword = (groups.get("kw") or "").strip(" 的：:")
            location = (groups.get("loc") or "").strip(" 的：:")
            slots = {"keyword": keyword, "location": location}
            if intent in {"count", "where", "spec", "alias"} and not keyword:
                continue
            return intent, slots

        # 「STM元器件」「耗材」这类整串分类说法 → 列该分类清单。
        # **只认库里真实存在的分类**（没有内置分类了，不能把任意一句话都当成分类）。
        folded = text.casefold()
        for row in self.repo.category_stats():
            code = row["category"]
            if code and code.casefold() == folded:
                return "category_list", {"category": code}

        if text:
            return "search", {"keyword": text}
        return "unknown", {}

    # ------------------------------------------------------------------ 执行
    def _dispatch(
        self, intent: str, slots: dict[str, str], text: str, low_stock_threshold: float
    ) -> tuple[str, dict]:
        handler = {
            "overview": lambda: self._overview(),
            "total": lambda: self._total(),
            "low_stock": lambda: self._low_stock(low_stock_threshold),
            "category_stats": lambda: self._categories(),
            "category_list": lambda: self._category_list(slots.get("category", "")),
            "location_list": lambda: self._location_list(slots.get("location", "")),
            "where": lambda: self._where(slots.get("keyword", "")),
            "spec": lambda: self._attribute(slots.get("keyword", ""), "spec"),
            "alias": lambda: self._attribute(slots.get("keyword", ""), "alias"),
            "count": lambda: self._count(slots.get("keyword", "")),
            "list": lambda: self._list(),
            "search": lambda: self._search(slots.get("keyword", "") or text),
        }.get(intent)
        if handler is None:
            return "没能理解这句话。可以试试：库存 / STM32 还有多少 / A柜里有什么 / 出库全部", {"intent": intent}
        return handler()

    # ------------------------------------------------------------------ 各类回答
    def _overview(self) -> tuple[str, dict]:
        records = self.repo.all_items()
        live, empty = _split_stock(records)
        locations = {getattr(r, "location", "") for r in live if getattr(r, "location", "")}
        lines = [
            f"库存总览：在库 {len(live)} 种，共 {fmt_qty(sum(_quantity_of(r) for r in live))} 件，"
            f"{len(locations)} 个位置"
        ]
        for _, label, count, quantity in self.analysis.live_category_summary():
            lines.append(f"  · {label}：{count} 种 / {fmt_qty(quantity)} 件")
        if empty:
            lines.append(f"  （另有 {len(empty)} 种已清零，回复「零库存」查看）")
        if live:
            lines.append("")
            lines.append("物品清单：")
            lines.extend(_numbered(live))
        return "\n".join(lines), {"items": _payload(live)}

    def _total(self) -> tuple[str, dict]:
        counts = self.repo.overview_counts()
        records = self.repo.all_items()
        live, empty = _split_stock(records)
        lines = [
            f"库存共 {len(live)} 种物品，合计 {fmt_qty(sum(_quantity_of(r) for r in live))} 件，"
            f"分布在 {counts['location_count']} 个位置"
        ]
        if empty:
            lines.append(f"（另有 {len(empty)} 种已清零）")
        if live:
            lines.append("")
            lines.append("物品清单：")
            lines.extend(_numbered(live))
        return "\n".join(lines), {"counts": counts, "items": _payload(live)}

    def _count(self, keyword: str) -> tuple[str, dict]:
        response = self.inventory.search(keyword, limit=5)
        hits = response.hits
        if not hits:
            return f"没找到和「{keyword}」相关的物品", {"keyword": keyword, "items": []}
        lines = ["找到这些："]
        lines.extend(_numbered(hits))
        return "\n".join(lines), {"keyword": keyword, "items": _payload(hits)}

    def _where(self, keyword: str) -> tuple[str, dict]:
        response = self.inventory.search(keyword, limit=5)
        hits = response.hits
        if not hits:
            return f"没找到和「{keyword}」相关的物品", {"keyword": keyword, "items": []}
        lines = [
            f"  {index}. {hit.name}" + (f"（{hit.spec}）" if hit.spec else "")
            + f" → {hit.location or '（未记录位置）'}（{fmt_qty(hit.quantity)} 件）"
            for index, hit in enumerate(hits, start=1)
        ]
        return "\n".join(lines), {"keyword": keyword, "items": _payload(hits)}

    def _attribute(self, keyword: str, field: str) -> tuple[str, dict]:
        response = self.inventory.search(keyword, limit=3)
        hits = response.hits
        if not hits:
            return f"没找到和「{keyword}」相关的物品", {"keyword": keyword}
        lines = []
        for hit in hits:
            if field == "spec":
                lines.append(f"{hit.name} 的规格：{hit.spec or '（未记录）'}")
            else:
                lines.append(f"{hit.name} 的别名：{'、'.join(hit.aliases) if hit.aliases else '（无）'}")
        return "\n".join(lines), {"keyword": keyword, "field": field, "items": _payload(hits)}

    def _category_list(self, category: str) -> tuple[str, dict]:
        total, records = self.repo.list_items(category=category, limit=1000)
        label = category_label(category)
        if not total:
            return f"「{label}」下还没有物品", {"category": category, "items": []}
        live, empty = _split_stock(records)
        if not live:
            return f"「{label}」下的物品已全部清零（{total} 种）", {"category": category, "total": total, "items": []}
        lines = [f"「{label}」在库 {len(live)} 种物品："]
        lines.extend(_numbered(live))
        if empty:
            lines.append(f"  （另有 {len(empty)} 种已清零）")
        return "\n".join(lines), {"category": category, "total": len(live), "items": _payload(live)}

    def _location_list(self, location: str) -> tuple[str, dict]:
        if not location:
            return "请告诉我要查哪个位置，例如「A柜里有什么」", {}
        total, records = self.repo.list_items(location=location, limit=1000)
        if not total:
            return f"「{location}」下没有记录", {"location": location, "items": []}
        live, empty = _split_stock(records)
        if not live:
            return f"「{location}」下的物品已全部清零（{total} 种）", {"location": location, "items": []}
        lines = [f"「{location}」在库 {len(live)} 种物品："]
        lines.extend(_numbered(live))
        if empty:
            lines.append(f"  （另有 {len(empty)} 种已清零）")
        return "\n".join(lines), {"location": location, "total": len(live), "items": _payload(live)}

    def _list(self) -> tuple[str, dict]:
        total, records = self.repo.list_items(limit=1000)
        if not total:
            return "库存还是空的，先入库吧", {"total": 0, "items": []}
        live, empty = _split_stock(records)
        if not live:
            return (
                f"当前没有在库物品（{total} 种都已是 0）。回复「零库存」查看，或用「清理零库存」删掉记录。",
                {"total": 0, "items": []},
            )
        lines = [f"在库共 {len(live)} 种物品："]
        lines.extend(_numbered(live))
        if empty:
            lines.append(f"  （另有 {len(empty)} 种已清零）")
        return "\n".join(lines), {"total": len(live), "items": _payload(live)}

    def _categories(self) -> tuple[str, dict]:
        stats = self.analysis.categories()
        if not stats:
            return "库存还是空的", {"items": []}
        lines: list[str] = []
        all_live: list[Any] = []
        for stat in stats:
            _, records = self.repo.list_items(category=stat.category, limit=1000)
            live, empty = _split_stock(records)
            all_live.extend(live)
            suffix = f"（另有 {len(empty)} 种已清零）" if empty else ""
            lines.append(f"【{stat.label}】在库 {len(live)} 种 / {fmt_qty(sum(_quantity_of(r) for r in live))} 件{suffix}")
            lines.extend(_numbered(live))
            lines.append("")
        return "\n".join(lines).rstrip(), {
            "categories": [s.model_dump() for s in stats],
            "items": _payload(all_live),
        }

    def _low_stock(self, threshold: float) -> tuple[str, dict]:
        items = self.analysis.low_stock(threshold, limit=10)
        if not items:
            return f"没有库存低于 {threshold:g} 的物品", {"threshold": threshold, "items": []}
        lines = [f"库存不足（≤ {threshold:g}）的物品："]
        lines.extend(_numbered(items))
        return "\n".join(lines), {"threshold": threshold, "items": _payload(items)}

    def _search(self, keyword: str) -> tuple[str, dict]:
        response = self.inventory.search(keyword, limit=8)
        hits = response.hits
        if not hits:
            return (
                f"没找到和「{keyword}」相关的物品。\n"
                "可以试试更短的关键词，或者用「入库 名称 数量 @位置」先建条目",
                {"keyword": keyword, "items": []},
            )
        lines = ["找到这些："]
        lines.extend(_numbered(hits))
        return "\n".join(lines), {"keyword": keyword, "items": _payload(hits)}

    # ------------------------------------------------------------------ LLM 兜底
    async def _ask_llm(self, question: str) -> str | None:
        assert self.llm is not None  # noqa: S101
        inventory_text = self._inventory_digest()
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个仓储库存助手。只能依据下面提供的库存数据回答，"
                    "不要编造不存在的物品或数量。数据里没有的就直说没有记录。回答要简短，适合 QQ 聊天。\n\n"
                    f"【当前库存数据】\n{inventory_text}"
                ),
            },
            {"role": "user", "content": question},
        ]
        try:
            result = await self.llm.chat(messages, max_tokens=500)
            return result["content"].strip() or None
        except Exception:  # noqa: BLE001 - 兜底失败不应该影响主流程
            logger.exception("大模型兜底回答失败")
            return None

    def _inventory_digest(self, limit: int = 120) -> str:
        records = self.repo.all_items()[:limit]
        if not records:
            return "（空）"
        return "\n".join(
            f"- {r.name}{f'（{r.spec}）' if r.spec else ''} × {fmt_qty(r.quantity)} @ {r.location or '未指定'}"
            for r in records
        )


__all__ = ["ITEM_LIMIT", "NLQService", "PATTERNS", "truncate"]
