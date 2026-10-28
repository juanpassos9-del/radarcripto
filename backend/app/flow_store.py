from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .schemas import Trade


class FlowStore:
    """Small append-only SQLite store for observed Binance aggregate trades."""

    def __init__(self) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "flow.sqlite3"
        self.path = Path(os.getenv("FLOW_DB_PATH", str(default_path))).expanduser()
        self.retention_days = max(1, int(os.getenv("FLOW_RETENTION_DAYS", "1")))
        self.durable = os.getenv("FLOW_STORAGE_DURABLE", "false").lower() in {"1", "true", "yes"}
        self.queue: asyncio.Queue[tuple[str, Trade, int]] = asyncio.Queue(maxsize=20_000)
        self.dropped = 0
        self.saved = 0
        self.last_prune = time.monotonic()

    def initialize(self) -> None:
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
        with sqlite3.connect(self.path, timeout=10) as connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO flow_trades(symbol, trade_id, event_time_ms, price, quantity, side, received_time_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(symbol, trade.id, trade.time, trade.price, trade.quantity, trade.side, received) for symbol, trade, received in batch],
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
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path, timeout=5) as connection:
            rows = connection.execute(
                "SELECT trade_id, event_time_ms, price, quantity, side FROM flow_trades WHERE symbol = ? ORDER BY event_time_ms DESC, trade_id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [Trade(id=row[0], time=row[1], price=row[2], quantity=row[3], side=row[4]) for row in rows]

    def summary(self, symbol: str) -> dict[str, Any]:
        if not self.path.exists():
            return {"storedTrades": 0, "oldestTradeTime": None, "newestTradeTime": None}
        with sqlite3.connect(self.path, timeout=5) as connection:
            row = connection.execute(
                "SELECT COUNT(*), MIN(event_time_ms), MAX(event_time_ms) FROM flow_trades WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return {"storedTrades": int(row[0]), "oldestTradeTime": row[1], "newestTradeTime": row[2]}

