"""Deterministic, closed-candle reversals at daily volatility bands."""

from __future__ import annotations

from typing import Any


def _pattern(candle: dict[str, Any], previous: dict[str, Any], band: float, side: str, tolerance: float) -> str | None:
    open_price, high, low, close = (float(candle[key]) for key in ("open", "high", "low", "close"))
    prev_open, prev_close = float(previous["open"]), float(previous["close"])
    span = high - low
    if span <= 0:
        return None
    body = abs(close - open_price)
    body_floor = max(body, span * 0.015)
    lower_wick = min(open_price, close) - low
    upper_wick = high - max(open_price, close)
    close_location = (close - low) / span
    bullish, bearish = close > open_price, close < open_price
    inside = close > band if side == "BUY" else close < band
    if not inside:
        return None

    # A false break is the clearest rejection: price exceeded the band zone,
    # then the candle closed back on the inside of the band.
    false_break = low < band * (1 - tolerance) if side == "BUY" else high > band * (1 + tolerance)
    if false_break:
        return "Falso rompimento"

    engulfing = (
        bullish and prev_close < prev_open and open_price <= prev_close and close >= prev_open
        if side == "BUY"
        else bearish and prev_close > prev_open and open_price >= prev_close and close <= prev_open
    )
    if engulfing:
        return "Engolfo"

    pin_bar = (
        lower_wick >= 1.8 * body_floor and close_location >= 0.60 and bullish
        if side == "BUY"
        else upper_wick >= 1.8 * body_floor and close_location <= 0.40 and bearish
    )
    if pin_bar:
        return "Pin bar"

    previous_outside = prev_close <= band if side == "BUY" else prev_close >= band
    if previous_outside:
        return "Fechamento de volta para dentro"

    impulse = body >= 0.65 * span and (bullish and close_location >= 0.70 if side == "BUY" else bearish and close_location <= 0.30)
    if impulse:
        return "Impulso reversivo"
    return None


def _score(sigma: float, pattern: str, volume_ratio: float | None, source: str) -> int:
    sigma_points = 15 if sigma < 2 else 20 if sigma < 2.5 else 25
    pattern_points = 30 if pattern in {"Pin bar", "Engolfo", "Falso rompimento"} else 25
    volume_points = 15 if volume_ratio is not None and volume_ratio >= 1.2 else 0
    source_points = 15 if source == "GARCH" else 0
    return sigma_points + pattern_points + volume_points + source_points + 15  # band touch


def _strength(score: int) -> str:
    if score >= 75:
        return "forte"
    if score >= 50:
        return "moderada"
    return "fraca"


def build_reversal_signals(
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
    zones_by_candle: list[dict[str, Any] | None],
    *,
    min_sigma: float = 1.0,
    touch_tolerance: float = 0.001,
    min_score: int = 50,
    cooldown: int = 10,
    buy_enabled: bool = True,
    sell_enabled: bool = True,
    garch_enabled: bool = True,
    hv_enabled: bool = True,
    volume_enabled: bool = True,
) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    last_signal_index = -cooldown
    suppressed = 0
    last_candidate: dict[str, Any] | None = None

    for index in range(20, min(len(candles), len(zones_by_candle))):
        candle = candles[index]
        zones = zones_by_candle[index]
        if not zones or zones.get("status") != "ready" or zones.get("reference") is None:
            continue
        previous = candles[index - 1]
        prior_volumes = [item.get("volume") for item in candles[index - 20:index]]
        volumes_available = all(value is not None for value in prior_volumes) and candle.get("volume") is not None
        average_volume = sum(float(value) for value in prior_volumes) / 20 if volumes_available else None
        volume_ratio = float(candle["volume"]) / average_volume if average_volume and average_volume > 0 else None
        volume_passes = not volume_enabled or volume_ratio is None or volume_ratio >= 1.2

        candidates: list[dict[str, Any]] = []
        sources = []
        if garch_enabled and zones.get("garch"):
            sources.append(("GARCH", zones["garch"].get("bands", {}), zones["garch"].get("dailyVol")))
        if hv_enabled and zones.get("hv252", {}).get("dailyVol") is not None:
            sources.append(("HV252", zones["hv252"].get("bands", {}), zones["hv252"].get("dailyVol")))

        for source, bands, daily_vol in sources:
            if not daily_vol or daily_vol <= 0:
                continue
            for multiple_text, band in bands.items():
                try:
                    sigma = float(multiple_text)
                except (TypeError, ValueError):
                    continue
                if sigma < min_sigma:
                    continue
                for side, field in (("BUY", "lower"), ("SELL", "upper")):
                    if (side == "BUY" and not buy_enabled) or (side == "SELL" and not sell_enabled):
                        continue
                    price = band.get(field)
                    if price is None or float(price) <= 0:
                        continue
                    band_price = float(price)
                    touched = float(candle["low"]) <= band_price * (1 + touch_tolerance) and float(candle["high"]) >= band_price * (1 - touch_tolerance)
                    if not touched:
                        continue
                    pattern = _pattern(candle, previous, band_price, side, touch_tolerance)
                    if not pattern or not volume_passes:
                        continue
                    score = _score(sigma, pattern, volume_ratio, source)
                    candidate = {
                        "time": int(candle["time"]), "symbol": symbol, "interval": interval,
                        "direction": side, "price": float(candle["close"]), "bandPrice": band_price,
                        "sigma": sigma, "source": source, "pattern": pattern, "score": score,
                        "strength": _strength(score), "volumeRatio": volume_ratio,
                        "qualified": score >= min_score,
                    }
                    candidates.append(candidate)

        if not candidates:
            continue
        directions = {item["direction"] for item in candidates}
        if len(directions) > 1:
            continue  # a candle spanning both sides is ambiguous, so emit no reversal
        candidates.sort(key=lambda item: (item["score"], item["sigma"], item["source"] == "GARCH"), reverse=True)
        best = candidates[0]
        best["sources"] = sorted({item["source"] for item in candidates})
        best["reasons"] = [
            f"Rejeição confirmada por {best['pattern'].lower()}",
            f"Toque em banda {best['source']} de {best['sigma']:g}σ",
            f"Volume {volume_ratio:.2f}× a média anterior" if volume_ratio is not None else "Volume indisponível; filtro liberado sem pontos de volume",
        ]
        last_candidate = best
        if not best["qualified"]:
            continue
        if index - last_signal_index < cooldown:
            best["suppressedByCooldown"] = True
            suppressed += 1
            continue
        best["suppressedByCooldown"] = False
        signals.append(best)
        last_signal_index = index

    return {
        "symbol": symbol,
        "interval": interval,
        "signals": signals[-40:],
        "latest": signals[-1] if signals else None,
        "lastCandidate": last_candidate,
        "suppressedByCooldown": suppressed,
        "settings": {
            "minSigma": min_sigma, "touchTolerance": touch_tolerance, "volumeMultiple": 1.2,
            "volumeLookback": 20, "minScore": min_score, "cooldown": cooldown,
            "buyEnabled": buy_enabled, "sellEnabled": sell_enabled,
            "garchEnabled": garch_enabled, "hvEnabled": hv_enabled, "volumeEnabled": volume_enabled,
        },
        "scoreRules": {"sigma": "15/20/25", "pattern": "25/30", "volume": 15, "garchSource": 15, "touch": 15},
    }
