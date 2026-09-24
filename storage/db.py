"""SQLite: кэш пулов и токенов, состояние (последний блок), алерты."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    address   TEXT PRIMARY KEY,
    symbol    TEXT,
    name      TEXT,
    decimals  INTEGER,          -- NULL, если decimals() не удалось прочитать
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pools (
    address   TEXT PRIMARY KEY,
    status    TEXT NOT NULL,    -- tracked | quote_pair | ignored_*
    version   TEXT,             -- v2 | v3
    factory   TEXT,
    token0    TEXT,
    token1    TEXT,
    fee       INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        INTEGER NOT NULL,
    mode              TEXT NOT NULL,        -- live | replay
    block_number      INTEGER NOT NULL,
    block_timestamp   INTEGER,
    pool              TEXT NOT NULL,
    dex               TEXT NOT NULL,
    token             TEXT NOT NULL,
    token_symbol      TEXT,
    token_name        TEXT,
    quote_symbol      TEXT,
    drop_pct          REAL NOT NULL,
    price_before      REAL,
    price_after       REAL,
    price_before_usd  REAL,
    price_after_usd   REAL,
    liquidity_usd     REAL,
    main_tx           TEXT,
    seller            TEXT,
    sell_usd          REAL,
    rugpull           INTEGER NOT NULL DEFAULT 0,
    extra             TEXT
);
CREATE INDEX IF NOT EXISTS alerts_pool_idx ON alerts(pool, block_number);
"""


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- state -----------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.conn.commit()

    def get_last_block(self) -> int | None:
        v = self.get_state("last_block")
        return int(v) if v is not None else None

    def set_last_block(self, block: int) -> None:
        self.set_state("last_block", block)

    # --- tokens / pools --------------------------------------------------
    def load_tokens(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tokens").fetchall()

    def load_pools(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pools").fetchall()

    def save_tokens(self, rows: Iterable[tuple[str, str | None, str | None, int | None]]) -> None:
        now = int(time.time())
        self.conn.executemany(
            "INSERT OR REPLACE INTO tokens(address, symbol, name, decimals, created_at) VALUES(?,?,?,?,?)",
            [(a, s, n, d, now) for a, s, n, d in rows])
        self.conn.commit()

    def save_pools(self, rows: Iterable[tuple]) -> None:
        """rows: (address, status, version, factory, token0, token1, fee)."""
        now = int(time.time())
        self.conn.executemany(
            "INSERT OR REPLACE INTO pools(address, status, version, factory, token0, token1, fee, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", [(*r, now) for r in rows])
        self.conn.commit()

    # --- alerts ----------------------------------------------------------
    def insert_alert(self, a: dict[str, Any]) -> int:
        cols = ["created_at", "mode", "block_number", "block_timestamp", "pool", "dex", "token",
                "token_symbol", "token_name", "quote_symbol", "drop_pct", "price_before", "price_after",
                "price_before_usd", "price_after_usd", "liquidity_usd", "main_tx", "seller", "sell_usd",
                "rugpull", "extra"]
        row = dict(a)
        row.setdefault("created_at", int(time.time()))
        row["rugpull"] = int(bool(row.get("rugpull")))
        if isinstance(row.get("extra"), (dict, list)):
            row["extra"] = json.dumps(row["extra"], ensure_ascii=False)
        cur = self.conn.execute(
            f"INSERT INTO alerts({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
            [row.get(c) for c in cols])
        self.conn.commit()
        return int(cur.lastrowid)

    def alerts(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
