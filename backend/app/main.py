from __future__ import annotations

import asyncio
import gzip
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
from .breakout_engine import build_breakout_signals
from .volatility import calculate_zones
from .macro_engine import MACRO_ASSETS, build_macro_snapshot

HYPERLIQUID_ASSETS: dict[str, dict[str, str]] = {
    "HL:para:2Y": {"coin": "para:2Y", "dex": "para", "category": "rates"},
    "HL:para:10Y": {"coin": "para:10Y", "dex": "para", "category": "rates"},
    "HL:para:30Y": {"coin": "para:30Y", "dex": "para", "category": "rates"},
    "HL:xyz:EWZ": {"coin": "xyz:EWZ", "dex": "xyz", "category": "equity"},
    "HL:xyz:TLT": {"coin": "xyz:TLT", "dex": "xyz", "category": "bond"},
}

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
TRADFI_INDEX_SYMBOLS = {
    "NCSIDOWJONES2USD-USDT": "Dow Jones",
    "NCSIDXY2USD-USDT": "DXY · Índice do dólar",
    "NCSIGER2USD-USDT": "GER40 · Alemanha",
    "NCSIKOSPI12USD-USDT": "KOSPI · Coreia do Sul",
    "NCSIKOSPI2USD-USDT": "KR200 · Coreia do Sul",
    "NCSINASDAQ1002USD-USDT": "NASDAQ 100",
    "NCSINIKKEI2252USD-USDT": "Nikkei 225 · Japão",
    "NCSIRUSSELL20002USD-USDT": "Russell 2000",
    "NCSISP5002USD-USDT": "S&P 500",
    "NCSKASX2USD-USDT": "ASX · Austrália",
}
TRADFI_COMMODITY_SYMBOLS = {
    "NCCOALUMINIUM2USD-USDT": "Alumínio",
    "NCCO724COPPER2USD-USDT": "Cobre",
    "NCCOGOLD2USD-USDT": "Ouro",
    "NCCOHEATINGOIL2USD-USDT": "Óleo de aquecimento",
    "NCCOLEAD2USD-USDT": "Chumbo",
    "NCCO7241NATGAS2USD-USDT": "Gás natural",
    "NCCONICKEL2USD-USDT": "Níquel",
    "NCCO1OILBRENT2USD-USDT": "Petróleo Brent",
    "NCCO1OILWTI2USD-USDT": "Petróleo WTI",
    "NCCOPALLADIUM2USD-USDT": "Paládio",
    "NCCOXPT2USD-USDT": "Platina",
    "NCCOXAG2USD-USDT": "Prata",
    "NCCOZINC2USD-USDT": "Zinco",
}
TRADFI_DAILY_CLOSES: dict[str, dict[str, Any]] = {}
TRADFI_DAILY_CLOSES_TASK: asyncio.Task[Any] | None = None
TRADFI_DAILY_CLOSES_PENDING: dict[str, dict[str, Any]] = {}
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w", "1M"}

def positive_int_setting(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default

api_request_windows: dict[str, deque[float]] = {}

class MarketService:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(headers={"User-Agent": "NexusCryptoTerminal/0.1"}, timeout=12)
        self.request_slots = asyncio.Semaphore(5)
        self.response_cache: dict[str, tuple[float, Any]] = {}
        self.response_inflight: dict[str, asyncio.Task[Any]] = {}
        self.exchange_headers = {
            "Origin": "https://www.binance.com",
            "Referer": "https://www.binance.com/",
        }
        self.books: dict[tuple[str, str], dict[str, Any]] = {}
        self.book_init_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.book_payload_cache: dict[tuple[str, int], tuple[tuple[Any, ...], BookSnapshot]] = {}
        self.market_subscribers: dict[tuple[str, str, str], set[asyncio.Queue[dict[str, Any]]]] = {}
        self.market_stream_tasks: dict[tuple[str, str, str], asyncio.Task[Any]] = {}
        self.tradfi_ticker_task: asyncio.Task[Any] | None = None
        self.hyperliquid_task: asyncio.Task[Any] | None = None
        self.tradfi_sample_task: asyncio.Task[Any] | None = None
        self.tradfi_seed_task: asyncio.Task[Any] | None = None
        self.tradfi_seed_complete = False
        self.tradfi_prices: dict[str, dict[str, Any]] = {}
        self.tradfi_samples: dict[str, deque[dict[str, Any]]] = {}
        self.tradfi_symbol_categories: dict[str, str] = {symbol: descriptor["category"] for symbol, descriptor in HYPERLIQUID_ASSETS.items()}
        self.tradfi_watchlist_subscribers: dict[str, set[WebSocket]] = {"indices": set(), "forex": set(), "commodities": set()}
        self.tradfi_macro_subscribers: set[WebSocket] = set()
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
        self.active_market_sessions = 0
        self.market_sessions_lock = asyncio.Lock()
        self.flow_quality: dict[str, dict[str, Any]] = {
            symbol: {"status": "starting", "connected": False, "sessionStartedAt": int(time.time() * 1000), "connectedAt": None, "lastEventTime": None, "lastReceivedAt": None, "lastLatencyMs": None, "events": 0, "reconnects": 0, "possibleIdGaps": 0, "backfillRowsFetched": 0, "backfillRowsQueued": 0, "backfillSkippedIds": 0, "backfillFailures": 0, "lastBackfillAt": None, "lastTradeId": None, "lastDisconnectAt": None}
            for symbol in SYMBOLS
        }

    async def close(self) -> None:
        if self.tradfi_ticker_task:
            self.tradfi_ticker_task.cancel()
            await asyncio.gather(self.tradfi_ticker_task, return_exceptions=True)
        if self.hyperliquid_task:
            self.hyperliquid_task.cancel()
            await asyncio.gather(self.hyperliquid_task, return_exceptions=True)
        if self.tradfi_sample_task:
            self.tradfi_sample_task.cancel()
            await asyncio.gather(self.tradfi_sample_task, return_exceptions=True)
        if self.tradfi_seed_task:
            self.tradfi_seed_task.cancel()
            await asyncio.gather(self.tradfi_seed_task, return_exceptions=True)
        await self.client.aclose()

    def ensure_tradfi_ticker_stream(self) -> None:
        if self.tradfi_ticker_task is None or self.tradfi_ticker_task.done():
            self.tradfi_ticker_task = asyncio.create_task(self.collect_tradfi_tickers())
        if self.tradfi_sample_task is None or self.tradfi_sample_task.done():
            self.tradfi_sample_task = asyncio.create_task(self.sample_tradfi_prices())
        if self.tradfi_seed_task is None or (self.tradfi_seed_task.done() and not self.tradfi_seed_complete):
            self.tradfi_seed_task = asyncio.create_task(self.seed_tradfi_history())
        if self.hyperliquid_task is None or self.hyperliquid_task.done():
            self.hyperliquid_task = asyncio.create_task(self.collect_hyperliquid_macro())

    async def seed_tradfi_history(self) -> None:
        try:
            catalog = await bingx_tradfi_catalog()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("BingX macro history seed could not load contract catalogue")
            return
        instruments = [item for item in catalog if item["symbol"] in MACRO_ASSETS]
        now_bucket = int(time.time() // 60) * 60_000

        async def load_symbol(instrument: dict[str, Any]):
            try:
                rows = await bingx_tradfi_request(
                    "/openApi/swap/v2/quote/klines",
                    {"symbol": instrument["symbol"], "interval": "1m", "limit": 90},
                    300,
                )
                samples = []
                for row in rows or []:
                    sample_time = (int(row["time"]) // 60_000) * 60_000
                    if sample_time >= now_bucket:
                        continue
                    samples.append((instrument["symbol"], sample_time, float(row["close"]), instrument["category"]))
                return samples
            except (httpx.HTTPError, HTTPException, ValueError, KeyError, TypeError) as error:
                log.info("BingX macro history seed unavailable", extra={"symbol": instrument["symbol"], "error": str(error)})
                return []

        loaded = await asyncio.gather(*(load_symbol(item) for item in instruments))
        rows = [sample for batch in loaded for sample in batch]
        for symbol, sample_time, price, category in rows:
            self.tradfi_samples.setdefault(symbol, deque(maxlen=7 * 24 * 60)).append({"time": sample_time, "price": price, "category": category})
        if rows and self.flow_store:
            await asyncio.to_thread(self.flow_store.save_tradfi_price_samples, rows)
        self.tradfi_seed_complete = True

    async def sample_tradfi_prices(self) -> None:
        # Align one-minute samples across contracts so cross-asset returns and correlations are comparable.
        delay = 60 - (time.time() % 60)
        await asyncio.sleep(delay)
        while True:
            sample_time = int(time.time() // 60) * 60_000
            samples: list[tuple[str, int, float, str]] = []
            for symbol, latest in tuple(self.tradfi_prices.items()):
                if sample_time - int(latest.get("receivedAt", 0)) > 90_000:
                    continue
                category = self.tradfi_symbol_categories.get(symbol)
                if not category:
                    continue
                point = {"time": sample_time, "price": float(latest["price"]), "category": category}
                self.tradfi_samples.setdefault(symbol, deque(maxlen=7 * 24 * 60)).append(point)
                samples.append((symbol, sample_time, point["price"], category))
            if samples and self.flow_store:
                try:
                    await asyncio.to_thread(self.flow_store.save_tradfi_price_samples, samples)
                except Exception:
                    log.exception("TradFi macro samples could not be persisted", extra={"count": len(samples)})
            if sample_time // (15 * 60_000) != (sample_time - 60_000) // (15 * 60_000) and self.flow_store:
                try:
                    await asyncio.to_thread(self.flow_store.prune)
                except Exception:
                    log.exception("TradFi macro sample retention cleanup failed")
            await asyncio.sleep(max(1, 60 - (time.time() % 60)))

    async def publish_tradfi_quote(self, symbol: str, price: float, event_time: int | None = None, source: str = "BingX") -> None:
        received_at = int(time.time() * 1000)
        self.tradfi_prices[symbol] = {"price": price, "eventTime": event_time or received_at, "receivedAt": received_at, "source": source}
        category = self.tradfi_symbol_categories.get(symbol)
        if not category:
            return
        reference = TRADFI_DAILY_CLOSES.get(symbol)
        today = datetime.now(timezone.utc).date().isoformat()
        previous_close = reference.get("close") if reference and reference.get("referenceDate") == today else None
        change = ((price - previous_close) / previous_close * 100) if previous_close else None
        message = {"type": "quote", "data": {
            "symbol": symbol, "price": price, "previousClose": previous_close,
            "changePercent": round(change, 4) if change is not None else None,
            "referenceTime": reference.get("candleTime") if previous_close is not None else None, "source": source,
            "eventTime": event_time, "receivedAt": received_at,
        }}
        subscribers = tuple(self.tradfi_watchlist_subscribers.get(category, ()))
        if subscribers:
            sends = [asyncio.create_task(subscriber.send_json(message)) for subscriber in subscribers]
            done, pending = await asyncio.wait(sends, timeout=1)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for subscriber, task in zip(subscribers, sends):
                if task not in done or task.cancelled() or task.exception() is not None:
                    self.tradfi_watchlist_subscribers[category].discard(subscriber)
        macro_subscribers = tuple(self.tradfi_macro_subscribers)
        if macro_subscribers:
            sends = [asyncio.create_task(subscriber.send_json(message)) for subscriber in macro_subscribers]
            done, pending = await asyncio.wait(sends, timeout=1)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for subscriber, task in zip(macro_subscribers, sends):
                if task not in done or task.cancelled() or task.exception() is not None:
                    self.tradfi_macro_subscribers.discard(subscriber)

    async def collect_tradfi_tickers(self) -> None:
        reconnect_delay = 1.0
        while True:
            try:
                instruments = await bingx_tradfi_catalog()
                self.tradfi_symbol_categories.update({item["symbol"]: item["category"] for item in instruments})
                async with websockets.connect(
                    "wss://open-api-swap.bingx.com/swap-market", ping_interval=20, ping_timeout=20,
                    open_timeout=12, close_timeout=5, max_size=2**20, max_queue=4096,
                ) as upstream:
                    connected_since = time.monotonic()
                    for instrument in instruments:
                        channel = f"{instrument['symbol']}@ticker"
                        await upstream.send(json.dumps({"id": channel, "reqType": "sub", "dataType": channel}))
                        await asyncio.sleep(0.11)
                    async for raw in upstream:
                        try:
                            text = gzip.decompress(raw).decode("utf-8") if isinstance(raw, bytes) else raw
                        except (OSError, EOFError):
                            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                        if text.strip().lower() == "ping":
                            await upstream.send("Pong")
                            continue
                        message = json.loads(text)
                        if message.get("code") not in (None, 0, "0"):
                            log.info("BingX TradFi ticker subscription response", extra={"dataType": message.get("dataType"), "code": message.get("code"), "message": message.get("msg")})
                            continue
                        data_type = str(message.get("dataType", ""))
                        if not data_type.endswith("@ticker"):
                            continue
                        data = message.get("data") or {}
                        symbol = str(data.get("s") or data_type[:-7]).upper()
                        if symbol not in self.tradfi_symbol_categories:
                            continue
                        try:
                            price = float(data["c"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        await self.publish_tradfi_quote(symbol, price, int(data.get("E", message.get("ts", time.time() * 1000))))
                    reconnect_delay = 1.0 if time.monotonic() - connected_since >= 20 else min(reconnect_delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("BingX TradFi ticker stream disconnected")
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            await asyncio.sleep(reconnect_delay)

    async def hyperliquid_info(self, payload: dict[str, Any]) -> Any:
        async with self.request_slots:
            response = await self.client.post("https://api.hyperliquid.xyz/info", json=payload, timeout=12)
        response.raise_for_status()
        return response.json()

    async def collect_hyperliquid_macro(self) -> None:
        reconnect_delay = 1.0
        while True:
            try:
                # Bootstrap active HIP-3 markets and seed prices before the shared websocket starts.
                for dex in ("para", "xyz"):
                    response = await self.hyperliquid_info({"type": "metaAndAssetCtxs", "dex": dex})
                    universe = (response[0] or {}).get("universe", [])
                    contexts = response[1] if len(response) > 1 else []
                    indices = {str(row.get("name", "")): index for index, row in enumerate(universe)}
                    for symbol, descriptor in HYPERLIQUID_ASSETS.items():
                        if descriptor["dex"] != dex:
                            continue
                        index = indices.get(descriptor["coin"])
                        if index is None or universe[index].get("isDelisted"):
                            self.tradfi_symbol_categories.pop(symbol, None)
                            continue
                        context = contexts[index] if index < len(contexts) else {}
                        raw_price = context.get("midPx") or context.get("markPx") or context.get("oraclePx")
                        if raw_price is None:
                            continue
                        self.tradfi_symbol_categories[symbol] = descriptor["category"]
                        await self.publish_tradfi_quote(symbol, float(raw_price), source="Hyperliquid HIP-3")

                async with websockets.connect(
                    "wss://api.hyperliquid.xyz/ws", ping_interval=20, ping_timeout=20,
                    open_timeout=12, close_timeout=5, max_size=2**20, max_queue=2048,
                ) as upstream:
                    connected_since = time.monotonic()
                    for dex in ("para", "xyz"):
                        await upstream.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids", "dex": dex}}))
                    async for raw in upstream:
                        message = json.loads(raw)
                        if message.get("channel") != "allMids":
                            continue
                        data = message.get("data") or {}
                        mids = data.get("mids") or {}
                        dex = str(data.get("dex") or "")
                        for symbol, descriptor in HYPERLIQUID_ASSETS.items():
                            if dex and descriptor["dex"] != dex:
                                continue
                            coin = descriptor["coin"]
                            mid = mids.get(coin) or mids.get(coin.split(":", 1)[-1])
                            if mid is None:
                                continue
                            self.tradfi_symbol_categories[symbol] = descriptor["category"]
                            await self.publish_tradfi_quote(symbol, float(mid), source="Hyperliquid HIP-3")
                    reconnect_delay = 1.0 if time.monotonic() - connected_since >= 20 else min(reconnect_delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Hyperliquid macro stream disconnected")
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            await asyncio.sleep(reconnect_delay)

    async def try_open_market_session(self) -> tuple[bool, int]:
        async with self.market_sessions_lock:
            limit = positive_int_setting("BETA_MAX_CONCURRENT_SESSIONS", 10, 100)
            if self.active_market_sessions >= limit:
                return False, self.active_market_sessions
            self.active_market_sessions += 1
            return True, self.active_market_sessions

    async def close_market_session(self) -> int:
        async with self.market_sessions_lock:
            self.active_market_sessions = max(0, self.active_market_sessions - 1)
            return self.active_market_sessions

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
        ttl = self._request_cache_ttl(base, path, params or {})
        if ttl <= 0:
            return await self._request_uncached(base, path, params)
        key = f"{base}{path}?{json.dumps(params or {}, sort_keys=True, separators=(',', ':'))}"
        return await self.cache_value(key, ttl, lambda: self._request_uncached(base, path, params))

    async def cache_value(self, key: str, ttl: float, factory: Any) -> Any:
        now = time.monotonic()
        cached = self.response_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        task = self.response_inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self.response_inflight[key] = task
            task.add_done_callback(lambda completed: self._cache_completed(key, ttl, completed))
        try:
            value = await asyncio.shield(task)
        except BaseException:
            if task.done() and self.response_inflight.get(key) is task:
                self.response_inflight.pop(key, None)
            raise
        self.response_cache[key] = (time.monotonic() + ttl, value)
        if self.response_inflight.get(key) is task:
            self.response_inflight.pop(key, None)
        if len(self.response_cache) > 1024:
            expired = [cache_key for cache_key, (expires, _) in self.response_cache.items() if expires <= time.monotonic()]
            for cache_key in expired:
                self.response_cache.pop(cache_key, None)
            if len(self.response_cache) > 1024:
                oldest = sorted(self.response_cache, key=lambda cache_key: self.response_cache[cache_key][0])
                for cache_key in oldest[:len(self.response_cache) - 768]:
                    self.response_cache.pop(cache_key, None)
        return value

    def _cache_completed(self, key: str, ttl: float, task: asyncio.Task[Any]) -> None:
        if self.response_inflight.get(key) is task:
            self.response_inflight.pop(key, None)
        if task.cancelled():
            return
        try:
            value = task.result()
        except BaseException:
            return
        self.response_cache[key] = (time.monotonic() + ttl, value)

    @staticmethod
    def _request_cache_ttl(base: str, path: str, params: dict[str, Any]) -> float:
        if base != SPOT_DATA:
            return 0
        if path == "/api/v3/ticker/24hr":
            return 20
        if path == "/api/v3/ticker/price":
            return 5
        if path == "/api/v3/klines":
            if "startTime" in params:
                return 30 if "endTime" not in params else 300
            return {
                "1m": 3, "3m": 4, "5m": 5, "15m": 8, "30m": 10,
                "1h": 15, "2h": 20, "4h": 30, "6h": 30,
                "12h": 45, "1d": 60, "1w": 120, "1M": 120,
            }.get(str(params.get("interval", "")), 0)
        return 0

    async def _request_uncached(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
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
        init_lock = self.book_init_locks.setdefault(key, asyncio.Lock())
        async with init_lock:
            state = self.books.get(key)
            if state:
                if state["status"] == "live" and int(time.time() * 1000) - state["receivedAt"] < 15_000:
                    return self.book_payload(symbol, state, limit)
                task = state.get("task")
                if task and not task.done():
                    return self.book_payload(symbol, state, limit)
            base, path = self.base_and_path(market, "depth")
            # Fetch enough levels for Spot snapshot/diff synchronization; trim in the response.
            snapshot = await self.request(base, path, {"symbol": symbol, "limit": 1000})
            bids = {float(price): float(qty) for price, qty in snapshot["bids"] if float(qty) > 0}
            asks = {float(price): float(qty) for price, qty in snapshot["asks"] if float(qty) > 0}
            state = {"bids": bids, "asks": asks, "lastUpdateId": int(snapshot["lastUpdateId"]), "status": "syncing", "eventTime": None, "receivedAt": int(time.time() * 1000), "buffer": [], "lock": asyncio.Lock(), "task": None}
            state["market"] = market
            self.books[key] = state
            state["task"] = asyncio.create_task(self.book_stream(market, symbol, state))
            return self.book_payload(symbol, state, limit)

    async def subscribe_market(self, market: str, symbol: str, interval: str) -> tuple[tuple[str, str, str], asyncio.Queue[dict[str, Any]]]:
        key = (market, symbol, interval)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        subscribers = self.market_subscribers.setdefault(key, set())
        subscribers.add(queue)
        task = self.market_stream_tasks.get(key)
        if task is None or task.done():
            self.market_stream_tasks[key] = asyncio.create_task(self._broadcast_market(key))
        return key, queue

    async def unsubscribe_market(self, key: tuple[str, str, str], queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self.market_subscribers.get(key)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if subscribers:
            return
        self.market_subscribers.pop(key, None)
        task = self.market_stream_tasks.pop(key, None)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _broadcast_market(self, key: tuple[str, str, str]) -> None:
        market, symbol, interval = key
        current_task = asyncio.current_task()
        try:
            async for event in self.event_stream(market, symbol, interval):
                for queue in tuple(self.market_subscribers.get(key, ())):
                    if queue.full():
                        try:
                            queue.get_nowait()
                            queue.task_done()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        continue
        except asyncio.CancelledError:
            raise
        finally:
            if self.market_stream_tasks.get(key) is current_task:
                self.market_stream_tasks.pop(key, None)

    def book_payload(self, symbol: str, state: dict[str, Any], limit: int) -> BookSnapshot:
        now_ms = int(time.time() * 1000)
        venues = ("Kraken", "Coinbase", "BingX", "OKX")
        external = [self.external_books.get((symbol, venue)) for venue in venues]
        version = (
            state.get("lastUpdateId"), state.get("receivedAt"), state.get("status"),
            tuple((item.get("status"), item.get("receivedAt")) if item else None for item in external),
            now_ms // 1000,
        )
        cache_key = (symbol, limit)
        cached = self.book_payload_cache.get(cache_key)
        if cached and cached[0] == version:
            return cached[1]
        bids = sorted(state["bids"].items(), reverse=True)[:limit]
        asks = sorted(state["asks"].items())[:limit]
        venue_states = {"Binance": "stale" if state["status"] == "live" and now_ms - int(state["receivedAt"]) > 15_000 else state["status"]}
        for venue, other in zip(venues, external):
            status = other["status"] if other else "connecting"
            venue_states[venue] = "stale" if status == "live" and now_ms - int(other["receivedAt"]) > 15_000 else status
        combined_bids: dict[float, dict[str, Any]] = {}
        combined_asks: dict[float, dict[str, Any]] = {}
        source_books = [("Binance", state), *[(name, item) for name, item in zip(venues, external) if item and venue_states.get(name) == "live"]]
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
        midpoint = reference_price
        liquidity_bands = {}
        for label, fraction in (("0.1%", 0.001), ("0.5%", 0.005)):
            bid_notional = sum(price * item["quantity"] for price, item in combined_bids.items() if midpoint * (1 - fraction) <= price <= midpoint)
            ask_notional = sum(price * item["quantity"] for price, item in combined_asks.items() if midpoint <= price <= midpoint * (1 + fraction))
            liquidity_bands[f"bid{label}"] = bid_notional
            liquidity_bands[f"ask{label}"] = ask_notional
        live_sources = [name for name, status in venue_states.items() if status == "live"]
        event_times = [item.get("eventTime") for item in [state, *[x for x in external if x]] if item.get("eventTime")]
        payload = BookSnapshot(symbol=symbol, bids=bid_levels, asks=ask_levels, spread=ask_rows[0][0] - bid_rows[0][0] if ask_rows and bid_rows else 0, imbalance=imbalance, eventTime=max(event_times) if event_times else None, receivedAt=now_ms, status="live" if live_sources else "connecting", source=" + ".join(live_sources) or "Nenhuma fonte ao vivo", sources=venue_states, liquidityBands=liquidity_bands)
        self.book_payload_cache[cache_key] = (version, payload)
        return payload

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
        for venue in ("Kraken", "Coinbase", "BingX", "OKX"):
            key = (symbol, venue)
            task = self.external_tasks.get(key)
            if task is None or task.done():
                self.external_tasks[key] = asyncio.create_task(self.external_collector(symbol, venue))

    @staticmethod
    def external_pair(symbol: str, venue: str) -> str:
        base = symbol.removesuffix("USDT")
        if venue == "Kraken":
            return f"{base}/USD"
        if venue == "BingX":
            return f"{base}-USDT"
        if venue == "OKX":
            return f"{base}-USDT"
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
        url = {
            "Kraken": "wss://ws.kraken.com/v2",
            "Coinbase": "wss://advanced-trade-ws.coinbase.com",
            "BingX": "wss://open-api-ws.bingx.com/market",
            "OKX": "wss://ws.okx.com/ws/v5/public",
        }[venue]
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
                    elif venue == "Coinbase":
                        # Coinbase heartbeats keep otherwise quiet subscriptions alive and
                        # let us distinguish a healthy idle feed from a dead socket.
                        for channel in ("level2", "market_trades", "heartbeats"):
                            await socket.send(json.dumps({"type": "subscribe", "product_ids": [pair], "channel": channel}))
                    elif venue == "BingX":
                        for channel in (f"{pair}@depth50", f"{pair}@trade"):
                            await socket.send(json.dumps({"id": f"nexus-{int(time.time() * 1000)}", "reqType": "sub", "dataType": channel}))
                    else:
                        await socket.send(json.dumps({"op": "subscribe", "args": [
                            {"channel": "books5", "instId": pair},
                            {"channel": "trades", "instId": pair},
                        ]}))
                    async for raw in socket:
                        if venue == "BingX":
                            try:
                                text = gzip.decompress(raw).decode("utf-8") if isinstance(raw, bytes) else raw
                            except (OSError, EOFError):
                                text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                            if "ping" in text.lower():
                                await socket.send("Pong")
                                continue
                            message = json.loads(text)
                        else:
                            if raw in ("ping", b"ping"):
                                await socket.send("pong")
                                continue
                            message = json.loads(raw)
                        received = int(time.time() * 1000)
                        trade_state["lastMessageAt"] = received
                        if venue == "BingX":
                            data_type = str(message.get("dataType", ""))
                            payload = message.get("data", {})
                            if message.get("code") not in (None, 0, "0"):
                                error = str(message.get("msg", message.get("message", "subscription rejected")))
                                state.update(status="unavailable", error=error)
                                trade_state.update(status="unavailable", connected=False, error=error)
                                continue
                            if "@depth" in data_type or (isinstance(payload, dict) and ("bids" in payload or "asks" in payload)):
                                if isinstance(payload, list):
                                    payload = payload[-1] if payload else {}
                                state["bids"] = {float(x[0]): float(x[1]) for x in payload.get("bids", []) if len(x) >= 2 and float(x[1]) > 0}
                                state["asks"] = {float(x[0]): float(x[1]) for x in payload.get("asks", []) if len(x) >= 2 and float(x[1]) > 0}
                                state["bids"] = dict(sorted(state["bids"].items(), reverse=True)[:MAX_AGGREGATED_BOOK_LEVELS])
                                state["asks"] = dict(sorted(state["asks"].items())[:MAX_AGGREGATED_BOOK_LEVELS])
                                state.update(status="live", eventTime=self.exchange_time(payload.get("E", payload.get("T", received))), receivedAt=received)
                            elif "@trade" in data_type:
                                rows = payload if isinstance(payload, list) else [payload]
                                for row in rows:
                                    stamp = self.exchange_time(row.get("T", row.get("E", received)))
                                    trade_id = str(row.get("t", f"{stamp}:{row.get('p')}:{row.get('q')}"))
                                    if trade_id in seen_ids:
                                        continue
                                    if len(seen_queue) == seen_queue.maxlen:
                                        seen_ids.discard(seen_queue[0])
                                    seen_queue.append(trade_id)
                                    seen_ids.add(trade_id)
                                    trades.append(Trade(id=trade_id, time=stamp, price=float(row["p"]), quantity=float(row["q"]), side="sell" if row.get("m") else "buy", source=venue))
                                    trade_state.update(status="live", connected=True, lastTradeAt=received, error=None)
                        elif venue == "OKX":
                            if message.get("event") == "error":
                                error = str(message.get("msg", "subscription rejected"))
                                state.update(status="unavailable", error=error)
                                trade_state.update(status="unavailable", connected=False, error=error)
                                continue
                            if message.get("event") == "subscribe" and message.get("arg", {}).get("channel") == "trades":
                                trade_state.update(status="live", connected=True, error=None)
                                continue
                            channel = message.get("arg", {}).get("channel")
                            for row in message.get("data", []):
                                if channel == "books5":
                                    state["bids"] = {float(x[0]): float(x[1]) for x in row.get("bids", []) if len(x) >= 2 and float(x[1]) > 0}
                                    state["asks"] = {float(x[0]): float(x[1]) for x in row.get("asks", []) if len(x) >= 2 and float(x[1]) > 0}
                                    state.update(status="live", eventTime=self.exchange_time(row.get("ts", received)), receivedAt=received)
                                elif channel == "trades":
                                    stamp = self.exchange_time(row.get("ts", received))
                                    trade_id = str(row.get("tradeId", f"{stamp}:{row.get('px')}:{row.get('sz')}:{row.get('side')}"))
                                    if trade_id in seen_ids:
                                        continue
                                    if len(seen_queue) == seen_queue.maxlen:
                                        seen_ids.discard(seen_queue[0])
                                    seen_queue.append(trade_id)
                                    seen_ids.add(trade_id)
                                    trades.append(Trade(id=trade_id, time=stamp, price=float(row["px"]), quantity=float(row["sz"]), side=row["side"], source=venue))
                                    trade_state.update(status="live", connected=True, lastTradeAt=received, error=None)
                        elif venue == "Kraken":
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
    market_tasks = list(service.market_stream_tasks.values())
    for task in market_tasks:
        task.cancel()
    await asyncio.gather(*market_tasks, return_exceptions=True)
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

def decode_market_token(token: str, expected_use: str) -> dict[str, Any] | None:
    secret = os.getenv("NEXUS_MARKET_TOKEN_SECRET", "")
    if len(secret) < 32:
        return None
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], issuer="nexus-terminal-web", audience="nexus-market-api")
        return claims if claims.get("token_use") == expected_use and isinstance(claims.get("email"), str) else None
    except jwt.PyJWTError:
        return None

def verify_market_token(token: str, expected_use: str) -> bool:
    return decode_market_token(token, expected_use) is not None

def allow_api_request(email: str, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    window = api_request_windows.setdefault(email, deque())
    while window and current - window[0] >= 60:
        window.popleft()
    if len(window) >= positive_int_setting("BETA_API_REQUESTS_PER_MINUTE", 60, 600):
        return False
    window.append(current)
    return True

@app.middleware("http")
async def require_market_auth(request: Request, call_next):
    if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    if len(os.getenv("NEXUS_MARKET_TOKEN_SECRET", "")) < 32:
        return JSONResponse(status_code=503, content={"detail": "Market API authentication is not configured"})
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    claims = decode_market_token(token, "api") if scheme.lower() == "bearer" and token else None
    if claims is None:
        return JSONResponse(status_code=401, content={"detail": "Valid market API token required"})
    if not allow_api_request(str(claims["email"])):
        return JSONResponse(status_code=429, content={"detail": "Beta API limit reached; retry shortly"}, headers={"Retry-After": "15"})
    return await call_next(request)

app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","), allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])

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

async def bingx_tradfi_request(path: str, params: dict[str, Any], ttl: float) -> Any:
    key = f"bingx-tradfi:{path}:{json.dumps(params, sort_keys=True, separators=(',', ':'))}"
    async def load() -> Any:
        async with service.request_slots:
            response = await service.client.get(f"https://open-api.bingx.com{path}", params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("code", "0")) != "0":
            raise HTTPException(502, f"BingX market data error: {payload.get('msg', payload.get('code'))}")
        return payload.get("data")
    return await service.cache_value(key, ttl, load)

async def bingx_tradfi_catalog() -> list[dict[str, Any]]:
    rows = await bingx_tradfi_request("/openApi/swap/v2/quote/contracts", {}, 900)
    instruments: list[dict[str, Any]] = []
    for row in rows or []:
        symbol = str(row.get("symbol", "")).upper()
        asset = str(row.get("asset", "")).upper()
        if row.get("status") != 1 or str(row.get("apiStateOpen", "")).lower() != "true" or row.get("currency") != "USDT":
            continue
        if asset.startswith("NCFX"):
            category, label = "forex", str(row.get("displayName", symbol)).removesuffix("-USDT")
            if label.endswith("-USDT"):
                label = label[:-5]
            if len(label) == 6 and label.isalpha():
                label = f"{label[:3]}/{label[3:]}"
        elif symbol in TRADFI_INDEX_SYMBOLS:
            category, label = "indices", TRADFI_INDEX_SYMBOLS[symbol]
        elif symbol in TRADFI_COMMODITY_SYMBOLS:
            category, label = "commodities", TRADFI_COMMODITY_SYMBOLS[symbol]
        else:
            continue
        instruments.append({"symbol": symbol, "name": label, "category": category, "asset": asset, "minimumNotional": row.get("tradeMinUSDT"), "pricePrecision": row.get("pricePrecision"), "quantityPrecision": row.get("quantityPrecision")})
    return sorted(instruments, key=lambda item: (item["category"], item["name"]))

async def validate_bingx_tradfi_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    catalog = await bingx_tradfi_catalog()
    if not any(item["symbol"] == symbol for item in catalog):
        raise HTTPException(404, "Índice ou par Forex indisponível na BingX.")
    return symbol

@app.get("/api/tradfi/contracts")
async def tradfi_contracts():
    try:
        instruments = await bingx_tradfi_catalog()
        return {"source": "BingX USDT-M Perpetual · contratos TradFi", "receivedAt": int(time.time() * 1000), "instruments": instruments}
    except (httpx.HTTPError, ValueError, KeyError) as error:
        log.warning("BingX TradFi contract list unavailable: %s", error)
        raise HTTPException(502, "Não foi possível carregar os contratos TradFi da BingX.") from error

async def refresh_tradfi_daily_closes() -> None:
    """Warm requested contracts' last completed daily close once per UTC day."""
    while TRADFI_DAILY_CLOSES_PENDING:
        _, instrument = TRADFI_DAILY_CLOSES_PENDING.popitem()
        symbol = instrument["symbol"]
        now = datetime.now(timezone.utc)
        reference_date = now.date().isoformat()
        day_start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        cached = TRADFI_DAILY_CLOSES.get(symbol)
        if cached and cached.get("referenceDate") == reference_date:
            continue
        try:
            rows = await bingx_tradfi_request(
                "/openApi/swap/v2/quote/klines",
                {"symbol": symbol, "interval": "1d", "limit": 3, "endTime": int(time.time() * 1000)},
                3600,
            )
            completed = [row for row in rows or [] if int(row["time"]) < day_start_ms]
            if completed:
                previous = max(completed, key=lambda row: int(row["time"]))
                TRADFI_DAILY_CLOSES[symbol] = {
                    "close": float(previous["close"]),
                    "candleTime": int(previous["time"] // 1000),
                    "referenceDate": reference_date,
                    "source": "BingX último candle diário fechado · UTC",
                }
                # Refresh the live row as soon as its daily reference becomes available.
                latest = service.tradfi_prices.get(symbol)
                if latest:
                    await service.publish_tradfi_quote(symbol, latest["price"], latest["eventTime"])
        except (httpx.HTTPError, HTTPException, ValueError, KeyError, TypeError) as error:
            log.info("BingX TradFi daily reference unavailable", extra={"symbol": symbol, "error": str(error)})
        # The public Kline endpoint is rate limited. Spread the warm-up across
        # contracts rather than bursting one historical request per watchlist row.
        await asyncio.sleep(1.05)

@app.get("/api/tradfi/watchlist")
async def tradfi_watchlist(category: str = Query("indices")):
    if category not in {"indices", "forex", "commodities"}:
        raise HTTPException(400, "Categoria TradFi inválida.")
    catalog = await bingx_tradfi_catalog()
    selected = [item for item in catalog if item["category"] == category]
    service.ensure_tradfi_ticker_stream()
    service.tradfi_symbol_categories.update({item["symbol"]: item["category"] for item in selected})
    reference_date = datetime.now(timezone.utc).date().isoformat()
    global TRADFI_DAILY_CLOSES_TASK
    stale = [item for item in selected if TRADFI_DAILY_CLOSES.get(item["symbol"], {}).get("referenceDate") != reference_date]
    TRADFI_DAILY_CLOSES_PENDING.update({item["symbol"]: item for item in stale})
    if stale:
        if TRADFI_DAILY_CLOSES_TASK is None or TRADFI_DAILY_CLOSES_TASK.done():
            TRADFI_DAILY_CLOSES_TASK = asyncio.create_task(refresh_tradfi_daily_closes())
    try:
        rows = await bingx_tradfi_request("/openApi/swap/v2/quote/ticker", {}, 5)
        tickers = rows if isinstance(rows, list) else [rows] if rows else []
        by_symbol = {str(row.get("symbol", "")).upper(): row for row in tickers}
        quotes = []
        for item in selected:
            symbol = item["symbol"]
            ticker = by_symbol.get(symbol, {})
            try:
                price = float(ticker["lastPrice"])
            except (KeyError, TypeError, ValueError):
                price = None
            reference = TRADFI_DAILY_CLOSES.get(symbol)
            previous_close = reference.get("close") if reference and reference.get("referenceDate") == reference_date else None
            change_percent = ((price - previous_close) / previous_close * 100) if price is not None and previous_close else None
            quotes.append({
                "symbol": symbol, "price": price, "previousClose": previous_close,
                "changePercent": round(change_percent, 4) if change_percent is not None else None,
                "referenceTime": reference.get("candleTime") if previous_close is not None else None,
                "source": "BingX ticker + fechamento diário anterior UTC",
            })
        return {"category": category, "quotes": quotes, "receivedAt": int(time.time() * 1000),
                "reference": "Último candle diário fechado (UTC); cotação em lote BingX.",
                "referenceStatus": "ready" if all(row["previousClose"] is not None for row in quotes) else "loading"}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        log.warning("BingX TradFi watchlist quotes unavailable", extra={"category": category, "error": str(error)})
        raise HTTPException(502, "Não foi possível atualizar as cotações da watchlist BingX.") from error

@app.get("/api/tradfi/macro")
async def tradfi_macro():
    # The macro asset universe is static and already declared in the engine. Avoid waiting
    # on the external BingX catalogue (or historical seeding) before responding to the UI.
    catalog = [{"symbol": symbol, "category": metadata["category"]} for symbol, metadata in MACRO_ASSETS.items()]
    service.ensure_tradfi_ticker_stream()
    now_ms = int(time.time() * 1000)
    selected = catalog
    symbols = [item["symbol"] for item in selected]
    history: dict[str, list[dict[str, Any]]] = {}
    if service.flow_store:
        try:
            history = await asyncio.to_thread(service.flow_store.tradfi_price_history, symbols, now_ms - 3 * 60 * 60_000)
        except Exception:
            log.exception("TradFi macro history query failed")
    for symbol, points in service.tradfi_samples.items():
        if symbol in MACRO_ASSETS:
            by_time = {item["time"]: item for item in history.get(symbol, [])}
            by_time.update({item["time"]: item for item in points if item["time"] >= now_ms - 3 * 60 * 60_000})
            history[symbol] = sorted(by_time.values(), key=lambda item: item["time"])
    return build_macro_snapshot(
        selected, history, service.tradfi_prices, now_ms,
        service.flow_store.storage if service.flow_store else "unavailable",
        service.flow_store.durable if service.flow_store else False,
    )

@app.get("/api/tradfi/candles")
async def tradfi_candles(symbol: str = Query(...), interval: str = Query("5m"), limit: int = Query(240, ge=20, le=500)):
    symbol = await validate_bingx_tradfi_symbol(symbol)
    if interval not in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}:
        raise HTTPException(400, "Intervalo TradFi inválido.")
    try:
        rows = await bingx_tradfi_request("/openApi/swap/v2/quote/klines", {"symbol": symbol, "interval": interval, "limit": limit}, 15)
        candles = [{"time": int(row["time"] // 1000), "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]), "volume": float(row.get("volume", 0))} for row in rows or []]
        return {"symbol": symbol, "interval": interval, "candles": candles, "source": "BingX USDT-M Perpetual", "receivedAt": int(time.time() * 1000)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        log.warning("BingX TradFi candles unavailable", extra={"symbol": symbol, "interval": interval, "error": str(error)})
        raise HTTPException(502, "Candles BingX indisponíveis para este ativo ou sessão.") from error

@app.get("/api/tradfi/indicators")
async def tradfi_indicators(symbol: str = Query(...), interval: str = Query("5m")):
    """Volatility, anchored levels and deterministic signals from BingX TradFi OHLCV."""
    symbol = await validate_bingx_tradfi_symbol(symbol)
    if interval not in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}:
        raise HTTPException(400, "Intervalo TradFi inválido.")
    try:
        rows, daily_rows, hourly_rows = await asyncio.gather(
            bingx_tradfi_request("/openApi/swap/v2/quote/klines", {"symbol": symbol, "interval": interval, "limit": 300}, 20),
            bingx_tradfi_request("/openApi/swap/v2/quote/klines", {"symbol": symbol, "interval": "1d", "limit": 400}, 300),
            bingx_tradfi_request("/openApi/swap/v2/quote/klines", {"symbol": symbol, "interval": "1h", "limit": 500}, 60),
        )

        def normalize(row: dict[str, Any]) -> dict[str, Any]:
            return {"time": int(row["time"] // 1000), "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]), "volume": float(row.get("volume", 0))}

        candles = [normalize(row) for row in rows or []]
        daily = [normalize(row) for row in daily_rows or []]
        hourly = [normalize(row) for row in hourly_rows or []]
        zones = calculate_zones(symbol, daily)
        zones["source"] = "BingX USDT-M TradFi daily klines"

        # Keep levels aligned with the information that was available at each
        # closed intraday candle. The newest daily bar is the still-forming bar.
        closed_candles = candles[:-1]
        zones_by_candle: list[dict[str, Any] | None] = []
        zones_cache: dict[int, dict[str, Any]] = {}
        for candle in closed_candles:
            day_index = -1
            for index, daily_candle in enumerate(daily):
                if daily_candle["time"] + 86_400 <= candle["time"]:
                    day_index = index
                else:
                    break
            if day_index < 0 or day_index + 1 >= len(daily):
                zones_by_candle.append(None)
                continue
            if day_index not in zones_cache:
                history = daily[:day_index + 2]
                zones_cache[day_index] = calculate_zones(symbol, history)
                zones_cache[day_index]["source"] = "BingX USDT-M TradFi daily klines"
            zones_by_candle.append(zones_cache[day_index])

        reversal = build_reversal_signals(
            symbol, interval, closed_candles, zones_by_candle,
            min_sigma=1.0, min_score=50, cooldown=10, volume_enabled=True,
        )
        breakout = build_breakout_signals(symbol, interval, closed_candles)
        breakout["source"] = "BingX USDT-M TradFi OHLCV; candles fechados; compressão sem dados futuros"

        # Candle VWAP is an OHLCV estimate, not trade-by-trade VWAP. Seed the
        # visible curves with prior daily candles from the active UTC period.
        vwap_totals = {name: [0.0, 0.0] for name in ("daily", "weekly", "monthly")}
        series: dict[str, list[dict[str, float | int]]] = {name: [] for name in vwap_totals}
        if candles:
            first_ms = candles[0]["time"] * 1000
            first_keys = _period_starts(first_ms)
            starts = dict(zip(vwap_totals, first_keys))
            first_day_start = starts["daily"]
            for daily_candle in daily[:-1]:
                stamp_ms = daily_candle["time"] * 1000
                if stamp_ms >= first_day_start:
                    continue
                day_key, week_key, month_key = _period_starts(stamp_ms)
                keys = {"daily": day_key, "weekly": week_key, "monthly": month_key}
                typical = (daily_candle["high"] + daily_candle["low"] + daily_candle["close"]) / 3
                volume = daily_candle["volume"]
                for name in ("weekly", "monthly"):
                    if keys[name] == starts[name] and volume > 0:
                        vwap_totals[name][0] += typical * volume
                        vwap_totals[name][1] += volume
            for hourly_candle in hourly[:-1]:
                stamp_ms = hourly_candle["time"] * 1000
                if stamp_ms < first_day_start or stamp_ms + 3_600_000 > first_ms:
                    continue
                hour_day, hour_week, hour_month = _period_starts(stamp_ms)
                keys = {"daily": hour_day, "weekly": hour_week, "monthly": hour_month}
                typical = (hourly_candle["high"] + hourly_candle["low"] + hourly_candle["close"]) / 3
                volume = hourly_candle["volume"]
                for name in vwap_totals:
                    if keys[name] == starts[name] and volume > 0:
                        vwap_totals[name][0] += typical * volume
                        vwap_totals[name][1] += volume
            last_keys = starts.copy()
            for candle in candles:
                keys = dict(zip(vwap_totals, _period_starts(candle["time"] * 1000)))
                for name in vwap_totals:
                    if keys[name] != last_keys[name]:
                        vwap_totals[name] = [0.0, 0.0]
                        last_keys[name] = keys[name]
                    typical = (candle["high"] + candle["low"] + candle["close"]) / 3
                    volume = candle["volume"]
                    if volume > 0:
                        vwap_totals[name][0] += typical * volume
                        vwap_totals[name][1] += volume
                    if vwap_totals[name][1] > 0:
                        series[name].append({"time": candle["time"], "value": vwap_totals[name][0] / vwap_totals[name][1]})

        now_ms = int(time.time() * 1000)
        today_start, week_start, month_start = _period_starts(now_ms)
        previous_month_dt = datetime.fromtimestamp(month_start / 1000, timezone.utc)
        previous_month_dt = (previous_month_dt.replace(day=1) - timedelta(days=1)).replace(day=1)
        previous_month_start = int(previous_month_dt.timestamp() * 1000)
        completed_daily = daily[:-1]
        def extreme(start_ms: int, end_ms: int, field: str, mode: str) -> float | None:
            values = [float(item[field]) for item in completed_daily if start_ms <= item["time"] * 1000 < end_ms]
            return (max(values) if mode == "high" else min(values)) if values else None
        previous = {
            "day": {"high": extreme(today_start - 86_400_000, today_start, "high", "high"), "low": extreme(today_start - 86_400_000, today_start, "low", "low")},
            "week": {"high": extreme(week_start - 7 * 86_400_000, week_start, "high", "high"), "low": extreme(week_start - 7 * 86_400_000, week_start, "low", "low")},
            "month": {"high": extreme(previous_month_start, month_start, "high", "high"), "low": extreme(previous_month_start, month_start, "low", "low")},
        }
        overlays = {"symbol": symbol, "interval": interval, "timezone": "UTC", "vwap": series, "previous": previous,
                    "source": "BingX OHLCV · VWAP aproximada por preço típico e volume dos candles", "receivedAt": now_ms}
        return {"symbol": symbol, "interval": interval, "zones": zones, "overlays": overlays,
                "reversal": reversal, "breakout": breakout, "receivedAt": now_ms,
                "limitations": ["VWAPs estimadas com OHLCV (preço típico × volume), não com cada negócio.", "Níveis anteriores usam candles diários UTC da BingX; ativos TradFi podem ter pausas e sessões próprias.", "Sinais são calculados somente em candles fechados e não enviam ordens."]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as error:
        log.warning("BingX TradFi indicators unavailable", extra={"symbol": symbol, "interval": interval, "error": str(error)})
        raise HTTPException(502, "Não foi possível calcular os indicadores TradFi com o histórico BingX disponível.") from error

@app.get("/api/tradfi/market")
async def tradfi_market(symbol: str = Query(...)):
    symbol = await validate_bingx_tradfi_symbol(symbol)
    try:
        ticker, depth, trades = await asyncio.gather(
            bingx_tradfi_request("/openApi/swap/v2/quote/ticker", {"symbol": symbol}, 3),
            bingx_tradfi_request("/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 20}, 2),
            bingx_tradfi_request("/openApi/swap/v2/quote/trades", {"symbol": symbol, "limit": 30}, 2),
        )
        bids_raw, asks_raw = (depth or {}).get("bids", []), (depth or {}).get("asks", [])
        bids = [{"price": float(row[0]), "quantity": float(row[1]), "total": float(row[0]) * float(row[1]), "sources": ["BingX Perpétuo"]} for row in bids_raw]
        asks = [{"price": float(row[0]), "quantity": float(row[1]), "total": float(row[0]) * float(row[1]), "sources": ["BingX Perpétuo"]} for row in asks_raw]
        bid_qty, ask_qty = sum(row["quantity"] for row in bids), sum(row["quantity"] for row in asks)
        normalized_trades = [{"id": str(row.get("fillId", row.get("ts", row.get("time", index)))), "time": int(row.get("time", row.get("ts", 0))), "price": float(row["price"]), "quantity": float(row.get("qty", 0)), "side": "sell" if row.get("isBuyerMaker") else "buy", "source": "BingX Perpétuo"} for index, row in enumerate(trades or [])]
        return {"symbol": symbol, "source": "BingX USDT-M Perpetual", "receivedAt": int(time.time() * 1000), "ticker": {"price": float(ticker["lastPrice"]), "change24h": float(ticker.get("priceChangePercent", 0)), "high24h": float(ticker.get("highPrice", 0)), "low24h": float(ticker.get("lowPrice", 0)), "quoteVolume24h": float(ticker.get("quoteVolume", 0)), "eventTime": int(ticker.get("closeTime", 0))}, "book": {"bids": bids, "asks": asks, "spread": float(asks_raw[0][0]) - float(bids_raw[0][0]) if asks_raw and bids_raw else 0, "imbalance": (bid_qty - ask_qty) / (bid_qty + ask_qty) if bid_qty + ask_qty else 0, "status": "live" if bids and asks else "stale", "source": "BingX Perpétuo"}, "trades": normalized_trades}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        log.warning("BingX TradFi market data unavailable", extra={"symbol": symbol, "error": str(error)})
        raise HTTPException(502, "Dados ao vivo BingX indisponíveis. O contrato pode estar fora da sessão.") from error

@app.get("/api/scanner-flow")
async def scanner_flow():
    """Observed Binance notional flow for the 25 supported Spot assets."""
    async def build() -> dict[str, Any]:
        store = service.flow_store
        now_ms = int(time.time() * 1000)
        bucket_ms = 15 * 60_000
        current_bucket = (now_ms // bucket_ms) * bucket_ms
        if store is None:
            raise HTTPException(503, "Flow storage is not ready")
        # One shared query supplies both the existing 15m pace and longer
        # observed-flow context to every scanner client.
        grouped = await asyncio.to_thread(store.scanner_flow, current_bucket - 16 * bucket_ms, now_ms)
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
            def window_flow(count: int) -> tuple[float | None, float | None, int]:
                window = [buckets.get(current_bucket - index * bucket_ms) for index in range(count)]
                observed = [item for item in window if item is not None]
                if not observed:
                    return None, None, 0
                notional = sum(item["buyNotional"] + item["sellNotional"] for item in observed)
                cvd = sum(item["buyNotional"] - item["sellNotional"] for item in observed)
                return notional, cvd, len(observed)
            notional_1h, cvd_1h, buckets_1h = window_flow(4)
            notional_4h, cvd_4h, buckets_4h = window_flow(16)
            assets[symbol] = {
                "symbol": symbol,
                "notional15m": current_notional,
                "cvdNotional15m": cvd_notional,
                "relativeFlow15m": relative_flow,
                "observedBuckets": len(buckets),
                "lastObservedBucket": latest_bucket,
                "notional1h": notional_1h,
                "cvdNotional1h": cvd_1h,
                "observedBuckets1h": buckets_1h,
                "notional4h": notional_4h,
                "cvdNotional4h": cvd_4h,
                "observedBuckets4h": buckets_4h,
            }
        return {
            "assets": assets,
            "bucketStartedAt": current_bucket,
            "source": "Binance Spot aggTrades persistidos",
            "receivedAt": now_ms,
            "relativeFlowMethod": "Ritmo de 15m compara o bloco atual, ajustado pelo tempo decorrido, com os 3 anteriores. CVD de 1h e 4h soma compra menos venda observadas em aggTrades da Binance, somente em blocos com trades.",
        }
    return await service.cache_value("scanner-flow", 15, build)

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
    venues = ("Kraken", "Coinbase", "BingX", "OKX")
    for venue in venues:
        health = service.external_trade_status.get((symbol, venue))
        status = health["status"] if health else "connecting"
        if health and health.get("connected") and health.get("lastMessageAt") and int(time.time() * 1000) - health["lastMessageAt"] > 30_000:
            status = "stale"
        sources[venue] = status
        trade_health[venue] = {"status": status, "lastTradeAt": health.get("lastTradeAt") if health else None, "reconnects": health.get("reconnects", 0) if health else 0, "error": health.get("error") if health else None}
    return {"trades": [trade.model_dump() for trade in rows], "source": "Binance + Kraken + Coinbase + BingX + OKX Spot", "sources": sources, "tradeHealth": trade_health, "receivedAt": int(time.time() * 1000), "coverage": "Negociações públicas observadas por exchange; não representa o mercado global. USD ≈ USDT."}

@app.get("/api/flow-quality")
async def flow_quality(symbol: str = Query("BTCUSDT")):
    symbol = validate_symbol(symbol)
    state = service.flow_quality[symbol].copy()
    async def load_summary() -> dict[str, Any]:
        if service.flow_store:
            return await asyncio.to_thread(service.flow_store.summary, symbol)
        return {"storedTrades": 0, "oldestTradeTime": None, "newestTradeTime": None}
    summary = await service.cache_value(f"flow-summary:{symbol}", 15, load_summary)
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

@app.get("/api/breakout-signals")
async def breakout_signals(
    symbol: str = Query("BTCUSDT"), interval: str = Query("5m"), market: str = Query("spot"),
    compression_percentile: float = Query(20, ge=1, le=50), volume_multiple: float = Query(1.5, ge=1, le=10),
    breakout_atr_multiple: float = Query(0.15, ge=0, le=2),
    range_lookback: int = Query(20, ge=5, le=100), volume_lookback: int = Query(20, ge=5, le=100),
    cooldown: int = Query(10, ge=0, le=500), buy_enabled: bool = Query(True), sell_enabled: bool = Query(True),
):
    """Independent closed-candle squeeze breakout signals for Spot OHLCV."""
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in INTERVALS:
        raise HTTPException(400, "Unsupported candle interval")
    base, path = service.base_and_path(market, "klines")
    try:
        rows = await service.request(base, path, {"symbol": symbol, "interval": interval, "limit": 500})
        candles = [
            {"time": int(row[0] // 1000), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
            for row in rows[:-1]
        ]
        return build_breakout_signals(
            symbol, interval, candles, compression_percentile=compression_percentile,
            range_lookback=range_lookback, volume_lookback=volume_lookback,
            volume_multiple=volume_multiple, breakout_atr_multiple=breakout_atr_multiple, cooldown=cooldown,
            buy_enabled=buy_enabled, sell_enabled=sell_enabled,
        )
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as error:
        raise HTTPException(502, f"Unable to calculate breakout signals: {error}") from error

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
    admitted, active_sessions = await service.try_open_market_session()
    if not admitted:
        await socket.close(code=1013, reason="Beta live session limit reached")
        return
    log.info("beta market session opened", extra={"active_sessions": active_sessions, "symbol": symbol})
    consumer = producer = None
    subscription_key: tuple[str, str, str] | None = None
    market_queue: asyncio.Queue[dict[str, Any]] | None = None
    try:
        await socket.accept(subprotocol="nexus-market-v1")
        subscription_key, market_queue = await service.subscribe_market(market, symbol, interval)
        consumer = asyncio.create_task(_consume_market(socket, market_queue))
        producer = asyncio.create_task(_produce_book(socket, symbol, market))
        while True:
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await socket.send_json({"type": "heartbeat", "data": {"time": int(time.time() * 1000)}})
    except WebSocketDisconnect:
        pass
    finally:
        tasks = [task for task in (consumer, producer) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if subscription_key is not None and market_queue is not None:
            await service.unsubscribe_market(subscription_key, market_queue)
        active_sessions = await service.close_market_session()
        log.info("beta market session closed", extra={"active_sessions": active_sessions, "symbol": symbol})

@app.websocket("/ws/tradfi/watchlist")
async def tradfi_watchlist_ws(socket: WebSocket):
    allowed_origins = {origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()}
    origin = socket.headers.get("origin")
    protocols = [value.strip() for value in socket.headers.get("sec-websocket-protocol", "").split(",") if value.strip()]
    market_token = protocols[1] if len(protocols) == 2 and protocols[0] == "nexus-market-v1" else ""
    if (origin and origin not in allowed_origins) or not verify_market_token(market_token, "ws"):
        await socket.close(code=4401, reason="Authentication required")
        return
    category = socket.query_params.get("category", "indices")
    if category not in service.tradfi_watchlist_subscribers:
        await socket.close(code=1008, reason="Unsupported TradFi category")
        return
    admitted, active_sessions = await service.try_open_market_session()
    if not admitted:
        await socket.close(code=1013, reason="Beta live session limit reached")
        return
    try:
        await socket.accept(subprotocol="nexus-market-v1")
        service.tradfi_watchlist_subscribers[category].add(socket)
        service.ensure_tradfi_ticker_stream()
        while True:
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await socket.send_json({"type": "heartbeat", "data": {"time": int(time.time() * 1000)}})
    except WebSocketDisconnect:
        pass
    finally:
        service.tradfi_watchlist_subscribers[category].discard(socket)
        active_sessions = await service.close_market_session()
        log.info("beta TradFi watchlist session closed", extra={"active_sessions": active_sessions, "category": category})

@app.websocket("/ws/tradfi/macro")
async def tradfi_macro_ws(socket: WebSocket):
    allowed_origins = {origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()}
    origin = socket.headers.get("origin")
    protocols = [value.strip() for value in socket.headers.get("sec-websocket-protocol", "").split(",") if value.strip()]
    market_token = protocols[1] if len(protocols) == 2 and protocols[0] == "nexus-market-v1" else ""
    if (origin and origin not in allowed_origins) or not verify_market_token(market_token, "ws"):
        await socket.close(code=4401, reason="Authentication required")
        return
    admitted, active_sessions = await service.try_open_market_session()
    if not admitted:
        await socket.close(code=1013, reason="Beta live session limit reached")
        return
    try:
        await socket.accept(subprotocol="nexus-market-v1")
        service.tradfi_macro_subscribers.add(socket)
        service.ensure_tradfi_ticker_stream()
        for symbol, quote in tuple(service.tradfi_prices.items()):
            if symbol in MACRO_ASSETS:
                await socket.send_json({"type": "quote", "data": {"symbol": symbol, **quote}})
        while True:
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await socket.send_json({"type": "heartbeat", "data": {"time": int(time.time() * 1000)}})
    except WebSocketDisconnect:
        pass
    finally:
        service.tradfi_macro_subscribers.discard(socket)
        active_sessions = await service.close_market_session()
        log.info("beta TradFi macro session closed", extra={"active_sessions": active_sessions})

async def _consume_market(socket: WebSocket, queue: asyncio.Queue[dict[str, Any]]):
    while True:
        event = await queue.get()
        try:
            await socket.send_json(event)
        finally:
            queue.task_done()

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
