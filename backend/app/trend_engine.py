"""Closed-candle trend-following signals for Binance Spot 5-minute candles."""

from __future__ import annotations

import math
from typing import Any


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2 / (period + 1)
    for index in range(period, len(values)):
        current = values[index] * multiplier + current * (1 - multiplier)
        result[index] = current
    return result


def _dmi_adx(candles: list[dict[str, Any]], period: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    length = len(candles)
    plus_di: list[float | None] = [None] * length
    minus_di: list[float | None] = [None] * length
    adx: list[float | None] = [None] * length
    if length <= period * 2:
        return plus_di, minus_di, adx

    true_ranges: list[float] = []
    plus_moves: list[float] = []
    minus_moves: list[float] = []
    for index in range(1, length):
        candle, previous = candles[index], candles[index - 1]
        high, low, previous_close = float(candle["high"]), float(candle["low"]), float(previous["close"])
        up_move = high - float(previous["high"])
        down_move = float(previous["low"]) - low
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        plus_moves.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_moves.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus = sum(plus_moves[:period])
    smoothed_minus = sum(minus_moves[:period])
    dx_values: list[tuple[int, float]] = []

    for index in range(period, length):
        if index > period:
            offset = index - 1
            smoothed_tr += true_ranges[offset] - smoothed_tr / period
            smoothed_plus += plus_moves[offset] - smoothed_plus / period
            smoothed_minus += minus_moves[offset] - smoothed_minus / period
        if smoothed_tr <= 0:
            continue
        positive = 100 * smoothed_plus / smoothed_tr
        negative = 100 * smoothed_minus / smoothed_tr
        plus_di[index], minus_di[index] = positive, negative
        denominator = positive + negative
        if denominator > 0:
            dx_values.append((index, 100 * abs(positive - negative) / denominator))

    if len(dx_values) < period:
        return plus_di, minus_di, adx
    seed = dx_values[:period]
    current = sum(value for _, value in seed) / period
    adx[seed[-1][0]] = current
    for index, value in dx_values[period:]:
        current = (current * (period - 1) + value) / period
        adx[index] = current
    return plus_di, minus_di, adx


def _daily_vwap_sigma(candles: list[dict[str, Any]]) -> tuple[list[float | None], list[float | None]]:
    vwap_values: list[float | None] = [None] * len(candles)
    sigma_values: list[float | None] = [None] * len(candles)
    day: int | None = None
    total_volume = mean = m2 = 0.0
    for index, candle in enumerate(candles):
        current_day = int(candle["time"]) // 86_400
        if current_day != day:
            day, total_volume, mean, m2 = current_day, 0.0, 0.0, 0.0
        volume = float(candle["volume"])
        if volume <= 0:
            continue
        price = (float(candle["high"]) + float(candle["low"]) + float(candle["close"])) / 3
        next_volume = total_volume + volume
        delta = price - mean
        mean += volume / next_volume * delta
        m2 += volume * delta * (price - mean)
        total_volume = next_volume
        vwap_values[index] = mean
        sigma_values[index] = math.sqrt(max(0.0, m2 / total_volume))
    return vwap_values, sigma_values


def build_trend_signals(symbol: str, interval: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
    settings = {
        "interval": "5m", "emaFast": 9, "emaSlow": 21, "adxPeriod": 14,
        "adxMinimum": 23, "rocPeriod": 5, "volumeMultiple": 1.3,
        "volumeLookback": 20, "vwapSigmaLimit": 2, "vwapTimezone": "UTC",
    }
    if interval != "5m":
        return {"symbol": symbol, "interval": interval, "status": "unsupported_interval", "signals": [], "latest": None, "settings": settings}

    clean: list[dict[str, Any]] = []
    for candle in candles:
        try:
            row = {key: float(candle[key]) for key in ("open", "high", "low", "close", "volume")}
            timestamp = int(candle["time"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if timestamp <= 0 or not all(math.isfinite(value) for value in row.values()):
            continue
        if row["close"] <= 0 or row["volume"] <= 0 or row["high"] < row["low"]:
            continue
        clean.append({"time": timestamp, **row})
    clean.sort(key=lambda item: item["time"])
    # API callers provide closed candles; deduplicate by opening timestamp.
    by_time = {candle["time"]: candle for candle in clean}
    bars = [by_time[key] for key in sorted(by_time)]
    minimum = 2 * 14 + 1
    if len(bars) < minimum:
        return {"symbol": symbol, "interval": interval, "status": "insufficient_history", "requiredCandles": minimum, "observedCandles": len(bars), "signals": [], "latest": None, "settings": settings}

    closes = [float(item["close"]) for item in bars]
    fast_ema, slow_ema = _ema(closes, 9), _ema(closes, 21)
    plus_di, minus_di, adx = _dmi_adx(bars, 14)
    vwap, sigma = _daily_vwap_sigma(bars)
    signals: list[dict[str, Any]] = []
    previous_direction: str | None = None

    for index, bar in enumerate(bars):
        direction: str | None = None
        roc = previous_roc = None
        if index >= 6:
            roc = (closes[index] / closes[index - 5] - 1) * 100
            previous_roc = (closes[index - 1] / closes[index - 6] - 1) * 100
        prior_volumes = [float(item["volume"]) for item in bars[max(0, index - 20):index]]
        volume_average = sum(prior_volumes) / len(prior_volumes) if len(prior_volumes) == 20 else 0.0
        volume_ratio = float(bar["volume"]) / volume_average if volume_average > 0 else 0.0
        current_adx = adx[index]
        previous_adx = adx[index - 1] if index else None
        current_vwap, current_sigma = vwap[index], sigma[index]

        if (
            fast_ema[index] is not None and slow_ema[index] is not None
            and current_adx is not None and previous_adx is not None
            and plus_di[index] is not None and minus_di[index] is not None
            and roc is not None and previous_roc is not None
            and current_vwap is not None and current_sigma is not None and current_sigma > 0
            and volume_ratio >= 1.3 and current_adx >= 23 and current_adx > previous_adx
        ):
            close = float(bar["close"])
            upper_limit, lower_limit = current_vwap + 2 * current_sigma, current_vwap - 2 * current_sigma
            buy = (fast_ema[index] > slow_ema[index] and plus_di[index] > minus_di[index]
                   and roc > 0 and roc > previous_roc and close > current_vwap and close <= upper_limit)
            sell = (fast_ema[index] < slow_ema[index] and minus_di[index] > plus_di[index]
                    and roc < 0 and roc < previous_roc and close < current_vwap and close >= lower_limit)
            direction = "BUY" if buy else "SELL" if sell else None

        # Emit on entry into a qualifying trend, rather than repeating the same
        # marker on every five-minute candle while all filters remain true.
        if direction is not None and direction != previous_direction:
            signals.append({
                "time": int(bar["time"]), "symbol": symbol, "interval": interval,
                "direction": direction, "price": float(bar["close"]),
                "adx": current_adx, "plusDI": plus_di[index], "minusDI": minus_di[index],
                "roc5": roc, "volumeRatio": volume_ratio, "vwap": current_vwap,
                "sigmaDistance": (float(bar["close"]) - current_vwap) / current_sigma,
                "qualified": True,
            })
        previous_direction = direction

    return {
        "symbol": symbol, "interval": interval, "status": "ready",
        "signals": signals[-80:], "latest": signals[-1] if signals else None,
        "settings": settings,
        "source": "Binance Spot OHLCV; indicadores confirmados em candles fechados; VWAP e desvio estimados por OHLCV; sinal emitido ao entrar no alinhamento TREND",
        "observedCandles": len(bars),
    }
