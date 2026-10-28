from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .schemas import Trade


class FlowStore:
    """Store observed Binance aggregate trades in Postgres or local SQLite."""

    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "").strip() or None
        self.storage = "postgres" if self.database_url else "sqlite"
        default_path = Path(__file__).resolve().parents[1] / "data" / "flow.sqlite3"
        self.path = Path(os.getenv("FLOW_DB_PATH", str(default_path))).expanduser()
        self.retention_days = max(1, int(os.getenv("FLOW_RETENTION_DAYS", "1")))
        configured_durable = os.getenv("FLOW_STORAGE_DURABLE", "false").lower() in {"1", "true", "yes"}
        self.durable = self.storage == "postgres" or configured_durable
        self.pg_connection: Any = None
        self.db_lock = threading.Lock()
        self.queue: asyncio.Queue[tuple[str, Trade, int]] = asyncio.Queue(maxsize=20_000)
        self.dropped = 0
        self.saved = 0
        self.last_prune = time.monotonic()

    def initialize(self) -> None:
        if self.storage == "postgres":
            import psycopg

            self.pg_connection = psycopg.connect(
                self.database_url,
                connect_timeout=10,
                sslmode="require",
                autocommit=True,
                prepare_threshold=None,
            )
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS flow_trades (
                        symbol TEXT NOT NULL,
                        trade_id BIGINT NOT NULL,
                        event_time_ms BIGINT NOT NULL,
                        price DOUBLE PRECISION NOT NULL,
                        quantity DOUBLE PRECISION NOT NULL,
                        side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                        received_time_ms BIGINT NOT NULL,
                        PRIMARY KEY (symbol, trade_id)
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_flow_trades_symbol_time ON flow_trades(symbol, event_time_ms DESC)")
            self.prune()
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS flow_trades (
                    symbol TEXT NOT NULL,
                    trade_id INTEGER NOT NULL,
                    event_time_ms INTEGER NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                    received_time_ms INTEGER NOT NULL,
                    PRIMARY KEY (symbol, trade_id)
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_flow_trades_symbol_time ON flow_trades(symbol, event_time_ms DESC)")
            cutoff = int(time.time() * 1000) - self.retention_days * 86_400_000
            connection.execute("DELETE FROM flow_trades WHERE event_time_ms < ?", (cutoff,))
            connection.execute("PRAGMA incremental_vacuum(10000)")
            connection.commit()

    def prune(self) -> None:
        cutoff = int(time.time() * 1000) - self.retention_days * 86_400_000
        if self.storage == "postgres":
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.execute("DELETE FROM flow_trades WHERE event_time_ms < %s", (cutoff,))
            return
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("DELETE FROM flow_trades WHERE event_time_ms < ?", (cutoff,))
            connection.execute("PRAGMA incremental_vacuum(10000)")
            connection.commit()

    def enqueue(self, symbol: str, trade: Trade, received_time_ms: int) -> None:
        try:
            self.queue.put_nowait((symbol, trade, received_time_ms))
        except asyncio.QueueFull:
            self.dropped += 1

    def _insert_batch(self, batch: list[tuple[str, Trade, int]]) -> int:
        values = [(symbol, trade.id, trade.time, trade.price, trade.quantity, trade.side, received) for symbol, trade, received in batch]
        if self.storage == "postgres":
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO flow_trades(symbol, trade_id, event_time_ms, price, quantity, side, received_time_ms) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (symbol, trade_id) DO NOTHING",
                    values,
                )
                return cursor.rowcount
        with sqlite3.connect(self.path, timeout=10) as connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO flow_trades(symbol, trade_id, event_time_ms, price, quantity, side, received_time_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            connection.commit()
            return connection.total_changes - before

    async def writer(self) -> None:
        while True:
            first = await self.queue.get()
            batch = [first]
            deadline = asyncio.get_running_loop().time() + 0.25
            while len(batch) < 1000:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
            try:
                self.saved += await asyncio.to_thread(self._insert_batch, batch)
                if time.monotonic() - self.last_prune >= 6 * 60 * 60:
                    await asyncio.to_thread(self.prune)
                    self.last_prune = time.monotonic()
            except Exception:
                self.dropped += len(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()

    def recent(self, symbol: str, limit: int) -> list[Trade]:
        if self.storage == "postgres":
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT trade_id, event_time_ms, price, quantity, side FROM flow_trades WHERE symbol = %s ORDER BY event_time_ms DESC, trade_id DESC LIMIT %s",
                    (symbol, limit),
                )
                rows = cursor.fetchall()
            return [Trade(id=row[0], time=row[1], price=row[2], quantity=row[3], side=row[4]) for row in rows]
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path, timeout=5) as connection:
            rows = connection.execute(
                "SELECT trade_id, event_time_ms, price, quantity, side FROM flow_trades WHERE symbol = ? ORDER BY event_time_ms DESC, trade_id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [Trade(id=row[0], time=row[1], price=row[2], quantity=row[3], side=row[4]) for row in rows]

    def summary(self, symbol: str) -> dict[str, Any]:
        if self.storage == "postgres":
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*), MIN(event_time_ms), MAX(event_time_ms) FROM flow_trades WHERE symbol = %s", (symbol,))
                row = cursor.fetchone()
                cursor.execute("SELECT pg_database_size(current_database())")
                storage_bytes = cursor.fetchone()[0]
            return {"storedTrades": int(row[0]), "oldestTradeTime": row[1], "newestTradeTime": row[2], "storageBytes": storage_bytes}
        if not self.path.exists():
            return {"storedTrades": 0, "oldestTradeTime": None, "newestTradeTime": None, "storageBytes": 0}
        with sqlite3.connect(self.path, timeout=5) as connection:
            row = connection.execute(
                "SELECT COUNT(*), MIN(event_time_ms), MAX(event_time_ms) FROM flow_trades WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        storage_bytes = sum(candidate.stat().st_size for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")) if candidate.exists())
        return {"storedTrades": int(row[0]), "oldestTradeTime": row[1], "newestTradeTime": row[2], "storageBytes": storage_bytes}

    def close(self) -> None:
        if self.pg_connection is not None:
            with self.db_lock:
                self.pg_connection.close()

