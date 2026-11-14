"""Daily VWAP rejection models: absorption, upthrust/shakeout and retest."""

from __future__ import annotations

import math
import time
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any


INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000,
}
MODEL_LABELS = {"absorption": "Absorção", "upthrust": "Upthrust/Shakeout", "retest": "Reteste"}


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
    volume_high: float = 1.8,
    volume_low: float = 0.7,
    spread_wide: float = 1.5,
    spread_narrow: float = 0.7,
    close_extreme: float = 0.7,
    average_periods: int = 20,
    signal_start_ms: int | None = None,
    source: str = "OHLCV",
    vwap_interval_ms: int | None = None,
) -> dict[str, Any]:
    interval_ms = INTERVAL_MS.get(interval)
    source_interval_ms = vwap_interval_ms or interval_ms
    settings = {
        "sigmaLevels": [1, 2, 3], "volumeHigh": volume_high, "volumeLow": volume_low,
        "spreadWide": spread_wide, "spreadNarrow": spread_narrow,
        "closeExtreme": close_extreme, "averagePeriods": average_periods,
        "resetTimezone": "UTC", "models": list(MODEL_LABELS.values()),
    }
    if interval_ms is None:
        return {"symbol": symbol, "interval": interval, "status": "unsupported_interval", "signals": [], "latest": None, "settings": settings, "source": source}

    # Caller timestamps are Unix seconds. Store source-bar states by the time
    # their complete bar becomes available, preventing a 5m candle from leaking
    # into a 1m signal before that source candle has closed.
    source_bars: list[dict[str, Any]] = []
    for candle in vwap_candles:
        try:
            timestamp = int(candle["time"]) * 1000
            volume = float(candle["volume"])
            high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])
            open_price = float(candle.get("open", close))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if timestamp <= 0 or volume <= 0 or not all(math.isfinite(v) for v in (volume, high, low, close, open_price)):
            continue
        price = _positive(candle.get("vwapPrice")) or (high + low + close) / 3
        source_bars.append({"time": timestamp, "available": timestamp + (source_interval_ms or 0), "day": _utc_day(timestamp), "volume": volume, "price": price})
    source_bars.sort(key=lambda item: item["time"])

    snapshots: dict[int, list[tuple[int, float, float]]] = {}
    snapshot_times: dict[int, list[int]] = {}
    current_day: int | None = None
    total_volume = mean = m2 = 0.0
    for bar in source_bars:
        if bar["day"] != current_day:
            current_day, total_volume, mean, m2 = bar["day"], 0.0, 0.0, 0.0
        next_total = total_volume + bar["volume"]
        delta = bar["price"] - mean
        mean += bar["volume"] / next_total * delta
        m2 += bar["volume"] * delta * (bar["price"] - mean)
        total_volume = next_total
        deviation = math.sqrt(max(0.0, m2 / total_volume))
        snapshots.setdefault(bar["day"], []).append((bar["available"], mean, deviation))
        snapshot_times.setdefault(bar["day"], []).append(bar["available"])

    # De-duplicate source rows by time and make all rolling calculations use
    # earlier bars only. No open candle enters the signal set.
    by_time: dict[int, dict[str, Any]] = {}
    for candle in candles:
        try:
            timestamp = int(candle["time"])
            values = {name: float(candle[name]) for name in ("open", "high", "low", "close", "volume")}
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if timestamp <= 0 or not all(math.isfinite(v) for v in values.values()):
            continue
        if values["volume"] > 0 and values["high"] >= values["low"]:
            by_time[timestamp] = {"time": timestamp, **values}
    bars = [by_time[key] for key in sorted(by_time)]

    metrics: list[dict[str, float] | None] = [None] * len(bars)
    observed = 0
    for index, bar in enumerate(bars):
        if signal_start_ms is not None and bar["time"] * 1000 < signal_start_ms:
            continue
        day = _utc_day(bar["time"] * 1000)
        available = snapshot_times.get(day, [])
        target_close = bar["time"] * 1000 + interval_ms
        snap_index = bisect_right(available, target_close) - 1
        if snap_index < 0:
            continue
        _, vwap, deviation = snapshots[day][snap_index]
        if deviation <= 0:
            continue
        observed += 1
        if index < average_periods:
            continue
        prior = bars[index - average_periods:index]
        mean_volume = sum(item["volume"] for item in prior) / average_periods
        mean_range = sum(max(0.0, item["high"] - item["low"]) for item in prior) / average_periods
        candle_range = bar["high"] - bar["low"]
        if mean_volume <= 0 or mean_range <= 0 or candle_range <= 0:
            continue
        metrics[index] = {
            "vwap": vwap, "deviation": deviation, "rvol": bar["volume"] / mean_volume, "volumeAverage": mean_volume,
            "spread": candle_range / mean_range, "clv": (bar["close"] - bar["low"]) / candle_range,
        }

    candidates: dict[tuple[int, str, int], dict[str, Any]] = {}

    def emit(confirm_index: int, event_index: int, direction: str, sigma: int, model: str, band: float, test_index: int | None = None) -> None:
        confirm, event = bars[confirm_index], bars[event_index]
        if _utc_day(confirm["time"] * 1000) != _utc_day(event["time"] * 1000):
            return
        if signal_start_ms is not None and confirm["time"] * 1000 < signal_start_ms:
            return
        key = (confirm["time"], direction, sigma)
        data = candidates.get(key)
        if data is None:
            data = {
                "time": confirm["time"], "symbol": symbol, "interval": interval,
                "direction": direction, "price": confirm["close"],
                "vwap": metrics[event_index]["vwap"] if metrics[event_index] else 0,
                "bandPrice": band, "sigma": sigma,
                "volumeRatio": metrics[event_index]["rvol"] if metrics[event_index] else 0,
                "volumeAverage": metrics[event_index]["volumeAverage"] if metrics[event_index] else 0,
                "relativeSpread": metrics[event_index]["spread"] if metrics[event_index] else 0,
                "closeLocation": metrics[event_index]["clv"] if metrics[event_index] else 0,
                "qualified": True, "models": [],
            }
            candidates[key] = data
        if model not in data["models"]:
            data["models"].append(model)
        if test_index is not None:
            data["testTime"] = bars[test_index]["time"]

    for index in range(average_periods, len(bars) - 1):
        metric = metrics[index]
        if metric is None:
            continue
        bar = bars[index]
        next_bar = bars[index + 1]
        # Events, retests and confirmations must remain within the same UTC VWAP session.
        if _utc_day(bar["time"] * 1000) != _utc_day(next_bar["time"] * 1000):
            continue
        for sigma in (1, 2, 3):
            lower = metric["vwap"] - sigma * metric["deviation"]
            upper = metric["vwap"] + sigma * metric["deviation"]
            buy_rejection = bar["low"] <= lower and bar["close"] > lower and metric["clv"] >= close_extreme
            sell_rejection = bar["high"] >= upper and bar["close"] < upper and metric["clv"] <= 1 - close_extreme

            # DV absorption: high relative volume, rejection close, then a
            # following candle takes out the absorption candle's extreme.
            if metric["rvol"] >= volume_high:
                if buy_rejection and next_bar["high"] > bar["high"]:
                    emit(index + 1, index, "BUY", sigma, "absorption", lower)
                if sell_rejection and next_bar["low"] < bar["low"]:
                    emit(index + 1, index, "SELL", sigma, "absorption", upper)

            # DV upthrust/shakeout: false break of the 10-bar local extreme,
            # a wide high-volume rejection and next-candle confirmation.
            if index >= 10 and metric["rvol"] >= volume_high and metric["spread"] >= spread_wide:
                previous = bars[index - 10:index]
                if sell_rejection and bar["high"] >= max(item["high"] for item in previous) and next_bar["low"] < bar["low"]:
                    emit(index + 1, index, "SELL", sigma, "upthrust", upper)
                if buy_rejection and bar["low"] <= min(item["low"] for item in previous) and next_bar["high"] > bar["high"]:
                    emit(index + 1, index, "BUY", sigma, "upthrust", lower)

            # DV retest: high-volume rejection, a low-volume/narrow retest
            # within five candles that holds the original extreme, then break.
            if metric["rvol"] < volume_high:
                continue
            for test_index in range(index + 1, min(index + 6, len(bars) - 1)):
                test_metric = metrics[test_index]
                test, confirm = bars[test_index], bars[test_index + 1]
                if _utc_day(test["time"] * 1000) != _utc_day(bar["time"] * 1000):
                    break
                if _utc_day(confirm["time"] * 1000) != _utc_day(bar["time"] * 1000):
                    break
                if test_metric is None or test_metric["rvol"] > volume_low or test_metric["spread"] > spread_narrow:
                    continue
                if buy_rejection and test["low"] <= lower and test["high"] >= lower and test["close"] > bar["low"] and confirm["high"] > test["high"]:
                    emit(test_index + 1, index, "BUY", sigma, "retest", lower, test_index)
                if sell_rejection and test["high"] >= upper and test["low"] <= upper and test["close"] < bar["high"] and confirm["low"] < test["low"]:
                    emit(test_index + 1, index, "SELL", sigma, "retest", upper, test_index)

    # Suppress same-candle, same-sigma opposing directions; retain one event
    # when multiple models confirm the same move (composite classification).
    signals: list[dict[str, Any]] = []
    grouped: dict[tuple[int, int], set[str]] = {}
    for timestamp, direction, sigma in candidates:
        grouped.setdefault((timestamp, sigma), set()).add(direction)
    for (timestamp, direction, sigma), signal in candidates.items():
        if len(grouped[(timestamp, sigma)]) > 1:
            continue
        signal["modelLabels"] = [MODEL_LABELS[name] for name in signal["models"]]
        signal["model"] = signal["models"][0] if len(signal["models"]) == 1 else "composite"
        signals.append(signal)
    signals.sort(key=lambda item: (item["time"], item["sigma"], item["direction"]))
    status = "ready" if observed >= average_periods + 1 else "insufficient_history"
    return {
        "symbol": symbol, "interval": interval, "status": status, "signals": signals[-80:],
        "latest": signals[-1] if signals else None, "settings": settings,
        "source": f"{source}; VWAP/desvio estimados por OHLCV ponderado por volume; sem bandas plotadas",
        "observedCandles": observed, "receivedAt": int(time.time() * 1000),
    }
