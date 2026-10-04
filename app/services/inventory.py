"""库存业务：检索、出入库、消歧、格式化。

模糊匹配的调用策略
------------------
1. ``item_id`` 直接定位
2. 「名称 + 位置 + 规格」精确键
3. 名称精确、别名精确
4. 分层模糊匹配（:mod:`app.core.fuzzy`）
5. 分数接近时**不猜**，返回候选让调用方消歧（除非显式 ``allow_ambiguous``）
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable, Sequence

from ..config import Settings
from ..core.fuzzy import MatchTarget, SearchHit, best_match, search_targets
from ..core.normalize import fold, tokenize_query
from ..errors import Ambiguous, LocationConflict, NotFound, ValidationFailed
from ..models import (
    ItemCreate,
    ItemOut,
    ItemUpdate,
    MergeResult,
    MovementOut,
    SearchHitOut,
    SearchResponse,
    StockAction,
    StockChangeRequest,
    StockChangeResult,
)
from ..repository import ItemRecord, Repository
from ..utils import fmt_qty, now_iso
from .categorize import category_code, guess_category

logger = logging.getLogger(__name__)


def _is_type_only(hit: SearchHit) -> bool:
    """这一分是不是纯靠「类型相同」拿到的。

    「排针」和「杜邦线」都是连接器，类型归约命中 0.9 —— 但这只能说明
    **属于同一类**，不能说明是同一条记录。拿这种证据去合并库存或记别名，
    会把完全不同的东西混到一起，所以这里把它降级为「没有把握」。
    """
    return bool(hit.reasons) and all("类型" in reason for reason in hit.reasons)


class InventoryService:
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings
        #: 检索语料缓存：``{分类: (库版本, 目标列表)}``。
        #: 模糊匹配要遍历全库，2000 条时每次查询重建要 700ms —— 而查询远多于写入。
        #: 版本号来自 ``Database.revision``（任何写事务提交都会 +1），
        #: 所以缓存绝不会比库旧。
        self._target_cache: dict[str | None, tuple[int, list[MatchTarget]]] = {}
        #: 库存写操作的互斥锁。
        #:
        #: SQLite 只保证**单条语句**原子，而 ``change_stock`` 是
        #: 「先读当前数量 → 算出新值 → 写回」三步，跨了两个事务。
        #: 两个并发请求会读到同一个旧值再各写各的 —— 直接丢更新
        #: （实测 6 线程 × 5 次入库，期望 30 只得到 13）。
        #: 单进程部署下进程内锁就够；若将来跑多 worker，需要换成数据库层乐观锁。
        self._write_lock = threading.RLock()

    def _targets(self, category: str | None = None) -> list[MatchTarget]:
        revision = self.repo.db.revision
        cached = self._target_cache.get(category)
        if cached is not None and cached[0] == revision:
            return cached[1]
        targets = self.repo.match_targets(category=category)
        self._target_cache = {category: (revision, targets)}   # 只留最近一次，避免无界增长
        return targets

    # ================================================================== 读
    def get_item(self, item_id: int) -> ItemRecord:
        record = self.repo.get_item(item_id)
        if not record:
            raise NotFound(f"物品 #{item_id} 不存在")
        return record

    def list_items(
        self,
        *,
        category: str | None = None,
        location: str | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[ItemRecord]]:
        return self.repo.list_items(
            category=category, location=location, keyword=keyword, limit=limit, offset=offset
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        threshold: float | None = None,
        category: str | None = None,
        any_mode: bool = False,
    ) -> SearchResponse:
        threshold = self.settings.fuzzy_threshold if threshold is None else threshold
        targets = self._targets(category)
        hits = search_targets(query, targets, threshold=threshold, limit=limit, any_mode=any_mode)
        return SearchResponse(
            query=query,
            tokens=tokenize_query(query),
            hits=self.enrich(hits),
            total=len(hits),
            threshold=threshold,
        )

    def search_all(self, query: str, *, category: str | None = None) -> list[ItemRecord]:
        """模糊搜索的**全量**结果，不截断。

        破坏性批量操作（``出库全部电容``）必须用这个：``search()`` 默认只返回
        top-N，拿来清零会静默漏掉一部分，却上报「全部完成」。
        """
        targets = self._targets(category)
        if not targets:
            return []
        hits = search_targets(
            query,
            targets,
            threshold=self.settings.fuzzy_threshold,
            limit=len(targets),
        )
        records = self.repo.get_items([hit.target_id for hit in hits])
        return [records[hit.target_id] for hit in hits if hit.target_id in records]

    def enrich(self, hits: Sequence[SearchHit]) -> list[SearchHitOut]:
        """把匹配结果补上库存信息，方便直接渲染。"""
        if not hits:
            return []
        # 只取命中的那几条 —— 以前是 all_items() 整表再筛，2000 条时要几十毫秒
        records = self.repo.get_items([hit.target_id for hit in hits])
        result: list[SearchHitOut] = []
        for hit in hits:
            record = records.get(hit.target_id)
            if not record:
                continue
            result.append(
                SearchHitOut(
                    id=record.id,
                    name=record.name,
                    score=hit.score,
                    quantity=record.quantity,
                    location=record.location,
                    spec=record.spec,
                    aliases=record.aliases,
                    reasons=hit.reasons,
                )
            )
        return result

    # ================================================================== 解析
    def resolve(
        self,
        name: str,
        *,
        location: str | None = None,
        spec: str | None = None,
        threshold: float | None = None,
        allow_ambiguous: bool = False,
        category: str | None = None,
    ) -> tuple[ItemRecord | None, float | None, list[SearchHitOut], str | None]:
        """把用户给的名字解析成唯一的库存记录。

        返回 ``(记录, 分数, 候选, 命中方式)``；记录为 ``None`` 表示没找到或需要消歧。
        """
        threshold = self.settings.fuzzy_threshold if threshold is None else threshold
        name = (name or "").strip()
        if not name:
            return None, None, [], None

        # ---- 1. 精确键（名称 + 位置 + 规格） ----
        if location is not None or spec is not None:
            exact = self.repo.get_item_by_key(name, location or "", spec or "")
            if exact:
                return exact, 1.0, [], "exact-key"

        # ---- 2. 名称精确 ----
        name_key = fold(name)
        matches = [r for r in self.repo.all_items(category) if fold(r.name) == name_key]
        if len(matches) == 1:
            return matches[0], 1.0, [], "exact-name"
        if len(matches) > 1:
            narrowed = self._narrow(matches, location, spec)
            if narrowed:
                return narrowed, 1.0, [], "exact-name"
            hits = [self._hit(record, 1.0, "名称完全一致") for record in matches]
            if allow_ambiguous:
                return matches[0], 1.0, hits, "exact-name"
            raise Ambiguous(
                f"「{name}」在 {len(matches)} 个位置都有库存，请指定位置",
                detail={"candidates": [h.model_dump() for h in hits]},
            )

        # ---- 3. 别名精确 ----
        alias_rows = self.repo.db.query(
            "SELECT a.item_id FROM item_aliases a WHERE a.alias_key = ?", (name_key,)
        )
        alias_ids = [row["item_id"] for row in alias_rows]
        if alias_ids:
            records = [r for r in self.repo.all_items(category) if r.id in set(alias_ids)]
            if len(records) == 1:
                return records[0], 1.0, [], "exact-alias"
            narrowed = self._narrow(records, location, spec)
            if narrowed:
                return narrowed, 1.0, [], "exact-alias"
            hits = [self._hit(record, 1.0, "别名完全一致") for record in records]
            if allow_ambiguous:
                return records[0], 1.0, hits, "exact-alias"
            raise Ambiguous(
                f"「{name}」是 {len(records)} 个条目的别名，请说得更具体",
                detail={"candidates": [h.model_dump() for h in hits]},
            )

        # ---- 4. 分层模糊匹配 ----
        targets = self._targets(category)
        target, score, hits = best_match(name, targets, threshold=threshold)
        # 纯类型级命中（「排针」vs「杜邦线」都是连接器）只说明同类，
        # 不能认定是同一条 —— 标记出来交给调用方决定怎么处理。
        weak_type = bool(target is not None and hits and _is_type_only(hits[0]))
        enriched = self.enrich(hits)
        if target is None:
            if hits:
                if not allow_ambiguous:
                    raise Ambiguous(
                        f"「{name}」可能指多个物品，请确认是哪一个",
                        detail={"candidates": [h.model_dump() for h in enriched]},
                    )
                top = self.repo.get_item(hits[0].target_id)
                return top, hits[0].score, enriched, "fuzzy"
            return None, score or None, enriched, None
        record = self.repo.get_item(target.id)
        if record and location is not None and record.location != location:
            # 指定了位置但命中的记录在别处 —— 交给调用方决定是否新建
            return None, score, enriched, None
        return record, score, enriched, "weak-type" if weak_type else "fuzzy"

    def _narrow(self, records: Sequence[ItemRecord], location: str | None, spec: str | None) -> ItemRecord | None:
        candidates = list(records)
        if location is not None:
            location_key = fold(location)
            filtered = [r for r in candidates if fold(r.location) == location_key]
            if filtered:
                candidates = filtered
            else:
                return None
        if spec is not None:
            spec_key = fold(spec)
            filtered = [r for r in candidates if fold(r.spec) == spec_key]
            if filtered:
                candidates = filtered
            else:
                return None
        return candidates[0] if len(candidates) == 1 else None

    def _hit(self, record: ItemRecord, score: float, reason: str) -> SearchHitOut:
        return SearchHitOut(
            id=record.id,
            name=record.name,
            score=score,
            quantity=record.quantity,
            location=record.location,
            spec=record.spec,
            aliases=record.aliases,
            reasons=[reason],
        )

    # ================================================================== 写
    def create_item(self, payload: ItemCreate) -> ItemRecord:
        """建档入口：检查重名 → 插入 必须原子，否则并发建档会撞唯一索引。"""
        with self._write_lock:
            return self._create_item_locked(payload)

    def _create_item_locked(self, payload: ItemCreate) -> ItemRecord:
        existing = self.repo.get_item_by_key(payload.name, payload.location, payload.spec)
        if existing:
            raise ValidationFailed(
                f"「{payload.name}」在「{payload.location or '未指定位置'}」已存在（#{existing.id}），"
                "请改用入库操作"
            )
        record = self.repo.create_item(
            name=payload.name,
            category=category_code(payload.category),
            quantity=payload.quantity,
            unit=payload.unit,
            location=payload.location,
            spec=payload.spec,
            note=payload.note,
            aliases=payload.aliases,
            operator=payload.operator,
        )
        self.repo.record_movement(
            item_id=record.id,
            item_name=record.name,
            action="create",
            delta=record.quantity,
            quantity_before=0,
            quantity_after=record.quantity,
            location=record.location,
            operator=payload.operator,
            source="api",
            note="新建库存条目",
        )
        self.repo.audit(
            action="item.create",
            actor=payload.operator,
            target_type="item",
            target_id=record.id,
            detail={"name": record.name, "quantity": record.quantity, "location": record.location},
        )
        return record

    def update_item(self, item_id: int, payload: ItemUpdate) -> ItemRecord:
        record = self.get_item(item_id)
        fields = payload.model_dump(exclude_unset=True, exclude={"operator"})
        if not fields:
            return record
        if "category" in fields and fields["category"] is not None:
            fields["category"] = fields["category"].value if hasattr(fields["category"], "value") else fields["category"]
        updated = self.repo.update_item(item_id, fields=fields, operator=payload.operator)
        if not updated:
            raise NotFound(f"物品 #{item_id} 不存在")
        self.repo.audit(
            action="item.update",
            actor=payload.operator,
            target_type="item",
            target_id=item_id,
            detail=fields,
        )
        return updated

    def delete_item(self, item_id: int, *, operator: str = "") -> None:
        record = self.get_item(item_id)
        self.repo.audit(
            action="item.delete",
            actor=operator,
            target_type="item",
            target_id=item_id,
            detail={"name": record.name, "quantity": record.quantity, "location": record.location},
        )
        self.repo.delete_item(item_id)

    def merge_items(
        self,
        target_id: int,
        source_ids: list[int],
        *,
        operator: str = "",
        source: str = "api",
    ) -> MergeResult:
        """把若干条记录并进目标记录：数量相加、别名合并、被并入的记录删除。

        合并**不改变总库存**（各条数量之和不变），所以只写审计、不写库存流水。
        被并入记录的名称与别名都会留成别名 —— 合并后搜旧写法照样找得到。
        """
        with self._write_lock:
            return self._merge_items_locked(
                target_id, source_ids, operator=operator, source=source
            )

    def _merge_items_locked(
        self,
        target_id: int,
        source_ids: list[int],
        *,
        operator: str = "",
        source: str = "api",
    ) -> MergeResult:
        target = self.get_item(target_id)
        sources = [self.get_item(item_id) for item_id in source_ids if item_id != target_id]
        if not sources:
            raise ValidationFailed("没有要合并的记录")

        total = target.quantity
        spec = target.spec
        location = target.location or next((r.location for r in sources if r.location), "")
        notes = [target.note] if target.note else []
        aliases_added: list[str] = []

        for record in sources:
            total += record.quantity
            if not spec and record.spec:
                spec = record.spec
            for alias in [record.name, *record.aliases]:
                folded = fold(alias)
                if folded and folded != fold(target.name) and folded not in {fold(a) for a in aliases_added}:
                    aliases_added.append(alias)
            if record.location and location and fold(record.location) != fold(location):
                notes.append(f"另有一批在 {record.location}（{fmt_qty(record.quantity)} 件）")

        self.repo.update_item(
            target.id,
            fields={
                "quantity": total,
                "spec": spec,
                "location": location,
                "note": "；".join(part for part in notes if part),
            },
            operator=operator,
        )
        if aliases_added:
            self.repo.add_aliases(target.id, aliases_added, source="merge", operator=operator)

        for record in sources:
            self.repo.audit(
                action="item.merge",
                actor=operator,
                actor_kind="qq" if source == "qq" else "api",
                target_type="item",
                target_id=target.id,
                detail={
                    "merged_from": record.id,
                    "merged_name": record.name,
                    "quantity": record.quantity,
                    "spec": record.spec,
                    "location": record.location,
                },
            )
            self.repo.delete_item(record.id)

        final = self.repo.get_item(target.id) or target
        merged_names = [record.name for record in sources]
        message = (
            f"「{final.name}」现在 {fmt_qty(final.quantity)} 件"
            + (f" @ {final.location}" if final.location else "")
            + f"，并入了 {'、'.join(merged_names)}"
        )
        if aliases_added:
            message += f"（别名 +{'、'.join(aliases_added)}）"
        return MergeResult(
            target_id=final.id,
            target_name=final.name,
            quantity_after=final.quantity,
            merged_names=merged_names,
            aliases_added=aliases_added,
            message=message,
        )

    def add_aliases(self, item_id: int, aliases: Iterable[str], *, operator: str = "") -> ItemRecord:
        self.get_item(item_id)
        result = self.repo.add_aliases(item_id, aliases, source="manual", operator=operator)
        self.repo.audit(
            action="item.alias.add",
            actor=operator,
            target_type="item",
            target_id=item_id,
            detail={"aliases": list(aliases)},
        )
        record = self.repo.get_item(item_id)
        record.aliases = result
        return record  # type: ignore[return-value]

    def remove_alias(self, item_id: int, alias: str, *, operator: str = "") -> ItemRecord:
        self.get_item(item_id)
        removed = self.repo.remove_alias(item_id, alias)
        if not removed:
            raise NotFound(f"「{alias}」不是 #{item_id} 的别名")
        self.repo.audit(
            action="item.alias.remove",
            actor=operator,
            target_type="item",
            target_id=item_id,
            detail={"alias": alias},
        )
        return self.get_item(item_id)

    # ================================================================== 出入库
    def change_stock(self, req: StockChangeRequest) -> StockChangeResult:
        """库存变更入口：**读-改-写整体串行**（见 :attr:`_write_lock`）。"""
        with self._write_lock:
            return self._change_stock_locked(req)

    def _change_stock_locked(self, req: StockChangeRequest) -> StockChangeResult:
        record, score, candidates, matched_by = self._locate(req)
        created = False

        if matched_by == "weak-type" and record is not None:
            if req.action is StockAction.IN and req.auto_create:
                # 入库时「只是同类」不构成并入的理由 —— 按新条目建档
                record = None
                matched_by = None
                score = None
            else:
                raise Ambiguous(
                    f"「{req.name}」只是和「{record.name}」同类，无法确定是不是同一个物品",
                    detail={"candidates": [c.model_dump() for c in candidates]},
                )

        if record is None:
            if req.action is StockAction.OUT or req.action is StockAction.SET:
                raise NotFound(
                    f"没有找到「{req.name or req.item_id}」",
                    detail={"candidates": [c.model_dump() for c in candidates]},
                )
            if not req.auto_create:
                raise NotFound(
                    f"没有找到「{req.name}」；如需新建条目，请带上 auto_create=true",
                    detail={"candidates": [c.model_dump() for c in candidates]},
                )
            record = self._create_for_change(req)
            created = True
            matched_by = "created"
            score = None
        elif matched_by == "fuzzy" and score and score >= 0.9 and req.name:
            # 用户用另一种写法命中了已有条目（例如「100Ω电阻」命中「100欧姆电阻」）：
            # 把这个写法自动记成别名，下次就能精确命中，两种叫法从此归到一起。
            record = self._remember_alias(record, req.name, req.operator)

        # 入库已有物品，但填了不同的规格/封装 —— 那是**另一种东西**，不能并进同一条。
        # 但原记录**本来没规格**时，用户补一个属于「补全」，不是新品种。
        requested_spec = (req.spec or "").strip()
        if record is not None and req.action is StockAction.IN and requested_spec:
            if not record.spec:
                self.repo.update_item(record.id, fields={"spec": requested_spec}, operator=req.operator)
                self.repo.audit(
                    action="item.spec.fill",
                    actor=req.operator,
                    actor_kind="qq" if req.source == "qq" else "api",
                    target_type="item",
                    target_id=record.id,
                    detail={"spec": requested_spec},
                )
                record = self.repo.get_item(record.id) or record
            elif fold(requested_spec) != fold(record.spec):
                previous_spec = record.spec
                variant_location = (req.location or record.location).strip()
                variant = self.repo.get_item_by_key(record.name, variant_location, requested_spec)
                if variant:
                    record = variant
                else:
                    record = self.repo.create_item(
                        name=record.name,
                        category=record.category,
                        quantity=0,
                        location=variant_location,
                        spec=requested_spec,
                        note="",
                        aliases=record.aliases,
                        operator=req.operator,
                    )
                    created = True
                    matched_by = "created"
                    score = None
                self.repo.audit(
                    action="item.spec.variant",
                    actor=req.operator,
                    actor_kind="qq" if req.source == "qq" else "api",
                    target_type="item",
                    target_id=record.id,
                    detail={"from_spec": previous_spec, "to_spec": requested_spec},
                )

        # 入库已有物品，但填了一个不同的位置 —— 不能默默丢用户填的位置
        requested_location = (req.location or "").strip()
        if (
            record is not None
            and req.action is StockAction.IN
            and requested_location
            and fold(requested_location) != fold(record.location)
        ):
            if req.merge_location is None:
                # 交给调用方去问用户：合并到新位置，还是在同一个新位置另建一条
                raise LocationConflict(
                    f"「{record.name}」已有记录在「{record.location or '未指定位置'}」，"
                    f"与本次填的「{requested_location}」不同",
                    detail={
                        "item": {
                            "id": record.id,
                            "name": record.name,
                            "quantity": record.quantity,
                            "location": record.location,
                        },
                        "requested_location": requested_location,
                    },
                )
            if req.merge_location:
                # 合并：把原记录的位置改成新位置，只保留一条
                self.repo.update_item(
                    record.id, fields={"location": requested_location}, operator=req.operator
                )
                self.repo.audit(
                    action="item.location.merge",
                    actor=req.operator,
                    actor_kind="qq" if req.source == "qq" else "api",
                    target_type="item",
                    target_id=record.id,
                    detail={"from": record.location, "to": requested_location},
                )
                record = self.repo.get_item(record.id) or record
            else:
                # 分开：在目标位置另建一条，原记录不动（同一物品分仓存放）
                existing_here = self.repo.get_item_by_key(
                    record.name, requested_location, req.spec or record.spec
                )
                if existing_here:
                    record = existing_here
                else:
                    record = self.repo.create_item(
                        name=record.name,
                        category=record.category,
                        quantity=0,
                        location=requested_location,
                        spec=req.spec or record.spec,
                        note="",
                        aliases=record.aliases,
                        operator=req.operator,
                    )
                    self.repo.audit(
                        action="item.location.split",
                        actor=req.operator,
                        actor_kind="qq" if req.source == "qq" else "api",
                        target_type="item",
                        target_id=record.id,
                        detail={"split_from": record.id, "location": requested_location},
                    )

        before = record.quantity
        delta = self._compute_delta(req, before)
        after = before + delta

        if req.action is StockAction.OUT and after < 0:
            raise ValidationFailed(
                f"库存不足：「{record.name}」当前 {fmt_qty(before)}，本次要出库 {fmt_qty(req.quantity)}"
            )

        self.repo.set_quantity(record.id, after, operator=req.operator)
        self.repo.record_movement(
            item_id=record.id,
            item_name=record.name,
            action=req.action.value,
            delta=delta,
            quantity_before=before,
            quantity_after=after,
            location=record.location,
            operator=req.operator,
            source=req.source,
            raw_text=req.raw_text,
            note=req.note,
        )
        self.repo.audit(
            action=f"stock.{req.action.value}",
            actor=req.operator,
            actor_kind="qq" if req.source == "qq" else "api",
            target_type="item",
            target_id=record.id,
            detail={
                "quantity": req.quantity,
                "delta": delta,
                "before": before,
                "after": after,
                "raw_text": req.raw_text,
            },
        )
        refreshed = self.repo.get_item(record.id)
        assert refreshed is not None  # noqa: S101
        return StockChangeResult(
            ok=True,
            action=req.action.value,
            message=self.describe_change(refreshed, req.action, before, after, created),
            item=refreshed.to_out(),
            created=created,
            quantity_before=before,
            quantity_after=after,
            delta=delta,
            matched_by=matched_by,
            score=score,
            candidates=candidates if not created else [],
        )

    def _locate(
        self, req: StockChangeRequest
    ) -> tuple[ItemRecord | None, float | None, list[SearchHitOut], str | None]:
        if req.item_id is not None:
            return self.get_item(req.item_id), 1.0, [], "id"
        if not req.name:
            raise ValidationFailed("必须提供 item_id 或 name")
        # 入库时允许指定一个新位置（同一物品可能放在多个位置）；
        # 出库/盘点则要求位置必须对得上，避免扣错柜子。
        return self.resolve(
            req.name,
            location=req.location if req.action is not StockAction.IN else None,
            spec=req.spec,
            threshold=req.threshold,
            allow_ambiguous=req.allow_ambiguous,
        )

    def _remember_alias(self, record: ItemRecord, typed: str, operator: str) -> ItemRecord:
        """把用户这次用的写法记成别名（自动学习同义写法）。"""
        typed = (typed or "").strip()
        if not typed or fold(typed) == fold(record.name):
            return record
        if fold(typed) in {fold(alias) for alias in record.aliases}:
            return record
        self.repo.add_aliases(record.id, [typed], source="auto", operator=operator)
        self.repo.audit(
            action="item.alias.auto",
            actor=operator,
            target_type="item",
            target_id=record.id,
            detail={"alias": typed, "name": record.name},
        )
        logger.info("自动记录别名：%s → %s", typed, record.name)
        return self.repo.get_item(record.id) or record

    def _compute_delta(self, req: StockChangeRequest, before: float) -> float:
        if req.action is StockAction.IN:
            return req.quantity
        if req.action is StockAction.OUT:
            return -req.quantity
        return req.quantity - before  # set

    def _create_for_change(self, req: StockChangeRequest) -> ItemRecord:
        category = category_code(req.category) if req.category else guess_category(req.name or "")
        name = (req.name or "").strip()
        # 别名等于名称本身没有意义（AI 有时会回填），过滤掉
        aliases = [alias for alias in req.aliases if fold(alias) != fold(name)]
        return self.repo.create_item(
            name=name,
            category=category,
            quantity=0,
            location=req.location or "",
            spec=req.spec or "",
            note=req.note,
            aliases=aliases,
            operator=req.operator,
        )

    @staticmethod
    def describe_change(
        record: ItemRecord, action: StockAction, before: float, after: float, created: bool
    ) -> str:
        verb = {StockAction.IN: "入库", StockAction.OUT: "出库", StockAction.SET: "盘点"}[action]
        prefix = "新建并" if created else ""
        return (
            f"{prefix}{verb}成功：{record.name}"
            f"{f'（{record.spec}）' if record.spec else ''} "
            f"{fmt_qty(before)} → {fmt_qty(after)}"
            f"{f'，位置 {record.location}' if record.location else ''}"
        )

    # ================================================================== 批量
    def bulk_out(
        self,
        item_ids: Iterable[int],
        *,
        operator: str = "",
        source: str = "api",
        note: str = "批量出库（整批清零）",
    ) -> dict[str, Any]:
        """把一批物品的数量清零。

        用于「出库全部」「这些物品全部出库」「出库 <分类> 全部」这类整批操作。
        逐条写流水与审计，方便事后追溯。
        """
        changed = 0
        skipped = 0
        total = 0.0
        for item_id in item_ids:
            record = self.repo.get_item(item_id)
            if not record or record.quantity <= 0:
                skipped += 1
                continue
            before = record.quantity
            self.repo.set_quantity(record.id, 0, operator=operator)
            self.repo.record_movement(
                item_id=record.id,
                item_name=record.name,
                action="out",
                delta=-before,
                quantity_before=before,
                quantity_after=0,
                location=record.location,
                operator=operator,
                source=source,
                note=note,
            )
            self.repo.audit(
                action="stock.out",
                actor=operator,
                actor_kind="qq" if source == "qq" else "api",
                target_type="item",
                target_id=record.id,
                detail={"mode": "bulk_zero", "before": before, "after": 0, "note": note},
            )
            changed += 1
            total += before
        return {"count": changed, "total": total, "skipped": skipped}

    # ================================================================== 渲染
    @staticmethod
    def format_item_line(record: "ItemRecord | ItemOut", *, show_id: bool = True) -> str:
        parts = []
        if show_id:
            parts.append(f"#{record.id}")
        parts.append(record.name)
        # 名称里已经写了封装就不再重复（``10kΩ 0805`` + spec ``0805`` → 不显示两遍）
        if record.spec and fold(record.spec) not in fold(record.name):
            parts.append(f"({record.spec})")
        text = f"{' '.join(parts)} × {fmt_qty(record.quantity)}"
        if record.location:
            text += f" @ {record.location}"
        return text

    def movements(self, **kwargs: Any) -> tuple[int, list[MovementOut]]:
        total, rows = self.repo.list_movements(**kwargs)
        return total, [MovementOut(**dict(row)) for row in rows]

    def export_rows(self, category: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "name": record.name,
                "quantity": record.quantity,
                "location": record.location,
                "category": record.category,
                "spec": record.spec,
                "unit": record.unit,
                "aliases": record.aliases,
                "note": record.note,
                "updated_at": record.updated_at,
            }
            for record in self.repo.all_items(category=category)
        ]
