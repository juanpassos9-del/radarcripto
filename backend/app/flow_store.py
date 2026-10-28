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
        self.retention_hours = max(1, int(os.getenv("FLOW_RETENTION_HOURS", "1")))
        self.aggregate_retention_days = max(7, int(os.getenv("FLOW_AGG_RETENTION_DAYS", "30")))
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
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS flow_minute_bars (
                        symbol TEXT NOT NULL,
                        bucket_time_ms BIGINT NOT NULL,
                        trade_count BIGINT NOT NULL,
                        buy_quantity DOUBLE PRECISION NOT NULL,
                        sell_quantity DOUBLE PRECISION NOT NULL,
                        buy_notional DOUBLE PRECISION NOT NULL,
                        sell_notional DOUBLE PRECISION NOT NULL,
                        open_price DOUBLE PRECISION NOT NULL,
                        open_time_ms BIGINT NOT NULL,
                        open_trade_id BIGINT NOT NULL,
                        high_price DOUBLE PRECISION NOT NULL,
                        low_price DOUBLE PRECISION NOT NULL,
                        close_price DOUBLE PRECISION NOT NULL,
                        close_time_ms BIGINT NOT NULL,
                        close_trade_id BIGINT NOT NULL,
                        PRIMARY KEY (symbol, bucket_time_ms)
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_flow_minute_bars_symbol_time ON flow_minute_bars(symbol, bucket_time_ms DESC)")
                cursor.execute("""
                    CREATE OR REPLACE FUNCTION nexus_update_flow_minute_bar() RETURNS trigger AS $$
                    BEGIN
                        INSERT INTO flow_minute_bars (
                            symbol, bucket_time_ms, trade_count, buy_quantity, sell_quantity,
                            buy_notional, sell_notional, open_price, open_time_ms, open_trade_id,
                            high_price, low_price, close_price, close_time_ms, close_trade_id
                        ) VALUES (
                            NEW.symbol, (NEW.event_time_ms / 60000) * 60000, 1,
                            CASE WHEN NEW.side = 'buy' THEN NEW.quantity ELSE 0 END,
                            CASE WHEN NEW.side = 'sell' THEN NEW.quantity ELSE 0 END,
                            CASE WHEN NEW.side = 'buy' THEN NEW.price * NEW.quantity ELSE 0 END,
                            CASE WHEN NEW.side = 'sell' THEN NEW.price * NEW.quantity ELSE 0 END,
                            NEW.price, NEW.event_time_ms, NEW.trade_id, NEW.price, NEW.price,
                            NEW.price, NEW.event_time_ms, NEW.trade_id
                        ) ON CONFLICT (symbol, bucket_time_ms) DO UPDATE SET
                            trade_count = flow_minute_bars.trade_count + 1,
                            buy_quantity = flow_minute_bars.buy_quantity + EXCLUDED.buy_quantity,
                            sell_quantity = flow_minute_bars.sell_quantity + EXCLUDED.sell_quantity,
                            buy_notional = flow_minute_bars.buy_notional + EXCLUDED.buy_notional,
                            sell_notional = flow_minute_bars.sell_notional + EXCLUDED.sell_notional,
                            open_price = CASE WHEN (EXCLUDED.open_time_ms, EXCLUDED.open_trade_id) < (flow_minute_bars.open_time_ms, flow_minute_bars.open_trade_id) THEN EXCLUDED.open_price ELSE flow_minute_bars.open_price END,
                            open_time_ms = LEAST(flow_minute_bars.open_time_ms, EXCLUDED.open_time_ms),
                            open_trade_id = CASE WHEN (EXCLUDED.open_time_ms, EXCLUDED.open_trade_id) < (flow_minute_bars.open_time_ms, flow_minute_bars.open_trade_id) THEN EXCLUDED.open_trade_id ELSE flow_minute_bars.open_trade_id END,
                            high_price = GREATEST(flow_minute_bars.high_price, EXCLUDED.high_price),
                            low_price = LEAST(flow_minute_bars.low_price, EXCLUDED.low_price),
                            close_price = CASE WHEN (EXCLUDED.close_time_ms, EXCLUDED.close_trade_id) > (flow_minute_bars.close_time_ms, flow_minute_bars.close_trade_id) THEN EXCLUDED.close_price ELSE flow_minute_bars.close_price END,
                            close_time_ms = GREATEST(flow_minute_bars.close_time_ms, EXCLUDED.close_time_ms),
                            close_trade_id = CASE WHEN (EXCLUDED.close_time_ms, EXCLUDED.close_trade_id) > (flow_minute_bars.close_time_ms, flow_minute_bars.close_trade_id) THEN EXCLUDED.close_trade_id ELSE flow_minute_bars.close_trade_id END;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
                """)
                cursor.execute("DROP TRIGGER IF EXISTS trg_flow_trade_minute_bar ON flow_trades")
                cursor.execute("CREATE TRIGGER trg_flow_trade_minute_bar AFTER INSERT ON flow_trades FOR EACH ROW EXECUTE FUNCTION nexus_update_flow_minute_bar()")
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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS flow_minute_bars (
                    symbol TEXT NOT NULL,
                    bucket_time_ms INTEGER NOT NULL,
                    trade_count INTEGER NOT NULL,
                    buy_quantity REAL NOT NULL,
                    sell_quantity REAL NOT NULL,
                    buy_notional REAL NOT NULL,
                    sell_notional REAL NOT NULL,
                    open_price REAL NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    open_trade_id INTEGER NOT NULL,
                    high_price REAL NOT NULL,
                    low_price REAL NOT NULL,
                    close_price REAL NOT NULL,
                    close_time_ms INTEGER NOT NULL,
                    close_trade_id INTEGER NOT NULL,
                    PRIMARY KEY (symbol, bucket_time_ms)
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_flow_minute_bars_symbol_time ON flow_minute_bars(symbol, bucket_time_ms DESC)")
            connection.execute("DROP TRIGGER IF EXISTS trg_flow_trade_minute_bar")
            connection.execute("""
                CREATE TRIGGER trg_flow_trade_minute_bar AFTER INSERT ON flow_trades BEGIN
                    INSERT INTO flow_minute_bars (
                        symbol, bucket_time_ms, trade_count, buy_quantity, sell_quantity,
                        buy_notional, sell_notional, open_price, open_time_ms, open_trade_id,
                        high_price, low_price, close_price, close_time_ms, close_trade_id
                    ) VALUES (
                        NEW.symbol, (NEW.event_time_ms / 60000) * 60000, 1,
                        CASE WHEN NEW.side = 'buy' THEN NEW.quantity ELSE 0 END,
                        CASE WHEN NEW.side = 'sell' THEN NEW.quantity ELSE 0 END,
                        CASE WHEN NEW.side = 'buy' THEN NEW.price * NEW.quantity ELSE 0 END,
                        CASE WHEN NEW.side = 'sell' THEN NEW.price * NEW.quantity ELSE 0 END,
                        NEW.price, NEW.event_time_ms, NEW.trade_id, NEW.price, NEW.price,
                        NEW.price, NEW.event_time_ms, NEW.trade_id
                    ) ON CONFLICT(symbol, bucket_time_ms) DO UPDATE SET
                        trade_count = trade_count + 1,
                        buy_quantity = buy_quantity + excluded.buy_quantity,
                        sell_quantity = sell_quantity + excluded.sell_quantity,
                        buy_notional = buy_notional + excluded.buy_notional,
                        sell_notional = sell_notional + excluded.sell_notional,
                        open_price = CASE WHEN (excluded.open_time_ms, excluded.open_trade_id) < (open_time_ms, open_trade_id) THEN excluded.open_price ELSE open_price END,
                        open_time_ms = min(open_time_ms, excluded.open_time_ms),
                        open_trade_id = CASE WHEN (excluded.open_time_ms, excluded.open_trade_id) < (open_time_ms, open_trade_id) THEN excluded.open_trade_id ELSE open_trade_id END,
                        high_price = max(high_price, excluded.high_price),
                        low_price = min(low_price, excluded.low_price),
                        close_price = CASE WHEN (excluded.close_time_ms, excluded.close_trade_id) > (close_time_ms, close_trade_id) THEN excluded.close_price ELSE close_price END,
                        close_time_ms = max(close_time_ms, excluded.close_time_ms),
                        close_trade_id = CASE WHEN (excluded.close_time_ms, excluded.close_trade_id) > (close_time_ms, close_trade_id) THEN excluded.close_trade_id ELSE close_trade_id END;
                END
            """)
            cutoff = int(time.time() * 1000) - self.retention_hours * 3_600_000
            connection.execute("DELETE FROM flow_trades WHERE event_time_ms < ?", (cutoff,))
            aggregate_cutoff = int(time.time() * 1000) - self.aggregate_retention_days * 86_400_000
            connection.execute("DELETE FROM flow_minute_bars WHERE bucket_time_ms < ?", (aggregate_cutoff,))
            connection.execute("PRAGMA incremental_vacuum(10000)")
            connection.commit()

    def prune(self) -> None:
        cutoff = int(time.time() * 1000) - self.retention_hours * 3_600_000
        aggregate_cutoff = int(time.time() * 1000) - self.aggregate_retention_days * 86_400_000
        if self.storage == "postgres":
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.execute("DELETE FROM flow_trades WHERE event_time_ms < %s", (cutoff,))
                cursor.execute("DELETE FROM flow_minute_bars WHERE bucket_time_ms < %s", (aggregate_cutoff,))
            return
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("DELETE FROM flow_trades WHERE event_time_ms < ?", (cutoff,))
            connection.execute("DELETE FROM flow_minute_bars WHERE bucket_time_ms < ?", (aggregate_cutoff,))
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
                if time.monotonic() - self.last_prune >= 15 * 60:
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
                cursor.execute("SELECT COUNT(*), MIN(bucket_time_ms), MAX(bucket_time_ms) FROM flow_minute_bars WHERE symbol = %s", (symbol,))
                bars = cursor.fetchone()
            return {"storedTrades": int(row[0]), "oldestTradeTime": row[1], "newestTradeTime": row[2], "storedFlowBars": int(bars[0]), "oldestFlowBarTime": bars[1], "newestFlowBarTime": bars[2], "storageBytes": storage_bytes}
        if not self.path.exists():
            return {"storedTrades": 0, "oldestTradeTime": None, "newestTradeTime": None, "storageBytes": 0}
        with sqlite3.connect(self.path, timeout=5) as connection:
            row = connection.execute(
                "SELECT COUNT(*), MIN(event_time_ms), MAX(event_time_ms) FROM flow_trades WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            bars = connection.execute("SELECT COUNT(*), MIN(bucket_time_ms), MAX(bucket_time_ms) FROM flow_minute_bars WHERE symbol = ?", (symbol,)).fetchone()
        storage_bytes = sum(candidate.stat().st_size for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")) if candidate.exists())
        return {"storedTrades": int(row[0]), "oldestTradeTime": row[1], "newestTradeTime": row[2], "storedFlowBars": int(bars[0]), "oldestFlowBarTime": bars[1], "newestFlowBarTime": bars[2], "storageBytes": storage_bytes}

    def flow_history(self, symbol: str, start_ms: int, end_ms: int, interval_minutes: int) -> list[dict[str, Any]]:
        bucket_ms = interval_minutes * 60_000
        if self.storage == "postgres":
            with self.db_lock, self.pg_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT bucket_time_ms, trade_count, buy_quantity, sell_quantity, buy_notional, sell_notional, open_price, high_price, low_price, close_price FROM flow_minute_bars WHERE symbol = %s AND bucket_time_ms >= %s AND bucket_time_ms < %s ORDER BY bucket_time_ms ASC",
                    (symbol, start_ms, end_ms),
                )
                rows = cursor.fetchall()
        else:
            if not self.path.exists():
                return []
            with sqlite3.connect(self.path, timeout=5) as connection:
                rows = connection.execute(
                    "SELECT bucket_time_ms, trade_count, buy_quantity, sell_quantity, buy_notional, sell_notional, open_price, high_price, low_price, close_price FROM flow_minute_bars WHERE symbol = ? AND bucket_time_ms >= ? AND bucket_time_ms < ? ORDER BY bucket_time_ms ASC",
                    (symbol, start_ms, end_ms),
                ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            bucket = (int(row[0]) // bucket_ms) * bucket_ms
            current = grouped.get(bucket)
            if current is None:
                grouped[bucket] = {
                    "time": bucket // 1000, "tradeCount": int(row[1]),
                    "buyQuantity": float(row[2]), "sellQuantity": float(row[3]),
                    "buyNotional": float(row[4]), "sellNotional": float(row[5]),
                    "open": float(row[6]), "high": float(row[7]),
                    "low": float(row[8]), "close": float(row[9]),
                }
            else:
                current["tradeCount"] += int(row[1])
                current["buyQuantity"] += float(row[2])
                current["sellQuantity"] += float(row[3])
                current["buyNotional"] += float(row[4])
                current["sellNotional"] += float(row[5])
                current["high"] = max(current["high"], float(row[7]))
                current["low"] = min(current["low"], float(row[8]))
                current["close"] = float(row[9])
        return list(grouped.values())

    def close(self) -> None:
        if self.pg_connection is not None:
            with self.db_lock:
                self.pg_connection.close()

