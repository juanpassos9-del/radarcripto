"""Closed-candle volatility-squeeze breakouts with volume confirmation."""

from __future__ import annotations

import math
from typing import Any


def _bandwidth(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    if mean <= 0:
        return None
    variance = sum((value - mean) ** 2 for value in window) / period
    return 4 * math.sqrt(variance) / mean  # (upper - lower) / middle, 2 deviations


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _atr(candles: list[dict[str, Any]], end: int, period: int = 14) -> float | None:
    """ATR over candles ending before `end`; excludes the breakout candle."""
    start = end - period
    if start < 1:
        return None
    true_ranges = []
    for index in range(start, end):
        candle = candles[index]
        previous_close = float(candles[index - 1]["close"])
        high, low = float(candle["high"]), float(candle["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges) / period


def build_breakout_signals(
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
    *,
    bb_period: int = 20,
    compression_lookback: int = 100,
    compression_percentile: float = 20,
    compression_recent_bars: int = 5,
    range_lookback: int = 20,
    volume_lookback: int = 20,
    volume_multiple: float = 1.5,
    breakout_atr_multiple: float = 0.15,
    atr_period: int = 14,
    cooldown: int = 10,
    buy_enabled: bool = True,
    sell_enabled: bool = True,
) -> dict[str, Any]:
    widths: list[float | None] = [_bandwidth([float(c["close"]) for c in candles[:i + 1]], bb_period) for i in range(len(candles))]
    minimum = bb_period + compression_lookback + max(range_lookback, volume_lookback, atr_period + 1)
    if len(candles) < minimum + 1:
        return {
            "symbol": symbol, "interval": interval, "status": "insufficient_history",
            "requiredCandles": minimum + 1, "observedCandles": len(candles), "signals": [], "latest": None,
            "settings": _settings(bb_period, compression_lookback, compression_percentile, compression_recent_bars, range_lookback, volume_lookback, volume_multiple, breakout_atr_multiple, atr_period, cooldown, buy_enabled, sell_enabled),
        }

    signals: list[dict[str, Any]] = []
    last_signal_index = -cooldown
    last_candidate: dict[str, Any] | None = None
    for index in range(minimum, len(candles)):
        candle = candles[index]
        prior_volumes = [float(item["volume"]) for item in candles[index - volume_lookback:index]]
        median_volume = _median(prior_volumes)
        volume_ratio = float(candle["volume"]) / median_volume if median_volume > 0 else None
        if volume_ratio is None or volume_ratio < volume_multiple:
            continue

        compressed_at: int | None = None
        start = max(bb_period + compression_lookback, index - compression_recent_bars)
        for candidate_index in range(start, index):
            width = widths[candidate_index]
            history = [item for item in widths[max(bb_period - 1, candidate_index - compression_lookback):candidate_index] if item is not None]
            if width is not None and len(history) >= compression_lookback and width <= _percentile(history, compression_percentile):
                compressed_at = candidate_index
                break
        if compressed_at is None:
            continue

        prior_range = candles[index - range_lookback:index]
        range_high = max(float(item["high"]) for item in prior_range)
        range_low = min(float(item["low"]) for item in prior_range)
        atr = _atr(candles, index, atr_period)
        if atr is None or atr <= 0:
            continue
        close = float(candle["close"])
        direction = "BUY" if close > range_high + breakout_atr_multiple * atr else "SELL" if close < range_low - breakout_atr_multiple * atr else None
        if direction is None or (direction == "BUY" and not buy_enabled) or (direction == "SELL" and not sell_enabled):
            continue
        last_candidate = {
            "time": int(candle["time"]), "symbol": symbol, "interval": interval, "direction": direction,
            "price": close, "rangeHigh": range_high, "rangeLow": range_low,
            "atr": atr, "atrPeriod": atr_period, "atrMultiple": breakout_atr_multiple,
            "distanceAtr": (close - range_high) / atr if direction == "BUY" else (range_low - close) / atr,
            "volumeRatio": volume_ratio, "volumeMedian": median_volume, "compressionPercentile": compression_percentile,
            "qualified": True, "compressedAt": int(candles[compressed_at]["time"]),
            "reasons": [f"Largura das Bandas de Bollinger comprimida (percentil ≤{compression_percentile:g}) nos últimos {compression_recent_bars} candles", f"Fechamento ultrapassou a faixa em {((close - range_high) if direction == 'BUY' else (range_low - close)):.8g} ({((close - range_high) if direction == 'BUY' else (range_low - close)) / atr:.2f} ATR)", f"Volume {volume_ratio:.2f}× a mediana dos {volume_lookback} candles anteriores"],
        }
        if index - last_signal_index < cooldown:
            continue
        signals.append(last_candidate)
        last_signal_index = index

    return {
        "symbol": symbol, "interval": interval, "status": "ready", "signals": signals[-40:],
        "latest": signals[-1] if signals else None, "lastCandidate": last_candidate,
        "settings": _settings(bb_period, compression_lookback, compression_percentile, compression_recent_bars, range_lookback, volume_lookback, volume_multiple, breakout_atr_multiple, atr_period, cooldown, buy_enabled, sell_enabled),
        "source": "Binance Spot OHLCV; apenas candles fechados; compressão avaliada sem dados futuros",
    }


def _settings(bb_period: int, compression_lookback: int, compression_percentile: float, compression_recent_bars: int, range_lookback: int, volume_lookback: int, volume_multiple: float, breakout_atr_multiple: float, atr_period: int, cooldown: int, buy_enabled: bool, sell_enabled: bool) -> dict[str, Any]:
    return {
        "bbPeriod": bb_period, "compressionLookback": compression_lookback,
        "compressionPercentile": compression_percentile, "compressionRecentBars": compression_recent_bars,
        "rangeLookback": range_lookback, "volumeLookback": volume_lookback,
        "volumeMultiple": volume_multiple, "breakoutAtrMultiple": breakout_atr_multiple,
        "atrPeriod": atr_period, "cooldown": cooldown,
        "buyEnabled": buy_enabled, "sellEnabled": sell_enabled,
    }
