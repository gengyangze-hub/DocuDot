"""SQLite 数据层：连接管理、建表、迁移。

设计取舍
--------
* 每次操作开一条短连接（SQLite 打开成本极低），从而天然线程安全 ——
  FastAPI 的同步端点跑在线程池里，不会踩到 ``check_same_thread`` 的坑。
* 写操作统一走 ``transaction()``（``BEGIN IMMEDIATE``），
  避免并发写入时的 ``database is locked``。
* 开启 WAL，读写互不阻塞。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_SQL = """
-- ---------------------------------------------------------------- 元信息
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- 物品主表
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    name_key     TEXT    NOT NULL,
    category     TEXT    NOT NULL DEFAULT '未分类',
    quantity     REAL    NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    unit         TEXT    NOT NULL DEFAULT '',
    location     TEXT    NOT NULL DEFAULT '',
    location_key TEXT    NOT NULL DEFAULT '',
    spec         TEXT    NOT NULL DEFAULT '',
    spec_key     TEXT    NOT NULL DEFAULT '',
    note         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    created_by   TEXT    NOT NULL DEFAULT '',
    updated_by   TEXT    NOT NULL DEFAULT '',
    -- 同一「名称 + 位置 + 规格」视为同一条库存记录
    UNIQUE (name_key, location_key, spec_key)
);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_location ON items(location_key);
CREATE INDEX IF NOT EXISTS idx_items_name_key ON items(name_key);

-- ---------------------------------------------------------------- 别名 / 标签
CREATE TABLE IF NOT EXISTS item_aliases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    alias      TEXT    NOT NULL,
    alias_key  TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'manual',
    created_at TEXT    NOT NULL,
    created_by TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alias_item_key ON item_aliases(item_id, alias_key);
CREATE INDEX IF NOT EXISTS idx_alias_key ON item_aliases(alias_key);

-- ---------------------------------------------------------------- 出入库流水
CREATE TABLE IF NOT EXISTS stock_movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    item_name       TEXT    NOT NULL,
    action          TEXT    NOT NULL,           -- create/in/out/set/import/delete
    delta           REAL    NOT NULL DEFAULT 0,
    quantity_before REAL    NOT NULL DEFAULT 0,
    quantity_after  REAL    NOT NULL DEFAULT 0,
    location        TEXT    NOT NULL DEFAULT '',
    operator        TEXT    NOT NULL DEFAULT '',
    source          TEXT    NOT NULL DEFAULT 'api',   -- api/qq/import/cli
    raw_text        TEXT    NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_item ON stock_movements(item_id);
CREATE INDEX IF NOT EXISTS idx_movements_created ON stock_movements(created_at DESC);

-- ---------------------------------------------------------------- API Key
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT    NOT NULL UNIQUE,      -- sha256(明文)
    key_prefix   TEXT    NOT NULL,             -- 只存前 8 位便于识别
    label        TEXT    NOT NULL DEFAULT '',
    scopes       TEXT    NOT NULL DEFAULT 'read',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    created_by   TEXT    NOT NULL DEFAULT '',
    last_used_at TEXT,
    expires_at   TEXT,
    revoked_at   TEXT
);

-- ---------------------------------------------------------------- 审计
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT    NOT NULL DEFAULT '',
    actor_kind  TEXT    NOT NULL DEFAULT 'api',  -- api/qq/admin/system
    action      TEXT    NOT NULL,
    target_type TEXT    NOT NULL DEFAULT '',
    target_id   TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- ---------------------------------------------------------------- 导入批次
CREATE TABLE IF NOT EXISTS import_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT    NOT NULL,
    fmt           TEXT    NOT NULL,
    mode          TEXT    NOT NULL DEFAULT 'commit',
    status        TEXT    NOT NULL DEFAULT 'ok',
    total         INTEGER NOT NULL DEFAULT 0,
    created_items INTEGER NOT NULL DEFAULT 0,
    updated_items INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    message       TEXT    NOT NULL DEFAULT '',
    operator      TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT 'api',
    created_at    TEXT    NOT NULL
);
"""


class Database:
    """极简 SQLite 封装。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        #: 每次写事务提交都会 +1。上层用它判断「检索语料缓存」是否还有效 ——
        #: 保守策略：任何写入都让缓存失效，绝不漏。
        self.revision = 0
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 连接
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        # ⚠️ synchronous 是**每连接**设置：只在 initialize() 里设一次是不够的，
        # 之后每条新连接都会退回默认 FULL —— 每次 COMMIT 两次 fsync，
        # 实测建一条库存要 100ms（2000 条灌库 217s）。WAL + NORMAL 由日志保证
        # 崩溃一致性，代价只是断电时可能丢最后几个事务。
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：``BEGIN IMMEDIATE`` + 自动提交/回滚。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚失败时保持原异常
                logger.exception("回滚事务失败")
            raise
        else:
            # 任何写事务提交都可能改变检索语料，保守地让缓存失效
            self.revision += 1
        finally:
            conn.close()

    # ------------------------------------------------------------ 便捷查询
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid or cursor.rowcount

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self.transaction() as conn:
            cursor = conn.executemany(sql, rows)
            return cursor.rowcount

    # ------------------------------------------------------------ 初始化
    def initialize(self) -> None:
        """建表 + 迁移。幂等，可重复调用。"""
        self.path.parent.mkdir(parents=True, exist_ok=True) if str(self.path) != ":memory:" else None
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA_SQL)
        version = self.get_meta("schema_version")
        if version is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(version) < SCHEMA_VERSION:
            self._migrate(int(version))
        logger.info("数据库就绪：%s（schema v%s）", self.path, self.get_meta("schema_version"))

    def _migrate(self, from_version: int) -> None:
        """版本迁移。

        v1 → v2：分类**不再有内置代码**，旧的 ``stm_component`` / ``steam_card`` /
        ``other`` 一律改名成对应的中文分类名（``STM元器件`` / ``Steam游戏卡`` / ``未分类``）。
        """
        if from_version < 2:
            renamed = self._rename_legacy_categories()
            if renamed:
                logger.info("分类迁移：%s 条记录的旧分类代码已改名", renamed)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _rename_legacy_categories(self) -> int:
        """把旧的内置分类代码改写成中文分类名（幂等）。"""
        from .core.categories import LEGACY_CATEGORY_CODES

        renamed = 0
        # 走 transaction() 而不是 connection()：后者是 autocommit，
        # 不会让 revision 递增，上层的检索缓存就不知道数据变了。
        with self.transaction() as conn:
            for old, new in LEGACY_CATEGORY_CODES.items():
                cursor = conn.execute(
                    "UPDATE items SET category = ? WHERE category = ?", (new, old)
                )
                renamed += cursor.rowcount or 0
        return renamed

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
