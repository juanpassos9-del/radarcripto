"""Daily GARCH(1,1) and historical-volatility price zones."""

from __future__ import annotations

import math
import statistics
import time
from typing import Any


def calculate_zones(
    symbol: str,
    daily: list[dict[str, float | int]],
    *,
    omega: float = 0.000001,
    alpha: float = 0.08,
    beta: float = 0.90,
    initial_period: int = 100,
    hv_period: int = 252,
) -> dict[str, Any]:
    # Binance's last daily kline is still open. Exclude it so zones only use
    # information available at the last fully closed daily candle.
    closed = daily[:-1]
    closes = [float(row["close"]) for row in closed if float(row["close"]) > 0]
    times = [int(row["time"]) for row in closed if float(row["close"]) > 0]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(closes) < 2 or len(returns) < initial_period + 1:
        return {"symbol": symbol, "status": "insufficient_history", "observations": len(returns), "required": initial_period + 1}

    if alpha < 0 or beta < 0 or omega <= 0 or alpha + beta >= 1:
        return {"symbol": symbol, "status": "invalid_parameters", "observations": len(returns)}

    shocks = [value * value for value in returns]
    variance = sum(shocks[:initial_period]) / initial_period
    # Match the Pine model: variance uses the prior return shock on each step.
    for index in range(initial_period, len(shocks)):
        variance = omega + alpha * shocks[index - 1] + beta * variance
    garch_daily = math.sqrt(max(variance, 0.0))
    hv_sample = returns[-hv_period:]
    hv_daily = statistics.stdev(hv_sample) if len(hv_sample) >= hv_period else None
    reference = closes[-1]  # last fully closed daily candle

    def band(vol: float | None, multiple: float) -> dict[str, float | None]:
        if vol is None:
            return {"upper": None, "lower": None}
        return {"upper": reference * math.exp(vol * multiple), "lower": reference * math.exp(-vol * multiple)}

    return {
        "symbol": symbol,
        "status": "ready",
        "source": "Binance Spot daily klines",
        "referenceMode": "previous_daily_close",
        "reference": reference,
        "asOf": times[-1] * 1000,
        "receivedAt": int(time.time() * 1000),
        "observations": len(returns),
        "garch": {"dailyVol": garch_daily, "annualizedVol": garch_daily * math.sqrt(252), "omega": omega, "alpha": alpha, "beta": beta, "initialPeriod": initial_period,
                  "bands": {str(mult): band(garch_daily, mult) for mult in (0.5, 1, 1.5, 2, 2.5, 3)}},
        "hv252": {"status": "ready" if hv_daily is not None else "insufficient_history", "dailyVol": hv_daily, "annualizedVol": hv_daily * math.sqrt(252) if hv_daily is not None else None,
                  "period": hv_period, "observations": len(hv_sample),
                  "bands": {str(mult): band(hv_daily, mult) for mult in (1, 2, 3)}},
        "method": "Lognormal exp(±σ)",
        "lookahead": "excluded current unfinished daily candle; bands anchored to last closed daily close",
    }
