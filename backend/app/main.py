from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import websockets
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .schemas import BookSnapshot, Candle, Level, MarketSnapshot, Trade
from .flow_store import FlowStore
from .reversal_engine import build_reversal_signals
from .volatility import calculate_zones

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("nexus.market")
SPOT_DATA = os.getenv("BINANCE_SPOT_REST_URL", "https://data-api.binance.vision")
SPOT_WS = os.getenv("BINANCE_SPOT_WS_URL", "wss://data-stream.binance.vision/stream")
MAX_GAP_BACKFILL_TRADES = max(0, int(os.getenv("FLOW_GAP_RECOVERY_MAX_TRADES", "5000")))
MAX_AGGREGATED_BOOK_LEVELS = 150
MAX_EXTERNAL_SYMBOLS = 5
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "BCHUSDT", "SUIUSDT", "TRXUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "FILUSDT",
    "UNIUSDT", "ETCUSDT", "ATOMUSDT", "AAVEUSDT", "WIFUSDT",
]
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w", "1M"}

class MarketService:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(headers={"User-Agent": "NexusCryptoTerminal/0.1"}, timeout=12)
        self.request_slots = asyncio.Semaphore(5)
        self.exchange_headers = {
            "Origin": "https://www.binance.com",
            "Referer": "https://www.binance.com/",
        }
        self.books: dict[tuple[str, str], dict[str, Any]] = {}
        self.samples: dict[tuple[str, str], deque[Trade]] = {}
        self.external_books: dict[tuple[str, str], dict[str, Any]] = {}
        self.external_trades: dict[str, deque[Trade]] = {}
        self.external_trade_status: dict[tuple[str, str], dict[str, Any]] = {}
        self.external_trade_ids: dict[tuple[str, str], tuple[deque[str], set[str]]] = {}
        self.external_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self.external_symbol_access: dict[str, int] = {}
        self.context_cache: dict[str, Any] | None = None
        self.context_cache_expires = 0.0
        self.context_lock = asyncio.Lock()
        self.gap_recovery_locks = {symbol: asyncio.Lock() for symbol in SYMBOLS}
        self.recovery_tasks: set[asyncio.Task[Any]] = set()
        self.flow_store: FlowStore | None = None
        self.flow_quality: dict[str, dict[str, Any]] = {
            symbol: {"status": "starting", "connected": False, "sessionStartedAt": int(time.time() * 1000), "connectedAt": None, "lastEventTime": None, "lastReceivedAt": None, "lastLatencyMs": None, "events": 0, "reconnects": 0, "possibleIdGaps": 0, "backfillRowsFetched": 0, "backfillRowsQueued": 0, "backfillSkippedIds": 0, "backfillFailures": 0, "lastBackfillAt": None, "lastTradeId": None, "lastDisconnectAt": None}
            for symbol in SYMBOLS
        }

    async def close(self) -> None:
        await self.client.aclose()

    async def collect_all_trades(self) -> None:
        streams = [f"{symbol.lower()}@aggTrade" for symbol in SYMBOLS]
        url = f"{SPOT_WS}?streams={'/'.join(streams)}"
        reconnect_delay = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=12, close_timeout=5, max_size=2**20, max_queue=4096) as socket:
                    connected_at = int(time.time() * 1000)
                    connected_since = time.monotonic()
                    for state in self.flow_quality.values():
                        if state["connectedAt"] is not None:
                            state["reconnects"] += 1
                        state.update(status="live", connected=True, connectedAt=connected_at)
                    async for raw in socket:
                        message = json.loads(raw)
                        data = message.get("data", message)
                        symbol = str(data.get("s", "")).upper()
                        if symbol not in self.flow_quality:
                            continue
                        received = int(time.time() * 1000)
                        trade = Trade(id=int(data["a"]), time=int(data["T"]), price=float(data["p"]), quantity=float(data["q"]), side="sell" if data["m"] else "buy")
                        state = self.flow_quality[symbol]
                        previous_id = state["lastTradeId"]
                        if previous_id is not None and trade.id > previous_id + 1:
                            first_missing, last_missing = previous_id + 1, trade.id - 1
                            gap_size = last_missing - first_missing + 1
                            state["possibleIdGaps"] += gap_size
                            if self.flow_store and MAX_GAP_BACKFILL_TRADES:
                                recovery = asyncio.create_task(self.recover_trade_gap(symbol, first_missing, last_missing))
                                self.recovery_tasks.add(recovery)
                                recovery.add_done_callback(self.recovery_tasks.discard)
                            elif gap_size:
                                state["backfillSkippedIds"] += gap_size
                        state.update(lastTradeId=trade.id, lastEventTime=trade.time, lastReceivedAt=received, lastLatencyMs=max(0, received-trade.time), events=state["events"] + 1, status="live", connected=True)
                        sample = self.samples.setdefault(("spot", symbol), deque(maxlen=5000))
                        if not sample or sample[-1].id != trade.id:
                            sample.append(trade)
                        if self.flow_store:
                            self.flow_store.enqueue(symbol, trade, received)
                disconnected_at = int(time.time() * 1000)
                for state in self.flow_quality.values():
                    state.update(status="reconnecting", connected=False, lastDisconnectAt=disconnected_at)
                reconnect_delay = 1.0 if time.monotonic() - connected_since >= 20 else min(reconnect_delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                disconnected_at = int(time.time() * 1000)
                log.exception("aggregate trade collector disconnected")
                for state in self.flow_quality.values():
                    state.update(status="reconnecting", connected=False, lastDisconnectAt=disconnected_at)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            await asyncio.sleep(reconnect_delay)

    async def recover_trade_gap(self, symbol: str, first_id: int, last_id: int) -> None:
        """Fill a bounded slice of a Binance aggTrade ID gap from its public REST API."""
        state = self.flow_quality[symbol]
        async with self.gap_recovery_locks[symbol]:
            if not self.flow_store or MAX_GAP_BACKFILL_TRADES <= 0:
                return
            requested_count = last_id - first_id + 1
            start_id = max(first_id, last_id - MAX_GAP_BACKFILL_TRADES + 1)
            state["backfillSkippedIds"] += max(0, start_id - first_id)
            cursor_id = start_id
            received_at = int(time.time() * 1000)
            try:
                while cursor_id <= last_id:
                    page_limit = min(1000, last_id - cursor_id + 1)
                    rows = await self.request(SPOT_DATA, "/api/v3/aggTrades", {
                        "symbol": symbol, "fromId": cursor_id, "limit": page_limit,
                    })
                    if not rows:
                        break
                    page_ids: list[int] = []
                    for row in rows:
                        trade_id = int(row["a"])
                        if trade_id < cursor_id:
                            continue
                        if trade_id > last_id:
                            break
                        expected_id = (max(page_ids) + 1) if page_ids else cursor_id
                        if trade_id > expected_id:
                            state["backfillSkippedIds"] += trade_id - expected_id
                        recovered = Trade(
                            id=trade_id, time=int(row["T"]), price=float(row["p"]),
                            quantity=float(row["q"]), side="sell" if row["m"] else "buy",
                        )
                        page_ids.append(trade_id)
                        state["backfillRowsFetched"] += 1
                        if self.flow_store.enqueue(symbol, recovered, received_at):
                            state["backfillRowsQueued"] += 1
                    if not page_ids:
                        break
                    next_id = max(page_ids) + 1
                    if next_id <= cursor_id:
                        break
                    cursor_id = next_id
                    state["lastBackfillAt"] = int(time.time() * 1000)
                if cursor_id <= last_id:
                    state["backfillSkippedIds"] += last_id - cursor_id + 1
                    log.warning("Binance trade gap was only partially recovered", extra={"symbol": symbol, "requested": requested_count, "fetched": state["backfillRowsFetched"]})
            except asyncio.CancelledError:
                raise
            except Exception:
                state["backfillFailures"] += 1
                log.exception("Binance trade gap recovery failed", extra={"symbol": symbol, "from_id": cursor_id, "to_id": last_id})

    async def request(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(4):
            try:
                # Keep concurrent exchange requests within a small bound. The free
                # Render instance has limited CPU/connections and should not fan
                # out dozens of simultaneous Binance calls per dashboard refresh.
                async with self.request_slots:
                    response = await self.client.get(base + path, params=params, headers=self.exchange_headers)
                if response.status_code == 429 or response.status_code == 418:
                    wait = min(float(response.headers.get("Retry-After", 1)), 10)
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt == 3:
                    raise error
                await asyncio.sleep(min(0.4 * 2**attempt, 5))
        raise HTTPException(502, "Binance rate limit exceeded")

    async def ticker(self, symbol: str, market: str) -> MarketSnapshot:
        base, path = self.base_and_path(market, "ticker")
        received = int(time.time() * 1000)
        row = await self.request(base, path, {"symbol": symbol})
        event = int(row["closeTime"])
        return MarketSnapshot(symbol=symbol, price=float(row["lastPrice"]), change24h=float(row["priceChangePercent"]), volume24h=float(row["volume"]), quoteVolume24h=float(row["quoteVolume"]), high24h=float(row["highPrice"]), low24h=float(row["lowPrice"]), source=self.market_source(market), eventTime=event, receivedAt=received, latencyMs=max(0, received - event), status="live" if received - event < 60_000 else "stale")

    async def market_context(self) -> dict[str, Any]:
        now = time.monotonic()
        if self.context_cache and now < self.context_cache_expires:
            return self.context_cache
        async with self.context_lock:
            now = time.monotonic()
            if self.context_cache and now < self.context_cache_expires:
                return self.context_cache

            async def fetch_json(url: str) -> Any:
                try:
                    response = await self.client.get(url, timeout=12)
                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPError, ValueError) as error:
                    log.warning("market context source unavailable: %s", error)
                    return None

            fear_data, tvl_data, stablecoin_data = await asyncio.gather(
                fetch_json("https://api.alternative.me/fng/?limit=2"),
                fetch_json("https://api.llama.fi/v2/historicalChainTvl"),
                fetch_json("https://stablecoins.llama.fi/stablecoincharts/all"),
            )
            received_at = int(time.time() * 1000)

            fear_rows = fear_data.get("data", []) if isinstance(fear_data, dict) else []
            fear = None
            if fear_rows:
                row = fear_rows[0]
                fear = {"value": int(row["value"]), "label": str(row["value_classification"]), "timestamp": int(row["timestamp"]) * 1000, "source": "Alternative.me", "status": "live"}

            tvl = None
            if isinstance(tvl_data, list) and tvl_data:
                latest = tvl_data[-1]
                previous = next((item for item in reversed(tvl_data[:-1]) if int(latest["date"]) - int(item["date"]) >= 20 * 3600), None)
                current_value = float(latest["tvl"])
                previous_value = float(previous["tvl"]) if previous else None
                tvl = {"value": current_value, "change24hPct": ((current_value / previous_value) - 1) * 100 if previous_value else None, "timestamp": int(latest["date"]) * 1000, "source": "DefiLlama", "status": "live"}

            stablecoin_supply = None
            if isinstance(stablecoin_data, list) and stablecoin_data:
                latest = stablecoin_data[-1]
                previous = next((item for item in reversed(stablecoin_data[:-1]) if int(latest["date"]) - int(item["date"]) >= 20 * 3600), None)
                current_value = float(latest.get("totalCirculatingUSD", {}).get("peggedUSD", 0))
                previous_value = float(previous.get("totalCirculatingUSD", {}).get("peggedUSD", 0)) if previous else None
                stablecoin_supply = {"value": current_value, "change24hPct": ((current_value / previous_value) - 1) * 100 if previous_value else None, "timestamp": int(latest["date"]) * 1000, "source": "DefiLlama", "status": "live"}

            self.context_cache = {"fearAndGreed": fear, "tvl": tvl, "stablecoinSupply": stablecoin_supply, "receivedAt": received_at}
            self.context_cache_expires = time.monotonic() + 3600
            return self.context_cache

    @staticmethod
    def market_source(market: str) -> str:
        return "Binance Spot"

    @staticmethod
    def base_and_path(market: str, resource: str) -> tuple[str, str]:
        """Return the REST base and endpoint for a supported public market feed."""
        paths = {
            "ticker": "/api/v3/ticker/24hr",
            "depth": "/api/v3/depth",
            "klines": "/api/v3/klines",
            "trades": "/api/v3/aggTrades",
        }
        return SPOT_DATA, paths[resource]

    async def book(self, symbol: str, market: str, limit: int) -> BookSnapshot:
        self.ensure_external_collectors(symbol)
        key = (market, symbol)
        state = self.books.get(key)
        if state:
            if state["status"] == "live" and int(time.time() * 1000) - state["receivedAt"] < 15_000:
                return self.book_payload(symbol, state, limit)
            task = state.get("task")
            if task and not task.done():
                # Never replace an in-progress snapshot/stream; callers share one book collector per symbol.
                return self.book_payload(symbol, state, limit)
        base, path = self.base_and_path(market, "depth")
        # Fetch enough levels for Spot snapshot/diff synchronization; trim in the response.
        snapshot_limit = 1000
        snapshot = await self.request(base, path, {"symbol": symbol, "limit": snapshot_limit})
        bids = {float(price): float(qty) for price, qty in snapshot["bids"] if float(qty) > 0}
        asks = {float(price): float(qty) for price, qty in snapshot["asks"] if float(qty) > 0}
        state = {"bids": bids, "asks": asks, "lastUpdateId": int(snapshot["lastUpdateId"]), "status": "syncing", "eventTime": None, "receivedAt": int(time.time() * 1000), "buffer": [], "lock": asyncio.Lock(), "task": None}
        state["market"] = market
        self.books[key] = state
        state["task"] = asyncio.create_task(self.book_stream(market, symbol, state))
        return self.book_payload(symbol, state, limit)

    def book_payload(self, symbol: str, state: dict[str, Any], limit: int) -> BookSnapshot:
        bids = sorted(state["bids"].items(), reverse=True)[:limit]
        asks = sorted(state["asks"].items())[:limit]
        now_ms = int(time.time() * 1000)
        external = [self.external_books.get((symbol, venue)) for venue in ("Kraken", "Coinbase")]
        venue_states = {"Binance": "stale" if state["status"] == "live" and now_ms - int(state["receivedAt"]) > 15_000 else state["status"]}
        for venue, other in zip(("Kraken", "Coinbase"), external):
            status = other["status"] if other else "connecting"
            venue_states[venue] = "stale" if status == "live" and now_ms - int(other["receivedAt"]) > 15_000 else status
        combined_bids: dict[float, dict[str, Any]] = {}
        combined_asks: dict[float, dict[str, Any]] = {}
        source_books = [("Binance", state), *[(name, item) for name, item in zip(("Kraken", "Coinbase"), external) if item and venue_states.get(name) == "live"]]
        source_books = [(venue, item) for venue, item in source_books if item.get("status") == "live" and now_ms - int(item.get("receivedAt", 0)) <= 15_000]
        mids = [(max(item["bids"]) + min(item["asks"])) / 2 for _, item in source_books if item.get("bids") and item.get("asks")]
        reference_price = sum(mids) / len(mids) if mids else 1.0
        bucket_size = max(reference_price * 0.00001, 0.00000001)  # 0.1 bp shared across venues.
        for venue, venue_state in source_books:
            if venue_state.get("status") != "live" or int(time.time() * 1000) - int(venue_state.get("receivedAt", 0)) > 15_000:
                continue
            for side, output in (("bids", combined_bids), ("asks", combined_asks)):
                levels = venue_state.get(side, {}).items()
                levels = sorted(levels, reverse=(side == "bids"))[:MAX_AGGREGATED_BOOK_LEVELS]
                for price, qty in levels:
                    price = float(price)  # USD and USDT are treated as 1:1 for this indicative cross-venue view.
                    qty = float(qty)
                    bucket = round(price / bucket_size) * bucket_size
                    level = output.setdefault(bucket, {"quantity": 0.0, "sources": set()})
                    level["quantity"] += qty
                    level["sources"].add(venue)
        bid_rows = sorted(combined_bids.items(), reverse=True)[:limit]
        ask_rows = sorted(combined_asks.items())[:limit]
        bid_levels, ask_levels = [], []
        total = 0.0
        for price, aggregate in bid_rows:
            qty = aggregate["quantity"]
            total += price * qty
            bid_levels.append(Level(price=price, quantity=qty, total=total, sources=sorted(aggregate["sources"])))
        total = 0.0
        for price, aggregate in ask_rows:
            qty = aggregate["quantity"]
            total += price * qty
            ask_levels.append(Level(price=price, quantity=qty, total=total, sources=sorted(aggregate["sources"])))
        bid_volume = sum(item["quantity"] for item in combined_bids.values())
        ask_volume = sum(item["quantity"] for item in combined_asks.values())
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) if bid_volume + ask_volume else 0.0
        live_sources = [name for name, status in venue_states.items() if status == "live"]
        event_times = [item.get("eventTime") for item in [state, *[x for x in external if x]] if item.get("eventTime")]
        return BookSnapshot(symbol=symbol, bids=bid_levels, asks=ask_levels, spread=ask_rows[0][0] - bid_rows[0][0] if ask_rows and bid_rows else 0, imbalance=imbalance, eventTime=max(event_times) if event_times else None, receivedAt=now_ms, status="live" if live_sources else "connecting", source=" + ".join(live_sources) or "Nenhuma fonte ao vivo", sources=venue_states)

    def ensure_external_collectors(self, symbol: str) -> None:
        # Keep a small LRU of requested symbols to bound public sockets on free hosting.
        self.external_symbol_access[symbol] = int(time.time() * 1000)
        active_symbols = {active_symbol for (active_symbol, _), task in self.external_tasks.items() if not task.done()}
        active_symbols.add(symbol)
        while len(active_symbols) > MAX_EXTERNAL_SYMBOLS:
            candidates = active_symbols - {symbol}
            if not candidates:
                break
            evicted = min(candidates, key=lambda item: self.external_symbol_access.get(item, 0))
            for (active_symbol, venue), task in list(self.external_tasks.items()):
                if active_symbol == evicted and not task.done():
                    task.cancel()
                    if (active_symbol, venue) in self.external_books:
                        self.external_books[(active_symbol, venue)]["status"] = "stopped"
            active_symbols.discard(evicted)
        for venue in ("Kraken", "Coinbase"):
            key = (symbol, venue)
            task = self.external_tasks.get(key)
            if task is None or task.done():
                self.external_tasks[key] = asyncio.create_task(self.external_collector(symbol, venue))

    @staticmethod
    def external_pair(symbol: str, venue: str) -> str:
        base = symbol.removesuffix("USDT")
        if venue == "Kraken":
            return f"{base}/USD"
        return f"{base}-USD"

    @staticmethod
    def exchange_time(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value if value > 10_000_000_000 else value * 1000)
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)

    async def external_collector(self, symbol: str, venue: str) -> None:
        pair = self.external_pair(symbol, venue)
        key = (symbol, venue)
        state = self.external_books.setdefault(key, {"bids": {}, "asks": {}, "status": "connecting", "eventTime": None, "receivedAt": 0})
        trade_state = self.external_trade_status.setdefault(key, {"status": "connecting", "connected": False, "lastMessageAt": None, "lastTradeAt": None, "reconnects": 0, "error": None})
        trades = self.external_trades.setdefault(symbol, deque(maxlen=5000))
        seen_queue, seen_ids = self.external_trade_ids.setdefault(key, (deque(maxlen=10_000), set()))
        url = "wss://ws.kraken.com/v2" if venue == "Kraken" else "wss://advanced-trade-ws.coinbase.com"
        reconnect_delay = 1.0
        while True:
            try:
                state.update(status="connecting", bids={}, asks={})
                trade_state.update(status="connecting", connected=False, error=None)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=12, close_timeout=5, max_size=8 * 2**20, max_queue=2048) as socket:
                    if trade_state.get("lastMessageAt") is not None:
                        trade_state["reconnects"] += 1
                    connected_since = time.monotonic()
                    if venue == "Kraken":
                        await socket.send(json.dumps({"method": "subscribe", "params": {"channel": "book", "symbol": [pair], "depth": 100, "snapshot": True}}))
                        await socket.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": [pair], "snapshot": True}}))
                    else:
                        # Coinbase heartbeats keep otherwise quiet subscriptions alive and
                        # let us distinguish a healthy idle feed from a dead socket.
                        for channel in ("level2", "market_trades", "heartbeats"):
                            await socket.send(json.dumps({"type": "subscribe", "product_ids": [pair], "channel": channel}))
                    async for raw in socket:
                        message = json.loads(raw)
                        received = int(time.time() * 1000)
                        trade_state["lastMessageAt"] = received
                        if venue == "Kraken":
                            channel, rows, message_type = message.get("channel"), message.get("data", []), message.get("type")
                            if message.get("success") is False:
                                error = str(message.get("error", "subscription rejected"))
                                state.update(status="unavailable", error=error)
                                trade_state.update(status="unavailable", connected=False, error=error)
                                log.info("Kraken spot subscription rejected", extra={"symbol": symbol, "error": error})
                                continue
                            if message.get("method") == "subscribe" and message.get("success") is True:
                                result_channel = message.get("result", {}).get("channel")
                                if result_channel == "trade":
                                    trade_state.update(status="live", connected=True, error=None)
                                continue
                            if channel == "heartbeat":
                                # A socket heartbeat proves transport health, not that
                                # the requested product's trade subscription was accepted.
                                continue
                            for row in rows:
                                if channel == "book":
                                    if message_type == "snapshot":
                                        state["bids"] = {float(x["price"]): float(x["qty"]) for x in row.get("bids", []) if float(x["qty"]) > 0}
                                        state["asks"] = {float(x["price"]): float(x["qty"]) for x in row.get("asks", []) if float(x["qty"]) > 0}
                                        state["bids"] = dict(sorted(state["bids"].items(), reverse=True)[:MAX_AGGREGATED_BOOK_LEVELS])
                                        state["asks"] = dict(sorted(state["asks"].items())[:MAX_AGGREGATED_BOOK_LEVELS])
                                    else:
                                        for side in ("bids", "asks"):
                                            for update in row.get(side, []):
                                                price, qty = float(update["price"]), float(update["qty"])
                                                if qty:
                                                    state[side][price] = qty
                                                else:
                                                    state[side].pop(price, None)
                                                if len(state[side]) > MAX_AGGREGATED_BOOK_LEVELS:
                                                    worst = min(state[side]) if side == "bids" else max(state[side])
                                                    state[side].pop(worst, None)
                                    state.update(status="live", eventTime=self.exchange_time(row.get("timestamp", received)), receivedAt=received)
                                elif channel == "trade":
                                    stamp = self.exchange_time(row["timestamp"])
                                    trade_id = str(row.get("trade_id", f"{stamp}:{row['price']}:{row['qty']}:{row['side']}"))
                                    if trade_id in seen_ids:
                                        continue
                                    if len(seen_queue) == seen_queue.maxlen:
                                        seen_ids.discard(seen_queue[0])
                                    seen_queue.append(trade_id)
                                    seen_ids.add(trade_id)
                                    trades.append(Trade(id=trade_id, time=stamp, price=float(row["price"]), quantity=float(row["qty"]), side=row["side"], source=venue))
                                    trade_state.update(status="live", connected=True, lastTradeAt=received)
                        else:
                            if message.get("type") == "error":
                                error = str(message.get("message", "subscription rejected"))
                                state.update(status="unavailable", error=error)
                                trade_state.update(status="unavailable", connected=False, error=error)
                                log.info("Coinbase pair/channel unavailable", extra={"symbol": symbol, "error": message.get("message")})
                                continue
                            channel = message.get("channel")
                            if channel == "subscriptions":
                                subscriptions = message.get("events", [])
                                active = {
                                    channel_name
                                    for event in subscriptions
                                    for channel_name, products in event.get("subscriptions", {}).items()
                                    if pair in products
                                }
                                if "market_trades" in active:
                                    trade_state.update(status="live", connected=True, error=None)
                                continue
                            if channel == "heartbeats":
                                # Keep the market-trade subscription status independent
                                # from the socket heartbeat channel.
                                continue
                            for event in message.get("events", []):
                                if channel == "l2_data":
                                    product = event.get("product_id")
                                    if product != pair:
                                        continue
                                    if event.get("type") == "snapshot":
                                        state["bids"], state["asks"] = {}, {}
                                    for update in event.get("updates", []):
                                        side = "bids" if update.get("side", "").lower() == "bid" else "asks"
                                        price, qty = float(update["price_level"]), float(update["new_quantity"])
                                        if qty:
                                            state[side][price] = qty
                                        else:
                                            state[side].pop(price, None)
                                        if len(state[side]) > MAX_AGGREGATED_BOOK_LEVELS:
                                            worst = min(state[side]) if side == "bids" else max(state[side])
                                            state[side].pop(worst, None)
                                    if event.get("type") == "snapshot":
                                        state["bids"] = dict(sorted(state["bids"].items(), reverse=True)[:MAX_AGGREGATED_BOOK_LEVELS])
                                        state["asks"] = dict(sorted(state["asks"].items())[:MAX_AGGREGATED_BOOK_LEVELS])
                                    state.update(status="live", eventTime=self.exchange_time(event.get("event_time", received)), receivedAt=received)
                                elif channel == "market_trades":
                                    for row in event.get("trades", []):
                                        if row.get("product_id") != pair:
                                            continue
                                        stamp = self.exchange_time(row.get("time", received))
                                        maker_side = row.get("side", "BUY").lower()
                                        trade_id = str(row.get("trade_id", f"{stamp}:{row['price']}:{row['size']}:{maker_side}"))
                                        if trade_id in seen_ids:
                                            continue
                                        if len(seen_queue) == seen_queue.maxlen:
                                            seen_ids.discard(seen_queue[0])
                                        seen_queue.append(trade_id)
                                        seen_ids.add(trade_id)
                                        # Advanced Trade reports the maker side; invert it to
                                        # keep the terminal's side field as aggressor direction.
                                        trades.append(Trade(id=trade_id, time=stamp, price=float(row["price"]), quantity=float(row["size"]), side="sell" if maker_side == "buy" else "buy", source=venue))
                                        trade_state.update(status="live", connected=True, lastTradeAt=received)
                state.update(status="reconnecting", bids={}, asks={})
                trade_state.update(status="reconnecting", connected=False)
                reconnect_delay = 1.0 if time.monotonic() - connected_since >= 20 else min(reconnect_delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                state["status"] = "unavailable" if not state["receivedAt"] else "reconnecting"
                trade_state.update(status="reconnecting", connected=False, error="connection interrupted")
                log.exception("external spot stream disconnected", extra={"symbol": symbol, "venue": venue})
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            await asyncio.sleep(reconnect_delay)

    async def book_stream(self, market: str, symbol: str, state: dict[str, Any]) -> None:
        endpoint = SPOT_WS
        stream = f"{symbol.lower()}@depth@100ms"
        url = f"{endpoint}?streams={stream}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as socket:
                    # Subscribe first, then fetch a snapshot while Binance buffers depth diffs.
                    # This avoids losing updates between the REST snapshot and WS handshake.
                    snapshot_limit = 1000
                    base, path = self.base_and_path(market, "depth")
                    snapshot = await self.request(base, path, {"symbol": symbol, "limit": snapshot_limit})
                    async with state["lock"]:
                        state["bids"] = {float(p): float(q) for p, q in snapshot["bids"] if float(q) > 0}
                        state["asks"] = {float(p): float(q) for p, q in snapshot["asks"] if float(q) > 0}
                        state["lastUpdateId"] = int(snapshot["lastUpdateId"])
                        state["status"] = "syncing"

                    async for raw in socket:
                        message = json.loads(raw)
                        data = message.get("data", message)
                        first = int(data.get("U", data.get("u", 0)))
                        final = int(data.get("u", 0))
                        async with state["lock"]:
                            last = state["lastUpdateId"]
                            if final <= last:
                                continue
                            if state["status"] != "live":
                                # Binance requires the first event to bridge lastUpdateId + 1.
                                if not (first <= last + 1 <= final):
                                    continue
                            elif first > last + 1:
                                # A missing sequence invalidates this local book; reconnect and resnapshot.
                                state["status"] = "syncing"
                                break

                            for key, side in (("b", "bids"), ("a", "asks")):
                                for price_text, qty_text in data.get(key, []):
                                    price, qty = float(price_text), float(qty_text)
                                    if qty == 0:
                                        state[side].pop(price, None)
                                    else:
                                        state[side][price] = qty
                            state["lastUpdateId"] = final
                            state["eventTime"] = int(data.get("E", int(time.time() * 1000)))
                            state["receivedAt"] = int(time.time() * 1000)
                            state["status"] = "live"
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("order book websocket failed", extra={"symbol": symbol, "market": market})
                state["status"] = "stale"
                await asyncio.sleep(1)

    async def event_stream(self, market: str, symbol: str, interval: str):
        endpoint = SPOT_WS
        streams = [f"{symbol.lower()}@aggTrade", f"{symbol.lower()}@kline_{interval}", f"{symbol.lower()}@ticker"]
        url = f"{endpoint}?streams={'/'.join(streams)}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as socket:
                    async for raw in socket:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        stream = msg.get("stream", "")
                        received = int(time.time() * 1000)
                        if "aggTrade" in stream or "aggTrade" in data:
                            trade = Trade(id=int(data["a"]), time=int(data["T"]), price=float(data["p"]), quantity=float(data["q"]), side="sell" if data["m"] else "buy")
                            key = (market, symbol)
                            sample = self.samples.setdefault(key, deque(maxlen=5000))
                            if not sample or sample[-1].id != trade.id:
                                sample.append(trade)
                            if self.flow_store:
                                self.flow_store.enqueue(symbol, trade, received)
                            yield {"type": "trade", "data": trade.model_dump()}
                        elif "kline" in stream or "kline" in data:
                            row = data["k"]
                            candle = Candle(time=int(row["t"] // 1000), open=float(row["o"]), high=float(row["h"]), low=float(row["l"]), close=float(row["c"]), volume=float(row["v"]))
                            yield {"type": "candle", "data": candle.model_dump(), "closed": bool(row.get("x", False))}
                        elif "ticker" in stream:
                            event = int(data.get("E", received))
                            snapshot = MarketSnapshot(symbol=symbol, price=float(data["c"]), change24h=float(data["P"]), volume24h=float(data["v"]), high24h=float(data["h"]), low24h=float(data["l"]), source=self.market_source(market), eventTime=event, receivedAt=received, latencyMs=max(0, received-event), status="live")
                            yield {"type": "market", "data": snapshot.model_dump()}
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market websocket disconnected", extra={"symbol": symbol, "market": market})
                await asyncio.sleep(1)

service: MarketService

@asynccontextmanager
async def lifespan(_: FastAPI):
    global service
    service = MarketService()
    service.flow_store = FlowStore()
    await asyncio.to_thread(service.flow_store.initialize)
    writer_task = asyncio.create_task(service.flow_store.writer())
    collector_task = asyncio.create_task(service.collect_all_trades())
    yield
    collector_task.cancel()
    for task in service.external_tasks.values():
        task.cancel()
    await asyncio.gather(*service.external_tasks.values(), return_exceptions=True)
    recovery_tasks = list(service.recovery_tasks)
    for task in recovery_tasks:
        task.cancel()
    await asyncio.gather(*recovery_tasks, return_exceptions=True)
    try:
        await asyncio.wait_for(service.flow_store.queue.join(), timeout=5)
    except asyncio.TimeoutError:
        log.warning("flow store shutdown timed out with queued trades", extra={"queued": service.flow_store.queue.qsize()})
    writer_task.cancel()
    await asyncio.gather(collector_task, writer_task, return_exceptions=True)
    if service.flow_store:
        await asyncio.to_thread(service.flow_store.close)
    for state in service.books.values():
        task = state.get("task")
        if task:
            task.cancel()
    await service.close()

app = FastAPI(title="NEXUS Market API", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","), allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])

def verify_market_token(token: str, expected_use: str) -> bool:
    secret = os.getenv("NEXUS_MARKET_TOKEN_SECRET", "")
    if len(secret) < 32:
        return False
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], issuer="nexus-terminal-web", audience="nexus-market-api")
        return claims.get("token_use") == expected_use and isinstance(claims.get("email"), str)
    except jwt.PyJWTError:
        return False

@app.middleware("http")
async def require_market_auth(request: Request, call_next):
    if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    if len(os.getenv("NEXUS_MARKET_TOKEN_SECRET", "")) < 32:
        return JSONResponse(status_code=503, content={"detail": "Market API authentication is not configured"})
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or not verify_market_token(token, "api"):
        return JSONResponse(status_code=401, content={"detail": "Valid market API token required"})
    return await call_next(request)

def validate_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"MVP supports only: {', '.join(SYMBOLS)}")
    return symbol

def validate_market(market: str) -> str:
    configured = os.getenv("NEXUS_MARKET_MODE", "spot").strip().lower()
    if configured != "spot":
        raise HTTPException(500, "Only the Spot data feed is configured on this service")
    if market != "spot":
        raise HTTPException(400, "This terminal provides Binance Spot data only")
    return "spot"

@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexus-market-api", "symbols": SYMBOLS, "time": int(time.time() * 1000)}

@app.get("/api/markets")
async def markets(market: str = Query("spot")):
    market = validate_market(market)
    results = await asyncio.gather(*(service.ticker(symbol, market) for symbol in SYMBOLS), return_exceptions=True)
    unavailable = []
    for index, item in enumerate(results):
        if isinstance(item, Exception):
            log.warning("market ticker unavailable", extra={"symbol": SYMBOLS[index], "market": market, "error": str(item)})
            unavailable.append({"symbol": SYMBOLS[index], "reason": str(item) or type(item).__name__})
    return {"markets": [item.model_dump() for item in results if isinstance(item, MarketSnapshot)], "unavailable": unavailable}

@app.get("/api/scanner-flow")
async def scanner_flow():
    """Observed Binance notional flow for the 25 supported Spot assets."""
    store = service.flow_store
    now_ms = int(time.time() * 1000)
    bucket_ms = 15 * 60_000
    current_bucket = (now_ms // bucket_ms) * bucket_ms
    if store is None:
        raise HTTPException(503, "Flow storage is not ready")
    grouped = await asyncio.to_thread(store.scanner_flow, current_bucket - 3 * bucket_ms, now_ms)
    assets: dict[str, dict[str, Any]] = {}
    elapsed = max(0, now_ms - current_bucket)
    for symbol in SYMBOLS:
        buckets = {int(item["time"]): item for item in grouped.get(symbol, [])}
        current = buckets.get(current_bucket)
        current_notional = current["buyNotional"] + current["sellNotional"] if current else None
        cvd_notional = current["buyNotional"] - current["sellNotional"] if current else None
        previous = [buckets.get(current_bucket - index * bucket_ms) for index in (1, 2, 3)]
        relative_flow = None
        if elapsed >= 120_000 and current_notional is not None and all(item is not None for item in previous):
            baseline = sum(item["buyNotional"] + item["sellNotional"] for item in previous if item is not None) / 3
            expected = baseline * min(1.0, elapsed / bucket_ms)
            if expected > 0:
                relative_flow = current_notional / expected
        latest_bucket = max(buckets, default=None)
        assets[symbol] = {
            "symbol": symbol,
            "notional15m": current_notional,
            "cvdNotional15m": cvd_notional,
            "relativeFlow15m": relative_flow,
            "observedBuckets": len(buckets),
            "lastObservedBucket": latest_bucket,
        }
    return {
        "assets": assets,
        "bucketStartedAt": current_bucket,
        "source": "Binance Spot aggTrades persistidos",
        "receivedAt": now_ms,
        "relativeFlowMethod": "Fluxo nocional do bloco UTC de 15m em andamento, ajustado pelo tempo decorrido, versus média dos 3 blocos anteriores. Requer 3 blocos anteriores observados e pelo menos 2 minutos do bloco atual.",
    }

@app.get("/api/context")
async def market_context():
    return await service.market_context()

@app.get("/api/candles")
async def candles(symbol: str = Query("BTCUSDT"), interval: str = Query("5m"), market: str = Query("spot"), limit: int = Query(240, ge=10, le=500)):
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in INTERVALS:
        raise HTTPException(400, "Unsupported candle interval")
    base, path = service.base_and_path(market, "klines")
    try:
        rows = await service.request(base, path, {"symbol": symbol, "interval": interval, "limit": limit})
    except (httpx.HTTPError, HTTPException) as error:
        log.exception("candle request failed", extra={"symbol": symbol, "market": market, "interval": interval})
        raise HTTPException(502, f"Unable to load Binance candles: {error}") from error
    return {"candles": [Candle(time=int(r[0] // 1000), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=float(r[5])).model_dump() for r in rows], "source": service.market_source(market), "receivedAt": int(time.time() * 1000), "coverage": "exchange historical OHLCV"}

def _period_starts(timestamp_ms: int) -> tuple[int, int, int]:
    moment = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return int(day.timestamp() * 1000), int(week.timestamp() * 1000), int(month.timestamp() * 1000)

def _vwap_add(totals: list[float], row: list[Any]) -> None:
    base_volume = float(row[5])
    quote_volume = float(row[7])
    if base_volume > 0 and quote_volume >= 0:
        totals[0] += quote_volume
        totals[1] += base_volume

def _period_extreme(rows: list[list[Any]], start_ms: int, end_ms: int, field: int, mode: str) -> float | None:
    values = [float(row[field]) for row in rows if start_ms <= int(row[0]) < end_ms and int(row[6]) + 1 <= end_ms]
    if not values:
        return None
    return max(values) if mode == "high" else min(values)

@app.get("/api/chart-overlays")
async def chart_overlays(symbol: str = Query("BTCUSDT"), interval: str = Query("5m"), market: str = Query("spot")):
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        raise HTTPException(400, "Unsupported chart overlay interval")
    base, path = service.base_and_path(market, "klines")
    try:
        now_ms = int(time.time() * 1000)
        if interval == "1d":
            daily_rows = await service.request(base, path, {"symbol": symbol, "interval": "1d", "limit": 500})
            target_rows = daily_rows
            source_rows = daily_rows
            minute_rows: list[list[Any]] = []
        else:
            history_start = ((now_ms - 1470 * 3_600_000) // 86_400_000) * 86_400_000
            target_rows, hourly_rows, daily_rows = await asyncio.gather(
                service.request(base, path, {"symbol": symbol, "interval": interval, "limit": 240}),
                service.request(base, path, {"symbol": symbol, "interval": "1h", "startTime": history_start, "limit": 1500}),
                service.request(base, path, {"symbol": symbol, "interval": "1d", "limit": 500}),
            )
            source_rows = hourly_rows
            first_time_ms = int(target_rows[0][0]) if target_rows else now_ms
            hour_start = (first_time_ms // 3_600_000) * 3_600_000
            minute_rows = []
            if first_time_ms > hour_start:
                minute_rows = await service.request(base, path, {
                    "symbol": symbol, "interval": "1m", "startTime": hour_start,
                    "endTime": first_time_ms - 1, "limit": 60,
                })

        # Seed the anchored curves before the first displayed candle. Daily bars
        # fill the older history; hourly bars provide detail through the chart range.
        first_time_ms = int(target_rows[0][0]) if target_rows else now_ms
        day_key, week_key, month_key = _period_starts(first_time_ms)
        anchor_keys = {"daily": day_key, "weekly": week_key, "monthly": month_key}
        totals = {name: [0.0, 0.0] for name in anchor_keys}
        if interval != "1d":
            history_start = int(source_rows[0][0]) if source_rows else first_time_ms
            earliest_anchor = min(anchor_keys.values())
            seed_rows = [row for row in daily_rows if int(row[0]) >= earliest_anchor and int(row[6]) + 1 <= history_start]
            seed_rows += [row for row in source_rows if int(row[0]) >= earliest_anchor and int(row[6]) + 1 <= first_time_ms]
            seed_rows += [row for row in minute_rows if int(row[0]) >= (first_time_ms // 3_600_000) * 3_600_000 and int(row[6]) + 1 <= first_time_ms]
            seed_rows.sort(key=lambda row: int(row[0]))
            for row in seed_rows:
                row_day, row_week, row_month = _period_starts(int(row[0]))
                for name, key in (("daily", row_day), ("weekly", row_week), ("monthly", row_month)):
                    if key == anchor_keys[name]:
                        _vwap_add(totals[name], row)

        last_keys = anchor_keys.copy()
        series: dict[str, list[dict[str, float | int]]] = {"daily": [], "weekly": [], "monthly": []}
        for row in target_rows:
            time_ms = int(row[0])
            keys = dict(zip(("daily", "weekly", "monthly"), _period_starts(time_ms)))
            for name in totals:
                if keys[name] != last_keys[name]:
                    totals[name] = [0.0, 0.0]
                    last_keys[name] = keys[name]
                _vwap_add(totals[name], row)
                if totals[name][1] > 0:
                    series[name].append({"time": time_ms // 1000, "value": totals[name][0] / totals[name][1]})

        if interval == "1d" and len(target_rows) > 240:
            visible_start = int(target_rows[-240][0]) // 1000
            series = {name: [point for point in points if point["time"] >= visible_start] for name, points in series.items()}

        today_start, week_start, month_start = _period_starts(now_ms)
        previous_day_start = today_start - 86_400_000
        previous_week_start = week_start - 7 * 86_400_000
        previous_month_dt = datetime.fromtimestamp(month_start / 1000, timezone.utc)
        previous_month_dt = (previous_month_dt.replace(day=1) - timedelta(days=1)).replace(day=1)
        previous_month_start = int(previous_month_dt.timestamp() * 1000)
        previous = {
            "day": {
                "high": _period_extreme(daily_rows, previous_day_start, today_start, 2, "high"),
                "low": _period_extreme(daily_rows, previous_day_start, today_start, 3, "low"),
            },
            "week": {
                "high": _period_extreme(daily_rows, previous_week_start, week_start, 2, "high"),
                "low": _period_extreme(daily_rows, previous_week_start, week_start, 3, "low"),
            },
            "month": {
                "high": _period_extreme(daily_rows, previous_month_start, month_start, 2, "high"),
                "low": _period_extreme(daily_rows, previous_month_start, month_start, 3, "low"),
            },
        }
        return {"symbol": symbol, "interval": interval, "timezone": "UTC", "vwap": series, "previous": previous, "source": f"{service.market_source(market)} klines quote volume / base volume", "receivedAt": int(time.time() * 1000)}
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Unable to load anchored VWAP and previous period levels: {error}") from error

@app.get("/api/book")
async def order_book(symbol: str = Query("BTCUSDT"), market: str = Query("spot"), limit: int = Query(16, ge=5, le=100)):
    return (await service.book(validate_symbol(symbol), validate_market(market), limit)).model_dump()

@app.get("/api/trades")
async def trades(symbol: str = Query("BTCUSDT"), market: str = Query("spot"), limit: int = Query(24, ge=1, le=100)):
    symbol, market = validate_symbol(symbol), validate_market(market)
    service.ensure_external_collectors(symbol)
    sample = service.samples.get((market, symbol), deque())
    stored = await asyncio.to_thread(service.flow_store.recent, symbol, limit) if service.flow_store else []
    if len(stored) >= limit:
        binance_rows = stored
    elif len(sample) >= limit:
        binance_rows = list(sample)[-limit:]
        binance_rows.reverse()
    else:
        base, path = service.base_and_path(market, "trades")
        response = await service.request(base, path, {"symbol": symbol, "limit": limit})
        fetched = [Trade(id=int(r["a"]), time=int(r["T"]), price=float(r["p"]), quantity=float(r["q"]), side="sell" if r["m"] else "buy") for r in response]
        binance_rows = sorted({trade.id: trade for trade in [*stored, *list(sample), *fetched]}.values(), key=lambda trade: (trade.time, str(trade.id)), reverse=True)[:limit]
        known = {trade.id for trade in sample}
        for trade in reversed(binance_rows):
            if trade.id not in known:
                sample.append(trade)
    external_rows = list(service.external_trades.get(symbol, []))
    unique = {(trade.source, str(trade.id)): trade for trade in [*binance_rows, *external_rows]}
    rows = sorted(unique.values(), key=lambda trade: (trade.time, str(trade.id)), reverse=True)[:limit]
    binance_state = service.flow_quality[symbol]
    binance_status = "live" if binance_state["connected"] else "reconnecting"
    sources = {"Binance": binance_status}
    trade_health = {"Binance": {"status": binance_status, "lastTradeAt": binance_state.get("lastReceivedAt")}}
    for venue in ("Kraken", "Coinbase"):
        health = service.external_trade_status.get((symbol, venue))
        status = health["status"] if health else "connecting"
        if health and health.get("connected") and health.get("lastMessageAt") and int(time.time() * 1000) - health["lastMessageAt"] > 30_000:
            status = "stale"
        sources[venue] = status
        trade_health[venue] = {"status": status, "lastTradeAt": health.get("lastTradeAt") if health else None, "reconnects": health.get("reconnects", 0) if health else 0, "error": health.get("error") if health else None}
    return {"trades": [trade.model_dump() for trade in rows], "source": "Binance + Kraken + Coinbase Spot", "sources": sources, "tradeHealth": trade_health, "receivedAt": int(time.time() * 1000), "coverage": "Negociações públicas observadas por exchange; não representa o mercado global. USD ≈ USDT."}

@app.get("/api/flow-quality")
async def flow_quality(symbol: str = Query("BTCUSDT")):
    symbol = validate_symbol(symbol)
    state = service.flow_quality[symbol].copy()
    summary = await asyncio.to_thread(service.flow_store.summary, symbol) if service.flow_store else {"storedTrades": 0, "oldestTradeTime": None, "newestTradeTime": None}
    store = service.flow_store
    return {
        "symbol": symbol,
        **state,
        **summary,
        "queueDepth": store.queue.qsize() if store else 0,
        "queueDropped": store.dropped if store else 0,
        "persistedThisProcess": store.saved if store else 0,
        "storage": store.storage if store else "unavailable",
        "storagePath": str(store.path) if store and store.storage == "sqlite" else None,
        "storageDurable": store.durable if store else False,
        "rawRetentionHours": store.retention_hours if store else None,
        "aggregateRetentionDays": store.aggregate_retention_days if store else None,
        "source": "Binance Spot aggTrade combined stream",
        "receivedAt": int(time.time() * 1000),
    }

@app.get("/api/flow-history")
async def flow_history(
    symbol: str = Query("BTCUSDT"),
    interval: int = Query(5, ge=1, le=60),
    start_ms: int | None = Query(None, ge=0),
    end_ms: int | None = Query(None, ge=0),
):
    """Return compact, persisted flow bars built only from observed aggTrades."""
    symbol = validate_symbol(symbol)
    if interval not in {1, 5, 15, 30, 60}:
        raise HTTPException(400, "Interval must be one of 1, 5, 15, 30 or 60 minutes")
    store = service.flow_store
    if store is None:
        raise HTTPException(503, "Flow storage is not ready")
    now_ms = int(time.time() * 1000)
    end = end_ms if end_ms is not None else now_ms
    start = start_ms if start_ms is not None else end - 6 * 60 * 60_000
    if start >= end:
        raise HTTPException(400, "start_ms must be before end_ms")
    max_window = store.aggregate_retention_days * 86_400_000
    if end - start > max_window:
        raise HTTPException(400, f"Requested range exceeds aggregate retention ({store.aggregate_retention_days} days)")
    if start < now_ms - max_window:
        raise HTTPException(400, "Requested start is older than configured aggregate retention")
    try:
        bars = await asyncio.to_thread(store.flow_history, symbol, start, end, interval)
    except Exception as error:
        log.exception("flow history query failed", extra={"symbol": symbol})
        raise HTTPException(503, "Persisted flow history is temporarily unavailable") from error
    observed_start = max(start, bars[0]["time"] * 1000) if bars else None
    expected_buckets = max(0, (end - observed_start + interval * 60_000 - 1) // (interval * 60_000)) if observed_start else 0
    return {
        "symbol": symbol,
        "intervalMinutes": interval,
        "bars": bars,
        "source": "Observed Binance Spot aggTrades aggregated into UTC time buckets",
        "coverage": {
            "requestedStart": start,
            "requestedEnd": end,
            "observedStart": observed_start,
            "observedBuckets": len(bars),
            "expectedBucketsSinceObservedStart": expected_buckets,
            "estimatedPercent": round(min(100, len(bars) / expected_buckets * 100), 2) if expected_buckets else None,
            "interpretation": "Estimate based on non-empty buckets; quiet minutes and collector gaps cannot be distinguished.",
        },
        "receivedAt": now_ms,
    }

@app.get("/api/reversal-signals")
async def reversal_signals(
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("5m"),
    market: str = Query("spot"),
    min_sigma: float = Query(1.0, ge=1.0, le=3),
    touch_tolerance: float = Query(0.001, ge=0, le=0.02),
    min_score: int = Query(50, ge=0, le=100),
    cooldown: int = Query(10, ge=0, le=500),
    buy_enabled: bool = Query(True),
    sell_enabled: bool = Query(True),
    garch_enabled: bool = Query(True),
    hv_enabled: bool = Query(True),
    volume_enabled: bool = Query(True),
):
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in INTERVALS:
        raise HTTPException(400, "Unsupported candle interval")
    base, path = service.base_and_path(market, "klines")
    try:
        candles_limit = 400 if interval == "1d" else 240
        rows, daily_rows = await asyncio.gather(
            service.request(base, path, {"symbol": symbol, "interval": interval, "limit": candles_limit}),
            service.request(base, path, {"symbol": symbol, "interval": "1d", "limit": 400}),
        )
        # Binance's final REST kline is still open. It cannot confirm a signal.
        candles = [
            {"time": int(row[0] // 1000), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
            for row in rows[:-1]
        ]
        daily_times = [int(row[0] // 1000) for row in daily_rows]
        zones_cache: dict[int, dict[str, Any]] = {}
        zones_by_candle: list[dict[str, Any] | None] = []
        for candle in candles:
            day_index = -1
            for index, day_start in enumerate(daily_times):
                if day_start + 86_400 <= candle["time"]:
                    day_index = index
                else:
                    break
            if day_index < 0 or day_index + 1 >= len(daily_rows):
                zones_by_candle.append(None)
                continue
            if day_index not in zones_cache:
                # Include the next (unfinished) daily bar so calculate_zones
                # excludes it and anchors to the last close available then.
                history = [{"time": int(row[0] // 1000), "close": float(row[4])} for row in daily_rows[:day_index + 2]]
                zones_cache[day_index] = calculate_zones(symbol, history)
            zones_by_candle.append(zones_cache[day_index])
        return build_reversal_signals(
            symbol, interval, candles, zones_by_candle,
            min_sigma=min_sigma, touch_tolerance=touch_tolerance, min_score=min_score, cooldown=cooldown,
            buy_enabled=buy_enabled, sell_enabled=sell_enabled,
            garch_enabled=garch_enabled, hv_enabled=hv_enabled, volume_enabled=volume_enabled,
        )
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as error:
        raise HTTPException(502, f"Unable to calculate reversal signals: {error}") from error

@app.get("/api/reversal-evaluation")
async def reversal_evaluation(
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("5m"),
    market: str = Query("spot"),
    lookahead: int = Query(12, ge=1, le=100),
    fee_bps: float = Query(10, ge=0, le=100),
    slippage_bps: float = Query(5, ge=0, le=100),
):
    """Evaluate closed-candle reversal signals against later OHLCV, with explicit costs."""
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in INTERVALS:
        raise HTTPException(400, "Unsupported candle interval")
    base, path = service.base_and_path(market, "klines")
    try:
        candles_limit = 1000
        rows, daily_rows = await asyncio.gather(
            service.request(base, path, {"symbol": symbol, "interval": interval, "limit": candles_limit}),
            service.request(base, path, {"symbol": symbol, "interval": "1d", "limit": 400}),
        )
        candles = [
            {"time": int(row[0] // 1000), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
            for row in rows[:-1]
        ]
        daily_times = [int(row[0] // 1000) for row in daily_rows]
        zones_cache: dict[int, dict[str, Any]] = {}
        zones_by_candle: list[dict[str, Any] | None] = []
        for candle in candles:
            day_index = -1
            for index, day_start in enumerate(daily_times):
                if day_start + 86_400 <= candle["time"]:
                    day_index = index
                else:
                    break
            if day_index < 0 or day_index + 1 >= len(daily_rows):
                zones_by_candle.append(None)
                continue
            if day_index not in zones_cache:
                history = [{"time": int(row[0] // 1000), "close": float(row[4])} for row in daily_rows[:day_index + 2]]
                zones_cache[day_index] = calculate_zones(symbol, history)
            zones_by_candle.append(zones_cache[day_index])

        generated = build_reversal_signals(
            symbol, interval, candles, zones_by_candle,
            min_sigma=1.0, min_score=50, cooldown=10, volume_enabled=True,
        )
        fee_and_slippage_bps = 2 * (fee_bps + slippage_bps)
        outcomes: list[dict[str, Any]] = []
        expired = 0
        index_by_time = {candle["time"]: index for index, candle in enumerate(candles)}
        for signal in generated["signals"]:
            signal_index = index_by_time.get(signal["time"])
            if signal_index is None or signal_index + lookahead >= len(candles):
                expired += 1
                continue
            entry = candles[signal_index + 1]["open"]
            exit_price = candles[signal_index + lookahead]["close"]
            if entry <= 0 or exit_price <= 0:
                continue
            gross_bps = ((exit_price / entry) - 1) * 10_000
            if signal["direction"] == "SELL":
                gross_bps = -gross_bps
            net_bps = gross_bps - fee_and_slippage_bps
            outcomes.append({
                "time": signal["time"], "direction": signal["direction"], "signalPrice": signal["price"],
                "entryPrice": entry, "exitPrice": exit_price, "grossBps": round(gross_bps, 2),
                "estimatedRoundTripCostsBps": round(fee_and_slippage_bps, 2), "netBps": round(net_bps, 2),
                "win": net_bps > 0,
            })
        net_returns = sorted(item["netBps"] for item in outcomes)
        count = len(net_returns)
        median = (net_returns[(count - 1) // 2] + net_returns[count // 2]) / 2 if count else None
        summary = {
            "generatedSignals": len(generated["signals"]), "evaluatedSignals": count, "notMaturedSignals": expired,
            "wins": sum(1 for value in net_returns if value > 0),
            "winRatePercent": round(sum(1 for value in net_returns if value > 0) / count * 100, 2) if count else None,
            "averageNetBps": round(sum(net_returns) / count, 2) if count else None,
            "medianNetBps": round(median, 2) if median is not None else None,
        }
        return {
            "symbol": symbol, "market": market, "interval": interval,
            "settings": {"minSigma": 1.0, "minScore": 50, "cooldown": 10, "volumeEnabled": True, "lookaheadCandles": lookahead, "feeBpsPerSide": fee_bps, "slippageBpsPerSide": slippage_bps},
            "summary": summary, "outcomes": outcomes,
            "source": "Binance Spot OHLCV; entry is next candle open and exit is close after the lookahead horizon",
            "limitations": ["Only the most recent 40 qualified signals are evaluated.", "Overlapping signals are counted independently.", "Fees and slippage are user-supplied estimates; funding, spread, market impact and execution failures are excluded.", "This is a retrospective estimate, not a live signal or a promise of future results."],
            "receivedAt": int(time.time() * 1000),
        }
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as error:
        raise HTTPException(502, f"Unable to evaluate reversal signals: {error}") from error

@app.get("/api/volatility")
async def volatility(symbol: str = Query("BTCUSDT"), market: str = Query("spot")):
    symbol, market = validate_symbol(symbol), validate_market(market)
    base, path = service.base_and_path(market, "klines")
    try:
        rows = await service.request(base, path, {"symbol": symbol, "interval": "1d", "limit": 400})
        daily = [{"time": int(row[0] // 1000), "close": float(row[4])} for row in rows]
        return calculate_zones(symbol, daily)
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as error:
        raise HTTPException(502, f"Daily volatility data unavailable: {error}") from error

@app.get("/api/market-info")
async def market_info(symbol: str = Query("BTCUSDT"), market: str = Query("spot")):
    symbol, market = validate_symbol(symbol), validate_market(market)
    try:
        price = await service.request(SPOT_DATA, "/api/v3/ticker/price", {"symbol": symbol})
        return {
            "market": "spot", "source": "Binance Spot", "price": float(price["price"]),
            "markPrice": None, "indexPrice": None, "fundingRate": None,
            "openInterest": None, "nextFundingTime": None,
            "receivedAt": int(time.time() * 1000), "status": "live",
        }
    except (httpx.HTTPError, KeyError, ValueError) as error:
        raise HTTPException(502, f"Spot data unavailable: {error}") from error

@app.get("/api/sentiment")
async def sentiment():
    try:
        response = await service.client.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        response.raise_for_status()
        item = response.json()["data"][0]
        return {"value": int(item["value"]), "label": item["value_classification"], "timestamp": int(item["timestamp"]) * 1000, "receivedAt": int(time.time() * 1000), "source": "Alternative.me", "status": "live"}
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
        log.warning("sentiment unavailable: %s", error)
        return {"value": None, "label": "Indisponível", "source": "Alternative.me", "status": "unavailable"}

@app.websocket("/ws/market")
async def market_ws(socket: WebSocket):
    allowed_origins = {origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()}
    origin = socket.headers.get("origin")
    protocols = [value.strip() for value in socket.headers.get("sec-websocket-protocol", "").split(",") if value.strip()]
    market_token = protocols[1] if len(protocols) == 2 and protocols[0] == "nexus-market-v1" else ""
    if (origin and origin not in allowed_origins) or not verify_market_token(market_token, "ws"):
        await socket.close(code=4401, reason="Authentication required")
        return
    symbol = socket.query_params.get("symbol", "BTCUSDT").upper()
    requested_market = socket.query_params.get("market", "spot")
    market = os.getenv("NEXUS_MARKET_MODE", "spot").strip().lower()
    if requested_market != "spot":
        await socket.close(code=1008, reason="This terminal provides Binance Spot data only")
        return
    interval = socket.query_params.get("interval", "5m")
    if symbol not in SYMBOLS or interval not in INTERVALS:
        await socket.close(code=1008, reason="Unsupported symbol, market or interval")
        return
    await socket.accept(subprotocol="nexus-market-v1")
    consumer = asyncio.create_task(_consume_market(socket, symbol, market, interval))
    producer = asyncio.create_task(_produce_book(socket, symbol, market))
    try:
        while True:
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await socket.send_json({"type": "heartbeat", "data": {"time": int(time.time() * 1000)}})
    except WebSocketDisconnect:
        pass
    finally:
        consumer.cancel()
        producer.cancel()

async def _consume_market(socket: WebSocket, symbol: str, market: str, interval: str):
    async for event in service.event_stream(market, symbol, interval):
        await socket.send_json(event)

async def _produce_book(socket: WebSocket, symbol: str, market: str):
    key = (market, symbol)
    while True:
        try:
            state = service.books.get(key)
            if state:
                await socket.send_json({"type": "book", "data": service.book_payload(symbol, state, 16).model_dump()})
            await asyncio.sleep(0.5)
        except (WebSocketDisconnect, RuntimeError):
            return
