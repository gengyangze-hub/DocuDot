"""仓储层：所有 SQL 都集中在这里，上层服务不直接写 SQL。"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .core.fuzzy import MatchTarget
from .core.normalize import fold
from .db import Database
from .models import ItemOut
from .utils import fmt_qty, now_iso

# --------------------------------------------------------------------------- #
# 记录
# --------------------------------------------------------------------------- #


@dataclass
class ItemRecord:
    id: int
    name: str
    category: str
    quantity: float
    unit: str
    location: str
    spec: str
    note: str
    created_at: str
    updated_at: str
    created_by: str
    updated_by: str
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row, aliases: Sequence[str] = ()) -> "ItemRecord":
        return cls(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            quantity=float(row["quantity"]),
            unit=row["unit"],
            location=row["location"],
            spec=row["spec"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by=row["created_by"],
            updated_by=row["updated_by"],
            aliases=list(aliases),
        )

    def to_out(self) -> ItemOut:
        return ItemOut(
            id=self.id,
            name=self.name,
            category=self.category,  # type: ignore[arg-type]
            quantity=self.quantity,
            unit=self.unit,
            location=self.location,
            spec=self.spec,
            note=self.note,
            aliases=self.aliases,
            created_at=self.created_at,
            updated_at=self.updated_at,
            created_by=self.created_by,
            updated_by=self.updated_by,
        )

    def to_target(self) -> MatchTarget:
        return MatchTarget(
            id=self.id,
            name=self.name,
            aliases=self.aliases,
            spec=self.spec,
            category=self.category,
            location=self.location,
            note=self.note,
        )

    @property
    def quantity_text(self) -> str:
        return fmt_qty(self.quantity)


def _keys(name: str, location: str, spec: str) -> tuple[str, str, str]:
    return fold(name), fold(location), fold(spec)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """``whk_`` 前缀 + 43 字符 URL-safe 随机串。"""
    return "whk_" + secrets.token_urlsafe(32)


# --------------------------------------------------------------------------- #
# 仓储
# --------------------------------------------------------------------------- #

ITEM_COLUMNS = (
    "id, name, name_key, category, quantity, unit, location, location_key, "
    "spec, spec_key, note, created_at, updated_at, created_by, updated_by"
)


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ================================================================== 物品
    def get_item(self, item_id: int) -> ItemRecord | None:
        row = self.db.query_one(f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,))
        if not row:
            return None
        return ItemRecord.from_row(row, self.aliases_for([item_id]).get(item_id, []))

    def get_item_by_key(self, name: str, location: str = "", spec: str = "") -> ItemRecord | None:
        name_key, location_key, spec_key = _keys(name, location, spec)
        row = self.db.query_one(
            f"SELECT {ITEM_COLUMNS} FROM items WHERE name_key = ? AND location_key = ? AND spec_key = ?",
            (name_key, location_key, spec_key),
        )
        if not row:
            return None
        return ItemRecord.from_row(row, self.aliases_for([row["id"]]).get(row["id"], []))

    def list_items(
        self,
        *,
        category: str | None = None,
        location: str | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "updated_at DESC, id DESC",
    ) -> tuple[int, list[ItemRecord]]:
        where: list[str] = []
        params: list[Any] = []
        if category:
            where.append("category = ?")
            params.append(category)
        if location:
            where.append("location_key = ?")
            params.append(fold(location))
        if keyword:
            where.append("(name LIKE ? OR name_key LIKE ? OR location LIKE ? OR spec LIKE ? OR note LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, f"%{fold(keyword)}%", like, like, like])
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total = int(self.db.scalar(f"SELECT COUNT(*) FROM items {clause}", params, 0) or 0)
        rows = self.db.query(
            f"SELECT {ITEM_COLUMNS} FROM items {clause} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        records = [ItemRecord.from_row(row) for row in rows]
        alias_map = self.aliases_for([r.id for r in records])
        for record in records:
            record.aliases = alias_map.get(record.id, [])
        return total, records

    def all_items(self, category: str | None = None) -> list[ItemRecord]:
        where = "WHERE category = ?" if category else ""
        params: tuple[Any, ...] = (category,) if category else ()
        rows = self.db.query(f"SELECT {ITEM_COLUMNS} FROM items {where} ORDER BY id", params)
        records = [ItemRecord.from_row(row) for row in rows]
        alias_map = self.aliases_for([r.id for r in records])
        for record in records:
            record.aliases = alias_map.get(record.id, [])
        return records

    def get_items(self, item_ids: Sequence[int]) -> dict[int, ItemRecord]:
        """按 id 批量取记录（含别名）。比 ``all_items()`` 再筛便宜得多。"""
        ids = [int(item_id) for item_id in item_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query(
            f"SELECT {ITEM_COLUMNS} FROM items WHERE id IN ({placeholders})", tuple(ids)
        )
        records = [ItemRecord.from_row(row) for row in rows]
        alias_map = self.aliases_for([r.id for r in records])
        for record in records:
            record.aliases = alias_map.get(record.id, [])
        return {record.id: record for record in records}

    def category_names(self) -> list[str]:
        """库里出现过的分类名（走 idx_items_category，不读整表）。"""
        rows = self.db.query(
            "SELECT DISTINCT category FROM items WHERE category <> '' ORDER BY category"
        )
        return [row["category"] for row in rows]

    def match_targets(self, category: str | None = None) -> list[MatchTarget]:
        return [record.to_target() for record in self.all_items(category)]

    def create_item(
        self,
        *,
        name: str,
        category: str = "other",
        quantity: float = 0,
        unit: str = "",
        location: str = "",
        spec: str = "",
        note: str = "",
        aliases: Iterable[str] = (),
        operator: str = "",
        timestamp: str | None = None,
    ) -> ItemRecord:
        ts = timestamp or now_iso()
        name_key, location_key, spec_key = _keys(name, location, spec)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO items (name, name_key, category, quantity, unit, location, location_key, "
                "spec, spec_key, note, created_at, updated_at, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, name_key, category, quantity, unit, location, location_key,
                    spec, spec_key, note, ts, ts, operator, operator,
                ),
            )
            item_id = int(cursor.lastrowid or 0)
            self._replace_aliases(conn, item_id, aliases, source="create", operator=operator, timestamp=ts)
        record = self.get_item(item_id)
        assert record is not None  # noqa: S101 - 刚插入必然存在
        return record

    def update_item(
        self,
        item_id: int,
        *,
        fields: dict[str, Any],
        operator: str = "",
        timestamp: str | None = None,
    ) -> ItemRecord | None:
        current = self.get_item(item_id)
        if not current:
            return None
        ts = timestamp or now_iso()

        # 注意：``fields`` 里可能出现显式的 None（例如只改别名的 PATCH）。
        # 用 ``fields.get(key, default)`` 会把 None 当成新值写进 NOT NULL 列，
        # 因此这里统一按「None = 不改」处理。
        def _pick(key: str, fallback: str) -> str:
            value = fields.get(key)
            return fallback if value is None else str(value)

        name = fields.get("name") or current.name  # 名称不允许被清空
        location = _pick("location", current.location)
        spec = _pick("spec", current.spec)
        name_key, location_key, spec_key = _keys(name, location, spec)

        assignments = ["name = ?", "name_key = ?", "location = ?", "location_key = ?",
                       "spec = ?", "spec_key = ?", "updated_at = ?", "updated_by = ?"]
        params: list[Any] = [name, name_key, location, location_key, spec, spec_key, ts, operator]
        for column in ("category", "quantity", "unit", "note"):
            if column in fields and fields[column] is not None:
                assignments.append(f"{column} = ?")
                params.append(fields[column])
        params.append(item_id)

        with self.db.transaction() as conn:
            conn.execute(f"UPDATE items SET {', '.join(assignments)} WHERE id = ?", params)
            if fields.get("aliases") is not None:
                self._replace_aliases(
                    conn, item_id, fields["aliases"], source="update", operator=operator, timestamp=ts
                )
        return self.get_item(item_id)

    def set_quantity(self, item_id: int, quantity: float, *, operator: str = "", timestamp: str | None = None) -> None:
        self.db.execute(
            "UPDATE items SET quantity = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (quantity, timestamp or now_iso(), operator, item_id),
        )

    def delete_item(self, item_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.execute("DELETE FROM item_aliases WHERE item_id = ?", (item_id,))
            return cursor.rowcount > 0

    def delete_by_category(self, category: str) -> int:
        with self.db.transaction() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM items WHERE category = ?", (category,))]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM item_aliases WHERE item_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM stock_movements WHERE item_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM items WHERE id IN ({placeholders})", ids)
            return len(ids)

    # ================================================================== 别名
    def aliases_for(self, item_ids: Sequence[int]) -> dict[int, list[str]]:
        if not item_ids:
            return {}
        placeholders = ",".join("?" for _ in item_ids)
        rows = self.db.query(
            f"SELECT item_id, alias FROM item_aliases WHERE item_id IN ({placeholders}) ORDER BY id",
            list(item_ids),
        )
        result: dict[int, list[str]] = {item_id: [] for item_id in item_ids}
        for row in rows:
            result.setdefault(row["item_id"], []).append(row["alias"])
        return result

    def _replace_aliases(
        self,
        conn: sqlite3.Connection,
        item_id: int,
        aliases: Iterable[str],
        *,
        source: str,
        operator: str,
        timestamp: str,
    ) -> None:
        conn.execute("DELETE FROM item_aliases WHERE item_id = ?", (item_id,))
        seen: set[str] = set()
        for alias in aliases:
            text = str(alias).strip()
            key = fold(text)
            if not text or not key or key in seen:
                continue
            seen.add(key)
            conn.execute(
                "INSERT OR IGNORE INTO item_aliases (item_id, alias, alias_key, source, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, text, key, source, timestamp, operator),
            )

    def add_aliases(self, item_id: int, aliases: Iterable[str], *, source: str = "manual", operator: str = "") -> list[str]:
        ts = now_iso()
        with self.db.transaction() as conn:
            for alias in aliases:
                text = str(alias).strip()
                key = fold(text)
                if not text or not key:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO item_aliases (item_id, alias, alias_key, source, created_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (item_id, text, key, source, ts, operator),
                )
        return self.aliases_for([item_id]).get(item_id, [])

    def remove_alias(self, item_id: int, alias: str) -> bool:
        return self.db.execute(
            "DELETE FROM item_aliases WHERE item_id = ? AND alias_key = ?", (item_id, fold(alias))
        ) > 0

    def alias_conflicts(self, alias: str, exclude_item_id: int | None = None) -> list[tuple[int, str]]:
        """同一个别名被多个条目占用时，写操作需要提示用户消歧。"""
        rows = self.db.query(
            "SELECT a.item_id, i.name FROM item_aliases a JOIN items i ON i.id = a.item_id "
            "WHERE a.alias_key = ? AND a.item_id != ?",
            (fold(alias), exclude_item_id or -1),
        )
        return [(row["item_id"], row["name"]) for row in rows]

    # ================================================================== 流水
    def record_movement(
        self,
        *,
        item_id: int,
        item_name: str,
        action: str,
        delta: float,
        quantity_before: float,
        quantity_after: float,
        location: str = "",
        operator: str = "",
        source: str = "api",
        raw_text: str = "",
        note: str = "",
        timestamp: str | None = None,
    ) -> int:
        return self.db.execute(
            "INSERT INTO stock_movements (item_id, item_name, action, delta, quantity_before, quantity_after, "
            "location, operator, source, raw_text, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id, item_name, action, delta, quantity_before, quantity_after,
                location, operator, source, raw_text, note, timestamp or now_iso(),
            ),
        )

    def list_movements(
        self,
        *,
        item_id: int | None = None,
        operator: str | None = None,
        source: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[sqlite3.Row]]:
        where: list[str] = []
        params: list[Any] = []
        if item_id is not None:
            where.append("item_id = ?")
            params.append(item_id)
        if operator:
            where.append("operator = ?")
            params.append(operator)
        if source:
            where.append("source = ?")
            params.append(source)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM stock_movements {clause}", params, 0) or 0)
        rows = self.db.query(
            f"SELECT * FROM stock_movements {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return total, rows

    # ================================================================== 审计
    def audit(
        self,
        *,
        action: str,
        actor: str = "",
        actor_kind: str = "api",
        target_type: str = "",
        target_id: str | int = "",
        detail: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> int:
        return self.db.execute(
            "INSERT INTO audit_log (actor, actor_kind, action, target_type, target_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                actor, actor_kind, action, target_type, str(target_id),
                json.dumps(detail or {}, ensure_ascii=False), timestamp or now_iso(),
            ),
        )

    def list_audit(self, *, limit: int = 50, offset: int = 0) -> tuple[int, list[sqlite3.Row]]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM audit_log", (), 0) or 0)
        rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return total, rows

    # ================================================================== API Key
    def create_api_key(
        self,
        *,
        label: str = "",
        scopes: Sequence[str] = ("read",),
        created_by: str = "",
        expires_at: str | None = None,
        raw_key: str | None = None,
    ) -> tuple[str, int]:
        raw = raw_key or generate_api_key()
        ts = now_iso()
        key_id = self.db.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, label, scopes, enabled, created_at, created_by, expires_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (hash_api_key(raw), raw[:12], label, ",".join(scopes), ts, created_by, expires_at),
        )
        return raw, key_id

    def get_api_key(self, raw_key: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM api_keys WHERE key_hash = ?", (hash_api_key(raw_key),))

    def list_api_keys(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM api_keys ORDER BY id")

    def count_api_keys(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM api_keys WHERE enabled = 1 AND revoked_at IS NULL", (), 0) or 0)

    def revoke_api_key(self, key_id: int) -> bool:
        return self.db.execute(
            "UPDATE api_keys SET enabled = 0, revoked_at = ? WHERE id = ?", (now_iso(), key_id)
        ) > 0

    def touch_api_key(self, key_id: int) -> None:
        self.db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), key_id))

    # ================================================================== 导入批次
    def create_import_batch(
        self,
        *,
        filename: str,
        fmt: str,
        mode: str,
        status: str,
        total: int,
        created_items: int,
        updated_items: int,
        skipped: int,
        message: str = "",
        operator: str = "",
        source: str = "api",
    ) -> int:
        return self.db.execute(
            "INSERT INTO import_batches (filename, fmt, mode, status, total, created_items, updated_items, "
            "skipped, message, operator, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                filename, fmt, mode, status, total, created_items, updated_items,
                skipped, message, operator, source, now_iso(),
            ),
        )

    def list_import_batches(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM import_batches ORDER BY id DESC LIMIT ?", (limit,))

    # ================================================================== 统计
    def overview_counts(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS item_count, COALESCE(SUM(quantity), 0) AS total_quantity, "
            "COUNT(DISTINCT location_key) AS location_count, COUNT(DISTINCT category) AS category_count "
            "FROM items"
        )
        alias_count = int(self.db.scalar("SELECT COUNT(*) FROM item_aliases", (), 0) or 0)
        movement_count = int(self.db.scalar("SELECT COUNT(*) FROM stock_movements", (), 0) or 0)
        return {
            "item_count": int(row["item_count"]) if row else 0,
            "total_quantity": float(row["total_quantity"]) if row else 0.0,
            "location_count": int(row["location_count"]) if row else 0,
            "category_count": int(row["category_count"]) if row else 0,
            "alias_count": alias_count,
            "movement_count": movement_count,
        }

    def category_stats(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT category, COUNT(*) AS item_count, COALESCE(SUM(quantity), 0) AS total_quantity, "
            "COUNT(DISTINCT location_key) AS distinct_locations "
            "FROM items GROUP BY category ORDER BY item_count DESC"
        )

    def location_stats(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT location, COUNT(*) AS item_count, COALESCE(SUM(quantity), 0) AS total_quantity "
            "FROM items GROUP BY location_key ORDER BY item_count DESC, location LIMIT ?",
            (limit,),
        )

    def location_count(self, location: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM items WHERE location_key = ?", (fold(location),), 0) or 0)

    def low_stock(self, threshold: float, limit: int = 20) -> list[ItemRecord]:
        rows = self.db.query(
            f"SELECT {ITEM_COLUMNS} FROM items WHERE quantity <= ? ORDER BY quantity ASC, id ASC LIMIT ?",
            (threshold, limit),
        )
        records = [ItemRecord.from_row(row) for row in rows]
        alias_map = self.aliases_for([r.id for r in records])
        for record in records:
            record.aliases = alias_map.get(record.id, [])
        return records
