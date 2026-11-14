"""Daily VWAP deviation rejection signals, independent from reversal/breakout models."""

from __future__ import annotations

import math
import time
from bisect import bisect_right
from datetime import datetime, timezone
from statistics import median
from typing import Any


INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000,
}


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _utc_day(timestamp_ms: int) -> int:
    moment = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    return int(moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def build_vwap_deviation_signals(
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
    vwap_candles: list[dict[str, Any]],
    *,
    volume_multiple: float = 1.2,
    volume_lookback: int = 20,
    rejection_wick_body: float = 1.5,
    cooldown: int = 10,
    signal_start_ms: int | None = None,
    source: str = "OHLCV",
    vwap_interval_ms: int | None = None,
) -> dict[str, Any]:
    interval_ms = INTERVAL_MS.get(interval)
    source_interval_ms = vwap_interval_ms or interval_ms
    settings = {
        "sigmaLevels": [1, 2, 3], "volumeMultiple": volume_multiple,
        "volumeLookback": volume_lookback, "rejectionWickBody": rejection_wick_body,
        "cooldown": cooldown, "resetTimezone": "UTC",
    }
    if interval_ms is None:
        return {"symbol": symbol, "interval": interval, "status": "unsupported_interval", "signals": [], "latest": None, "settings": settings, "source": source}

    closed_vwap = []
    for candle in vwap_candles:
        try:
            # Public callers pass chart timestamps in Unix seconds; convert
            # source bars to milliseconds for UTC-day boundaries and lookup.
            timestamp = int(candle["time"]) * 1000
            volume = float(candle["volume"])
            high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])
            open_price = float(candle.get("open", close))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (volume, high, low, close, open_price)) or volume <= 0 or timestamp <= 0:
            continue
        vwap_price = _positive(candle.get("vwapPrice")) or (high + low + close) / 3
        closed_vwap.append({"time": timestamp, "day": _utc_day(timestamp), "volume": volume, "price": vwap_price})
    closed_vwap.sort(key=lambda item: item["time"])

    # Online volume-weighted mean/variance. Save one immutable state per source bar,
    # so each signal is checked only against information available by its close.
    snapshots: dict[int, list[tuple[int, float, float]]] = {}
    snapshot_times: dict[int, list[int]] = {}
    current_day: int | None = None
    total_volume = mean = m2 = 0.0
    for candle in closed_vwap:
        if candle["day"] != current_day:
            current_day, total_volume, mean, m2 = candle["day"], 0.0, 0.0, 0.0
        volume, price = candle["volume"], candle["price"]
        next_total = total_volume + volume
        delta = price - mean
        mean += volume / next_total * delta
        m2 += volume * delta * (price - mean)
        total_volume = next_total
        deviation = math.sqrt(max(0.0, m2 / total_volume)) if total_volume > 0 else 0.0
        available_at = candle["time"] + (source_interval_ms or 0)
        snapshots.setdefault(candle["day"], []).append((available_at, mean, deviation))
        snapshot_times.setdefault(candle["day"], []).append(available_at)

    ordered_candles = sorted(candles, key=lambda item: int(item.get("time", 0)))
    signals: list[dict[str, Any]] = []
    last_signal_index = -cooldown
    observed_bars = 0
    for index, candle in enumerate(ordered_candles):
        try:
            timestamp = int(candle["time"])
            open_price, high, low, close = (float(candle[name]) for name in ("open", "high", "low", "close"))
            volume = float(candle.get("volume", 0))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if signal_start_ms is not None and timestamp * 1000 < signal_start_ms:
            continue
        day = _utc_day(timestamp * 1000)
        day_snapshots = snapshots.get(day, [])
        # Select last completed VWAP source bar at or before this chart candle's close.
        target_close_ms = timestamp * 1000 + interval_ms
        snapshot_index = bisect_right(snapshot_times.get(day, []), target_close_ms) - 1
        if snapshot_index < 0:
            continue
        snapshot = day_snapshots[snapshot_index]
        _, vwap, deviation = snapshot
        if deviation <= 0 or not all(math.isfinite(value) for value in (open_price, high, low, close, volume)):
            continue
        observed_bars += 1
        if volume <= 0 or index < volume_lookback:
            continue
        prior_volumes = [float(item.get("volume", 0)) for item in ordered_candles[index - volume_lookback:index]]
        if len(prior_volumes) != volume_lookback or any(not math.isfinite(value) or value <= 0 for value in prior_volumes):
            continue
        median_volume = median(prior_volumes)
        volume_ratio = volume / median_volume if median_volume > 0 else 0
        if volume_ratio < volume_multiple:
            continue

        body = abs(close - open_price)
        if body <= 0:
            continue
        lower_wick = max(0.0, min(open_price, close) - low)
        upper_wick = max(0.0, high - max(open_price, close))
        candidate = None
        # If one candle rejects multiple bands, retain the farthest confirmed level.
        for sigma in (3, 2, 1):
            lower_band, upper_band = vwap - sigma * deviation, vwap + sigma * deviation
            if low <= lower_band and close > lower_band and close > open_price and lower_wick / body >= rejection_wick_body:
                candidate = ("BUY", sigma, lower_band, lower_wick / body)
                break
            if high >= upper_band and close < upper_band and close < open_price and upper_wick / body >= rejection_wick_body:
                candidate = ("SELL", sigma, upper_band, upper_wick / body)
                break
        if not candidate or index - last_signal_index < cooldown:
            continue
        direction, sigma, band, wick_ratio = candidate
        signals.append({
            "time": timestamp, "symbol": symbol, "interval": interval, "direction": direction,
            "price": close, "vwap": vwap, "bandPrice": band, "sigma": sigma,
            "volumeRatio": volume_ratio, "volumeMedian": median_volume,
            "wickBodyRatio": wick_ratio, "qualified": True,
        })
        last_signal_index = index

    status = "ready" if observed_bars >= volume_lookback + 1 else "insufficient_history"
    return {
        "symbol": symbol, "interval": interval, "status": status, "signals": signals[-40:],
        "latest": signals[-1] if signals else None, "settings": settings,
        "source": f"{source}; VWAP e desvio estimados por OHLCV ponderado por volume; bandas calculadas sem serem plotadas",
        "observedCandles": observed_bars, "receivedAt": int(time.time() * 1000),
    }
