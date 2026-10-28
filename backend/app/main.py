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
import websockets
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .schemas import BookSnapshot, Candle, Level, MarketSnapshot, Trade
from .reversal_engine import build_reversal_signals
from .volatility import calculate_zones

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("nexus.market")
SPOT = "https://api.binance.com"
FUTURES = "https://fapi.binance.com"
SPOT_WS = "wss://stream.binance.com:9443/stream"
FUTURES_WS = "wss://fstream.binance.com/stream"
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
        self.books: dict[tuple[str, str], dict[str, Any]] = {}
        self.samples: dict[tuple[str, str], deque[Trade]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(4):
            try:
                # Keep concurrent exchange requests within a small bound. The free
                # Render instance has limited CPU/connections and should not fan
                # out dozens of simultaneous Binance calls per dashboard refresh.
                async with self.request_slots:
                    response = await self.client.get(base + path, params=params)
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
        base = FUTURES if market == "futures" else SPOT
        path = "/fapi/v1/ticker/24hr" if market == "futures" else "/api/v3/ticker/24hr"
        received = int(time.time() * 1000)
        row = await self.request(base, path, {"symbol": symbol})
        event = int(row["closeTime"])
        return MarketSnapshot(symbol=symbol, price=float(row["lastPrice"]), change24h=float(row["priceChangePercent"]), volume24h=float(row["volume"]), high24h=float(row["highPrice"]), low24h=float(row["lowPrice"]), source=f"Binance {market}", eventTime=event, receivedAt=received, latencyMs=max(0, received - event), status="live" if received - event < 60_000 else "stale")

    async def book(self, symbol: str, market: str, limit: int) -> BookSnapshot:
        key = (market, symbol)
        state = self.books.get(key)
        if state:
            if state["status"] == "live" and int(time.time() * 1000) - state["receivedAt"] < 15_000:
                return self.book_payload(symbol, state, limit)
            task = state.get("task")
            if task and not task.done():
                # Never replace an in-progress snapshot/stream; callers share one book collector per symbol.
                return self.book_payload(symbol, state, limit)
        base = FUTURES if market == "futures" else SPOT
        path = "/fapi/v1/depth" if market == "futures" else "/api/v3/depth"
        # USDⓈ-M accepts only specific depth limits (5, 10, 20, 50, 100, ...).
        # Fetch enough levels for snapshot/diff synchronization; trim in the response.
        snapshot_limit = 1000 if market == "spot" else 100
        snapshot = await self.request(base, path, {"symbol": symbol, "limit": snapshot_limit})
        bids = {float(price): float(qty) for price, qty in snapshot["bids"] if float(qty) > 0}
        asks = {float(price): float(qty) for price, qty in snapshot["asks"] if float(qty) > 0}
        state = {"bids": bids, "asks": asks, "lastUpdateId": int(snapshot["lastUpdateId"]), "status": "syncing", "eventTime": None, "receivedAt": int(time.time() * 1000), "buffer": [], "lock": asyncio.Lock(), "task": None}
        self.books[key] = state
        state["task"] = asyncio.create_task(self.book_stream(market, symbol, state))
        return self.book_payload(symbol, state, limit)

    def book_payload(self, symbol: str, state: dict[str, Any], limit: int) -> BookSnapshot:
        bids = sorted(state["bids"].items(), reverse=True)[:limit]
        asks = sorted(state["asks"].items())[:limit]
        bid_levels, ask_levels = [], []
        total = 0.0
        for price, qty in bids:
            total += price * qty
            bid_levels.append(Level(price=price, quantity=qty, total=total))
        total = 0.0
        for price, qty in asks:
            total += price * qty
            ask_levels.append(Level(price=price, quantity=qty, total=total))
        bid_volume = sum(qty for _, qty in bids)
        ask_volume = sum(qty for _, qty in asks)
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) if bid_volume + ask_volume else 0.0
        return BookSnapshot(symbol=symbol, bids=bid_levels, asks=ask_levels, spread=asks[0][0] - bids[0][0] if asks and bids else 0, imbalance=imbalance, eventTime=state["eventTime"], receivedAt=state["receivedAt"], status=state["status"])

    async def book_stream(self, market: str, symbol: str, state: dict[str, Any]) -> None:
        endpoint = FUTURES_WS if market == "futures" else SPOT_WS
        stream = f"{symbol.lower()}@depth@100ms" if market == "futures" else f"{symbol.lower()}@depth@100ms"
        url = f"{endpoint}?streams={stream}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as socket:
                    # Subscribe first, then fetch a snapshot while Binance buffers depth diffs.
                    # This avoids losing updates between the REST snapshot and WS handshake.
                    snapshot_limit = 100 if market == "futures" else 1000
                    snapshot = await self.request(FUTURES if market == "futures" else SPOT, "/fapi/v1/depth" if market == "futures" else "/api/v3/depth", {"symbol": symbol, "limit": snapshot_limit})
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
                            if final < last:
                                continue
                            if state["status"] != "live":
                                # Skip old buffered events; wait for the event spanning the snapshot ID.
                                bridge_id = last + 1 if market == "futures" else last
                                if not (first <= bridge_id <= final):
                                    continue
                            elif market == "futures" and int(data.get("pu", last)) != last:
                                # Futures depth diffs are chained by pu; a broken chain needs a fresh snapshot.
                                state["status"] = "stale"
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
        endpoint = FUTURES_WS if market == "futures" else SPOT_WS
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
                            yield {"type": "trade", "data": trade.model_dump()}
                        elif "kline" in stream or "kline" in data:
                            row = data["k"]
                            candle = Candle(time=int(row["t"] // 1000), open=float(row["o"]), high=float(row["h"]), low=float(row["l"]), close=float(row["c"]), volume=float(row["v"]))
                            yield {"type": "candle", "data": candle.model_dump(), "closed": bool(row.get("x", False))}
                        elif "ticker" in stream:
                            event = int(data.get("E", received))
                            snapshot = MarketSnapshot(symbol=symbol, price=float(data["c"]), change24h=float(data["P"]), volume24h=float(data["v"]), high24h=float(data["h"]), low24h=float(data["l"]), source=f"Binance {market}", eventTime=event, receivedAt=received, latencyMs=max(0, received-event), status="live")
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
    yield
    for state in service.books.values():
        task = state.get("task")
        if task:
            task.cancel()
    await service.close()


app = FastAPI(title="NEXUS Market API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","), allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])


def validate_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"MVP supports only: {', '.join(SYMBOLS)}")
    return symbol


def validate_market(market: str) -> str:
    if market not in {"spot", "futures"}:
        raise HTTPException(400, "market must be spot or futures")
    return market


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


@app.get("/api/candles")
async def candles(symbol: str = Query("BTCUSDT"), interval: str = Query("5m"), market: str = Query("spot"), limit: int = Query(240, ge=10, le=500)):
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in INTERVALS:
        raise HTTPException(400, "Unsupported candle interval")
    base, path = (FUTURES, "/fapi/v1/klines") if market == "futures" else (SPOT, "/api/v3/klines")
    try:
        rows = await service.request(base, path, {"symbol": symbol, "interval": interval, "limit": limit})
    except (httpx.HTTPError, HTTPException) as error:
        log.exception("candle request failed", extra={"symbol": symbol, "market": market, "interval": interval})
        raise HTTPException(502, f"Unable to load Binance candles: {error}") from error
    return {"candles": [Candle(time=int(r[0] // 1000), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=float(r[5])).model_dump() for r in rows], "source": f"Binance {market}", "receivedAt": int(time.time() * 1000), "coverage": "exchange historical OHLCV"}


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
async def chart_overlays(symbol: str = Query("BTCUSDT"), interval: str = Query("5m"), market: str = Query("futures")):
    symbol, market = validate_symbol(symbol), validate_market(market)
    if interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        raise HTTPException(400, "Unsupported chart overlay interval")
    base, path = (FUTURES, "/fapi/v1/klines") if market == "futures" else (SPOT, "/api/v3/klines")
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
        return {"symbol": symbol, "interval": interval, "timezone": "UTC", "vwap": series, "previous": previous, "source": "Binance USDⓈ-M klines quote volume / base volume", "receivedAt": int(time.time() * 1000)}
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Unable to load anchored VWAP and previous period levels: {error}") from error


@app.get("/api/book")
async def order_book(symbol: str = Query("BTCUSDT"), market: str = Query("spot"), limit: int = Query(16, ge=5, le=100)):
    return (await service.book(validate_symbol(symbol), validate_market(market), limit)).model_dump()


@app.get("/api/trades")
async def trades(symbol: str = Query("BTCUSDT"), market: str = Query("spot"), limit: int = Query(24, ge=1, le=100)):
    symbol, market = validate_symbol(symbol), validate_market(market)
    sample = service.samples.get((market, symbol), deque())
    if len(sample) >= limit:
        rows = list(sample)[-limit:]
        rows.reverse()
    else:
        base, path = (FUTURES, "/fapi/v1/aggTrades") if market == "futures" else (SPOT, "/api/v3/aggTrades")
        response = await service.request(base, path, {"symbol": symbol, "limit": limit})
        rows = [Trade(id=int(r["a"]), time=int(r["T"]), price=float(r["p"]), quantity=float(r["q"]), side="sell" if r["m"] else "buy") for r in response]
        rows.reverse()
        known = {trade.id for trade in sample}
        for trade in reversed(rows):
            if trade.id not in known:
                sample.append(trade)
    return {"trades": [trade.model_dump() for trade in rows], "source": f"Binance {market} aggTrades", "receivedAt": int(time.time() * 1000), "coverage": "observed trades; bounded in-memory sample"}


@app.get("/api/reversal-signals")
async def reversal_signals(
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("5m"),
    market: str = Query("futures"),
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
    base, path = (FUTURES, "/fapi/v1/klines") if market == "futures" else (SPOT, "/api/v3/klines")
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


@app.get("/api/volatility")
async def volatility(symbol: str = Query("BTCUSDT"), market: str = Query("futures")):
    symbol, market = validate_symbol(symbol), validate_market(market)
    base, path = (FUTURES, "/fapi/v1/klines") if market == "futures" else (SPOT, "/api/v3/klines")
    try:
        rows = await service.request(base, path, {"symbol": symbol, "interval": "1d", "limit": 400})
        daily = [{"time": int(row[0] // 1000), "close": float(row[4])} for row in rows]
        return calculate_zones(symbol, daily)
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as error:
        raise HTTPException(502, f"Daily volatility data unavailable: {error}") from error


@app.get("/api/derivatives")
async def derivatives(symbol: str = Query("BTCUSDT")):
    symbol = validate_symbol(symbol)
    try:
        premium, interest = await asyncio.gather(service.request(FUTURES, "/fapi/v1/premiumIndex", {"symbol": symbol}), service.request(FUTURES, "/fapi/v1/openInterest", {"symbol": symbol}))
        return {"rate": float(premium["lastFundingRate"]), "markPrice": float(premium["markPrice"]), "indexPrice": float(premium["indexPrice"]), "openInterest": float(interest["openInterest"]), "nextFundingTime": int(premium["nextFundingTime"]), "eventTime": int(premium["time"]), "receivedAt": int(time.time() * 1000), "status": "live"}
    except (httpx.HTTPError, KeyError, ValueError) as error:
        raise HTTPException(502, f"Futures data unavailable: {error}") from error


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
    symbol = socket.query_params.get("symbol", "BTCUSDT").upper()
    market = socket.query_params.get("market", "spot")
    interval = socket.query_params.get("interval", "5m")
    if symbol not in SYMBOLS or market not in {"spot", "futures"} or interval not in INTERVALS:
        await socket.close(code=1008, reason="Unsupported symbol, market or interval")
        return
    await socket.accept()
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
