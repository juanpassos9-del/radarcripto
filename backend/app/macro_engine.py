from __future__ import annotations

import math
from typing import Any


MACRO_ASSETS: dict[str, dict[str, str]] = {
    "NCSIDXY2USD-USDT": {"key": "DXY", "label": "DXY", "category": "fx", "color": "#52d6d0"},
    "NCFXEUR2USD-USDT": {"key": "EURUSD", "label": "EUR/USD", "category": "fx", "color": "#a68bff"},
    "NCFXGBP2USD-USDT": {"key": "GBPUSD", "label": "GBP/USD", "category": "fx", "color": "#f3a45d"},
    "NCFXAUD2USD-USDT": {"key": "AUDUSD", "label": "AUD/USD", "category": "fx", "color": "#48d69a"},
    "NCFXNZD2USD-USDT": {"key": "NZDUSD", "label": "NZD/USD", "category": "fx", "color": "#56a9ff"},
    "NCFXUSD2JPY-USDT": {"key": "USDJPY", "label": "USD/JPY", "category": "fx", "color": "#ff7391"},
    "NCSISP5002USD-USDT": {"key": "SP500", "label": "S&P 500", "category": "index", "color": "#ffca61"},
    "NCSINASDAQ1002USD-USDT": {"key": "NASDAQ100", "label": "NASDAQ 100", "category": "index", "color": "#63b6ff"},
    "NCSIRUSSELL20002USD-USDT": {"key": "RUSSELL2000", "label": "Russell 2000", "category": "index", "color": "#cf87ff"},
    "NCSIDOWJONES2USD-USDT": {"key": "DOW", "label": "Dow Jones", "category": "index", "color": "#f481a6"},
    "NCCO1OILWTI2USD-USDT": {"key": "WTI", "label": "WTI", "category": "energy", "color": "#ff795f"},
    "NCCO1OILBRENT2USD-USDT": {"key": "BRENT", "label": "Brent", "category": "energy", "color": "#ffad57"},
    "NCCOGOLD2USD-USDT": {"key": "GOLD", "label": "Ouro", "category": "metal", "color": "#f3d56a"},
    "NCCO724COPPER2USD-USDT": {"key": "COPPER", "label": "Cobre", "category": "metal", "color": "#dd8a59"},
    "HL:para:2Y": {"key": "UST2Y", "label": "Treasury 2Y · yield", "category": "rates", "color": "#65d5ef", "source": "Hyperliquid · Paragon", "unit": "yield_pct", "riskDirection": "inverse"},
    "HL:para:10Y": {"key": "UST10Y", "label": "Treasury 10Y · yield", "category": "rates", "color": "#f29cdd", "source": "Hyperliquid · Paragon", "unit": "yield_pct", "riskDirection": "inverse"},
    "HL:para:30Y": {"key": "UST30Y", "label": "Treasury 30Y · yield", "category": "rates", "color": "#ff9d68", "source": "Hyperliquid · Paragon", "unit": "yield_pct", "riskDirection": "inverse"},
    "HL:xyz:DRAM": {"key": "DRAM", "label": "DRAM/USDC · perpétuo sintético", "category": "equity", "color": "#75d7ff", "source": "Hyperliquid · XYZ", "unit": "USDC"},
    "HL:xyz:EWZ": {"key": "EWZ", "label": "EWZ sintético", "category": "equity", "color": "#57d69a", "source": "Hyperliquid · XYZ"},
    "HL:xyz:SOXL": {"key": "SOXL", "label": "SOXL/USDC · perpétuo sintético", "category": "equity", "color": "#ff80bf", "source": "Hyperliquid · XYZ", "unit": "USDC"},
    "HL:xyz:TLT": {"key": "TLT", "label": "TLT sintético", "category": "bond", "color": "#e3bd65", "source": "Hyperliquid · XYZ"},
    "HL:spot:WBRL-USDC": {"key": "WBRLUSDC", "label": "WBRL/USDC · real empacotado", "category": "fx", "color": "#b8e986", "source": "Hyperliquid Spot", "unit": "USDC por WBRL"},
}

MOMENTUM_WINDOWS = (1, 5, 15, 60)


def _near_price(points: list[dict[str, Any]], target_ms: int, tolerance_ms: int = 120_000) -> dict[str, Any] | None:
    match = next((point for point in reversed(points) if point["time"] <= target_ms), None)
    return match if match and target_ms - match["time"] <= tolerance_ms else None


def build_macro_snapshot(
    catalog: list[dict[str, Any]],
    histories: dict[str, list[dict[str, Any]]],
    live: dict[str, dict[str, Any]],
    now_ms: int,
    storage: str,
    durable: bool,
) -> dict[str, Any]:
    instruments = {item["symbol"]: item for item in catalog}
    measures: dict[str, dict[str, Any]] = {}
    chart_series: list[dict[str, Any]] = []
    oldest_sample: int | None = None
    live_count = 0

    for symbol, metadata in MACRO_ASSETS.items():
        instrument = instruments.get(symbol)
        if not instrument:
            measures[metadata["key"]] = {"symbol": symbol, **metadata, "available": False, "price": None, "change5mPct": None, "change30mPct": None, "acceleration5mPp": None, "z5m": None, "windows": {}, "sampleCount": 0, "ageMs": None, "status": "unavailable"}
            continue
        points = sorted(histories.get(symbol, []), key=lambda point: point["time"])
        sample_count = len(points)
        # Add the latest feed value for the now leg; the baselines remain aligned 1-minute samples.
        current = live.get(symbol)
        has_live = bool(current and now_ms - int(current.get("receivedAt", 0)) <= 90_000)
        if has_live:
            live_count += 1
            points = [point for point in points if point["time"] < now_ms - 1_000]
            points.append({"time": now_ms, "price": float(current["price"])})
            age_ms = now_ms - int(current.get("receivedAt", now_ms))
        else:
            age_ms = now_ms - points[-1]["time"] if points else None
        points = [point for point in points if point["price"] > 0]
        first = points[0] if points else None
        latest = points[-1] if points else None
        is_yield = metadata.get("unit") == "yield_pct"
        recent = [point for point in points if now_ms - 60 * 60_000 <= point["time"] <= now_ms]
        one_minute_returns = [
            (right["price"] - left["price"]) * 100 if is_yield else (right["price"] / left["price"] - 1) * 100
            for left, right in zip(recent, recent[1:])
            if left["price"] > 0 and 30_000 <= right["time"] - left["time"] <= 90_000
        ]
        volatility_1m = None
        if len(one_minute_returns) >= 12:
            mean_1m = sum(one_minute_returns) / len(one_minute_returns)
            variance_1m = sum((value - mean_1m) ** 2 for value in one_minute_returns) / len(one_minute_returns)
            volatility_1m = math.sqrt(variance_1m)
        windows: dict[str, dict[str, float | None | str]] = {}
        latest_is_fresh = bool(latest and now_ms - latest["time"] <= 120_000)
        for minutes in (1, 5, 15, 30, 60):
            base = _near_price(points, now_ms - minutes * 60_000) if latest_is_fresh else None
            change = None
            if latest and base and base["price"] > 0:
                change = (latest["price"] - base["price"]) * 100 if is_yield else (latest["price"] / base["price"] - 1) * 100
            z_score = None
            if minutes in MOMENTUM_WINDOWS and change is not None and volatility_1m and volatility_1m > 1e-8:
                z_score = change / (volatility_1m * math.sqrt(minutes))
            windows[str(minutes)] = {
                "change": round(change, 4) if change is not None else None,
                "velocityPerMinute": round(change / minutes, 5) if change is not None else None,
                "z": round(z_score, 3) if z_score is not None else None,
                "direction": "alta" if change is not None and change > 0 else "queda" if change is not None and change < 0 else "neutro" if change is not None else "indisponível",
            }
        change_5m = windows["5"]["change"]
        change_30m = windows["30"]["change"]
        at_5m = _near_price(points, now_ms - 5 * 60_000) if latest_is_fresh else None
        at_10m = _near_price(points, now_ms - 10 * 60_000) if latest_is_fresh else None
        prior_5m = (at_5m["price"] / at_10m["price"] - 1) * 100 if at_5m and at_10m and not is_yield else ((at_5m["price"] - at_10m["price"]) * 100 if at_5m and at_10m else None)
        acceleration = change_5m - prior_5m if change_5m is not None and prior_5m is not None else None
        change_5m_bps = change_5m if is_yield else None
        change_30m_bps = change_30m if is_yield else None
        acceleration_bps = acceleration if is_yield else None
        if not is_yield:
            change_5m = windows["5"]["change"]
            change_30m = windows["30"]["change"]
        z5m = windows["5"]["z"]
        if first:
            oldest_sample = first["time"] if oldest_sample is None else min(oldest_sample, first["time"])
        measures[metadata["key"]] = {
            "symbol": symbol, **metadata, "available": True,
            "price": latest["price"] if latest else None,
            "quoteVolume24h": current.get("quoteVolume24h") if current else None,
            "change5mPct": round(change_5m, 4) if change_5m is not None else None,
            "change30mPct": round(change_30m, 4) if change_30m is not None else None,
            "acceleration5mPp": round(acceleration, 4) if acceleration is not None else None,
            "change5mBps": round(change_5m_bps, 3) if change_5m_bps is not None else None,
            "change30mBps": round(change_30m_bps, 3) if change_30m_bps is not None else None,
            "acceleration5mBps": round(acceleration_bps, 3) if acceleration_bps is not None else None,
            "z5m": round(z5m, 3) if z5m is not None else None,
            "windows": windows,
            "sampleCount": sample_count, "ageMs": age_ms,
            "status": "live" if has_live else "history" if latest else "warming",
        }
        if metadata["key"] in {"DXY", "EURUSD", "SP500", "NASDAQ100", "WTI", "GOLD", "COPPER", "EWZ", "DRAM", "SOXL"} and len(recent) >= 2:
            baseline = recent[0]["price"]
            chart_series.append({
                "key": metadata["key"], "label": metadata["label"], "color": metadata["color"],
                "values": [{"time": point["time"], "value": round((point["price"] / baseline - 1) * 100, 4)} for point in recent],
            })

    def signal(key: str, invert: bool = False) -> float | None:
        z_value = measures.get(key, {}).get("z5m")
        reverse = invert or next((item.get("riskDirection") == "inverse" for item in MACRO_ASSETS.values() if item["key"] == key), False)
        return math.tanh((-z_value if reverse else z_value) / 1.8) if z_value is not None else None

    def mean_available(parts: list[float | None]) -> float | None:
        values = [value for value in parts if value is not None]
        return sum(values) / len(values) if values else None

    usd_strength = mean_available([
        signal("DXY"), signal("USDJPY"), signal("EURUSD", True), signal("GBPUSD", True),
        signal("AUDUSD", True), signal("NZDUSD", True),
    ])
    equity_breadth = mean_available([signal("SP500"), signal("NASDAQ100"), signal("RUSSELL2000"), signal("DOW")])
    cyclicals = mean_available([signal("COPPER"), signal("GOLD", True)])
    # Keep Treasury yield direction separate from its risk interpretation. Rates
    # corroborate the cross-asset backdrop; rising yields alone are not risk-off.
    yield_move = mean_available([signal("UST2Y", True), signal("UST10Y", True), signal("UST30Y", True)])
    risk_context = mean_available([equity_breadth, -usd_strength if usd_strength is not None else None, cyclicals])
    rates_confirmation = risk_context * abs(yield_move) if risk_context is not None and yield_move is not None else None
    components = [
        {"key": "equities", "label": "Índices", "value": round(equity_breadth * 100) if equity_breadth is not None else None, "weight": 0.40},
        {"key": "usd", "label": "Dólar", "value": round(-usd_strength * 100) if usd_strength is not None else None, "weight": 0.30},
        {"key": "cyclicals", "label": "Cíclicos / ouro", "value": round(cyclicals * 100) if cyclicals is not None else None, "weight": 0.15},
        {"key": "rates", "label": "Treasuries · contexto", "value": round(rates_confirmation * 100) if rates_confirmation is not None else None, "weight": 0.15},
    ]
    active_components = [item for item in components if item["value"] is not None]
    risk_score = round(sum(item["value"] * item["weight"] for item in active_components) / sum(item["weight"] for item in active_components)) if active_components else None
    coverage = round(sum(item["weight"] for item in active_components) * 100)
    cross_asset_confirmation = equity_breadth is not None and (usd_strength is not None or cyclicals is not None)
    sufficient_coverage = coverage >= 55
    regime = "RISK-ON" if sufficient_coverage and cross_asset_confirmation and risk_score is not None and risk_score >= 20 else "RISK-OFF" if sufficient_coverage and cross_asset_confirmation and risk_score is not None and risk_score <= -20 else "MISTO" if sufficient_coverage and cross_asset_confirmation and risk_score is not None else "AQUECENDO"
    regime_qualified = sufficient_coverage and cross_asset_confirmation and risk_score is not None

    def build_breadth(keys: list[str], inverted: set[str] | None = None) -> dict[str, Any]:
        inverted = inverted or set()
        values = [(-signal(key) if key in inverted and signal(key) is not None else signal(key)) for key in keys]
        active_values = [value for value in values if value is not None]
        advancing = sum(value >= 0.15 for value in active_values)
        declining = sum(value <= -0.15 for value in active_values)
        neutral = len(active_values) - advancing - declining
        score = round(mean_available(active_values) * 100) if active_values else None
        return {
            "advancing": advancing, "neutral": neutral, "declining": declining,
            "available": len(active_values), "total": len(keys), "score": score,
            "coverage": round(len(active_values) / len(keys) * 100) if keys else 0,
        }

    breadth = {
        "indices": {"label": "Índices amplos", **build_breadth(["SP500", "NASDAQ100", "RUSSELL2000", "DOW"])},
        "usd": {"label": "Força do USD", **build_breadth(["DXY", "USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"], {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"})},
        "energy": {"label": "Energia", **build_breadth(["WTI", "BRENT"])},
        "metals": {"label": "Metais", **build_breadth(["GOLD", "COPPER"])},
        "yields": {"label": "Treasury yields", **build_breadth(["UST2Y", "UST10Y", "UST30Y"])},
    }
    risk_votes = {
        "Índices": equity_breadth,
        "USD": -usd_strength if usd_strength is not None else None,
        "Cíclicos / ouro": cyclicals,
    }
    on_classes = [name for name, value in risk_votes.items() if value is not None and value >= 0.2]
    off_classes = [name for name, value in risk_votes.items() if value is not None and value <= -0.2]
    breadth["confirmation"] = {
        "direction": "RISK-ON" if len(on_classes) >= 2 else "RISK-OFF" if len(off_classes) >= 2 else "MISTA",
        "classes": on_classes if len(on_classes) >= 2 else off_classes if len(off_classes) >= 2 else [],
        "votes": {name: round(value * 100) if value is not None else None for name, value in risk_votes.items()},
    }

    divergence_specs = [
        ("NASDAQ100", "SP500", "Nasdaq × S&P 500", 1),
        ("RUSSELL2000", "SP500", "Russell 2000 × S&P 500", 1),
        ("DXY", "EURUSD", "DXY × EUR/USD", -1),
        ("DXY", "GOLD", "DXY × ouro", -1),
    ]
    divergences = []
    for left_key, right_key, label, expected_sign in divergence_specs:
        left_z, right_z = measures.get(left_key, {}).get("z5m"), measures.get(right_key, {}).get("z5m")
        if left_z is None or right_z is None or abs(left_z) < 0.75 or abs(right_z) < 0.75:
            continue
        violates_relation = left_z * right_z < 0 if expected_sign > 0 else left_z * right_z > 0
        if violates_relation:
            divergences.append({
                "id": f"{left_key}:{right_key}", "leftKey": left_key, "rightKey": right_key,
                "label": label, "leftZ5m": left_z, "rightZ5m": right_z,
                "strength": round(min(abs(left_z), abs(right_z)), 2),
                "note": "Movimentos de curto prazo em desacordo com a relação observada; não implica reversão.",
            })
    usd_state = "USD COMPRADO" if usd_strength is not None and usd_strength >= 0.2 else "USD VENDIDO" if usd_strength is not None and usd_strength <= -0.2 else "USD MISTO" if usd_strength is not None else "SEM HISTÓRICO"
    oil_pressure = mean_available([signal("WTI"), signal("BRENT")])
    oil_state = "ACELERANDO" if oil_pressure is not None and oil_pressure >= 0.2 else "ALIVIANDO" if oil_pressure is not None and oil_pressure <= -0.2 else "SEM PRESSÃO DIRECIONAL" if oil_pressure is not None else "AGUARDANDO DADOS"

    outlook = []
    if equity_breadth is not None:
        if equity_breadth >= 0.15 and (usd_strength is None or usd_strength <= 0.15):
            index_bias, index_title = "favorable", "Índices EUA · suporte observado"
        elif equity_breadth <= -0.15 or (usd_strength is not None and usd_strength >= 0.3):
            index_bias, index_title = "caution", "Índices EUA · pressão observada"
        else:
            index_bias, index_title = "watch", "Índices EUA · sinais mistos"
        index_detail = "S&P 500, NASDAQ e Russell confirmam movimento pró-risco." if index_bias == "favorable" else "Dólar firme ou breadth negativo pode limitar índices amplos." if index_bias == "caution" else "Confirme direção entre os índices antes de assumir continuidade."
        if oil_pressure is not None and oil_pressure >= 0.25:
            index_detail += " Petróleo acelerando adiciona risco de custo/inflação."
        outlook.append({"group": "EQUITIES", "bias": index_bias, "title": index_title, "detail": index_detail})
    if usd_strength is not None:
        fx_bias = "favorable" if usd_strength <= -0.2 else "caution" if usd_strength >= 0.2 else "watch"
        fx_title = "EUR/USD · AUD/USD · GBP/USD" if fx_bias == "favorable" else "DXY e pares USD" if fx_bias == "caution" else "Pares cambiais · sem dominância clara"
        fx_detail = "USD enfraquecendo favorece relativamente os pares em que o dólar é a moeda cotada." if fx_bias == "favorable" else "USD fortalecendo favorece relativamente o dólar frente à cesta observada." if fx_bias == "caution" else "A cesta de dólar ainda não apresenta direção dominante."
        outlook.append({"group": "FX", "bias": fx_bias, "title": fx_title, "detail": fx_detail})
    if oil_pressure is not None:
        oil_bias = "favorable" if oil_pressure >= 0.2 else "caution" if oil_pressure <= -0.2 else "watch"
        oil_title = "WTI / Brent · aceleração de preço" if oil_bias == "favorable" else "WTI / Brent · momentum negativo" if oil_bias == "caution" else "Energia · sem aceleração clara"
        oil_detail = "Energia em alta; pode beneficiar a própria commodity e elevar custos para setores consumidores." if oil_bias == "favorable" else "Energia em queda; alivia custos no curto prazo, mas reflita a fraqueza da demanda." if oil_bias == "caution" else "Sem direção suficiente para inferir pressão de custo."
        outlook.append({"group": "ENERGY", "bias": oil_bias, "title": oil_title, "detail": oil_detail})
    gold_signal = signal("GOLD")
    if gold_signal is not None:
        gold_bias = "favorable" if gold_signal >= 0.2 else "caution" if gold_signal <= -0.2 else "watch"
        gold_title = "Ouro com momentum positivo" if gold_bias == "favorable" else "Ouro perdendo força" if gold_bias == "caution" else "Ouro sem direção clara"
        outlook.append({"group": "METALS", "bias": gold_bias, "title": gold_title, "detail": "Ouro é proxy defensiva, mas também responde a dólar e juros; não confirma aversão a risco isoladamente."})

    insights: list[dict[str, str]] = []
    if usd_state == "USD COMPRADO":
        insights.append({"tone": "risk", "title": "Pressão compradora no dólar", "detail": "DXY e pares cambiais disponíveis apontam fortalecimento relativo do USD; índices e commodities cotadas em dólar podem sentir pressão."})
    elif usd_state == "USD VENDIDO":
        insights.append({"tone": "positive", "title": "Pressão vendedora no dólar", "detail": "A cesta cambial sugere enfraquecimento relativo do USD; EUR/USD e ativos cotados em dólar podem encontrar suporte."})
    if regime == "RISK-ON":
        insights.append({"tone": "positive", "title": "Confluência pró-risco", "detail": "Índices e proxies cambiais/cíclicos disponíveis favorecem um viés de risco positivo no horizonte curto."})
    elif regime == "RISK-OFF":
        insights.append({"tone": "risk", "title": "Confluência defensiva", "detail": "A cesta curta favorece redução de risco; monitore índices, força do dólar e ouro em conjunto."})
    if oil_state == "ACELERANDO":
        insights.append({"tone": "warning", "title": "Petróleo acelera", "detail": "Brent/WTI adicionam pressão de custos e inflação; isso pode pesar sobre índices amplos, embora a reação varie por setor e sessão."})
    elif oil_state == "ALIVIANDO":
        insights.append({"tone": "positive", "title": "Petróleo perde força", "detail": "A pressão recente de energia está aliviando; observe se os índices confirmam esse movimento."})
    if yield_move is not None:
        yield_direction = "subindo" if yield_move >= 0.2 else "caindo" if yield_move <= -0.2 else "sem direção clara"
        rates_tone = "positive" if rates_confirmation is not None and rates_confirmation >= 0.2 else "warning" if rates_confirmation is not None and rates_confirmation <= -0.2 else "neutral"
        rates_context_label = "reforça o viés pró-risco" if rates_tone == "positive" else "reforça o viés defensivo" if rates_tone == "warning" else "sem confirmação cruzada"
        insights.append({"tone": rates_tone, "title": f"Treasuries: yields {yield_direction} · {rates_context_label}", "detail": "Yields de 2Y/10Y/30Y são lidos junto com índices, dólar e cíclicos. O movimento de juros sozinho não define Risk-On ou Risk-Off."})
    if not insights:
        insights.append({"tone": "neutral", "title": "Sinais macro sem confluência", "detail": "Os fatores disponíveis estão mistos ou ainda não têm histórico sincronizado suficiente para uma leitura direcional."})

    accelerators = sorted(
        [item for item in measures.values() if item.get("acceleration5mPp") is not None or item.get("acceleration5mBps") is not None],
        key=lambda item: abs(item.get("z5m") or 0), reverse=True,
    )[:6]
    usable_samples = sum(1 for item in measures.values() if item.get("sampleCount", 0) > 0)
    return {
        "asOf": now_ms, "source": "BingX TradFi + Hyperliquid Spot/HIP-3 · cotações ao vivo; amostras alinhadas de 1 minuto",
        "storage": {"engine": storage, "durable": durable, "retentionDays": 7, "oldestSample": oldest_sample, "trackedAssets": len(MACRO_ASSETS), "assetsWithHistory": usable_samples},
        "status": "live" if live_count else "history" if usable_samples else "warming",
        "regime": {"label": regime, "candidate": regime, "qualified": regime_qualified, "score": risk_score, "coverage": coverage, "components": components},
        "breadth": breadth,
        "usd": {"state": usd_state, "strengthScore": round(usd_strength * 100) if usd_strength is not None else None},
        "oil": {"state": oil_state, "pressureScore": round(oil_pressure * 100) if oil_pressure is not None else None},
        "assets": list(measures.values()), "accelerators": accelerators, "outlook": outlook,
        "yieldCurve": _build_yield_curve(histories, live, now_ms),
        # Preserve the removed matrix response shape during independent web/API deploys.
        # Older browser bundles can render empty cells instead of crashing on missing fields.
        "relationships": [],
        "correlationMatrix": {
            "assets": [],
            "windows": {str(minutes): {"values": [], "observations": []} for minutes in (15, 30, 60)},
            "correlationShifts": [],
        },
        "correlationShifts": [],
        "divergences": divergences, "insights": insights, "chart": {"windowMinutes": 60, "series": chart_series},
        "method": "Variação, velocidade e aceleração visíveis em 1/5/15/60 min; Z-score de cada janela usa desvio-padrão dos retornos observados de 1 min na última hora. Score Risk-On/Off usa Z-score de 5 min com pesos: índices 40%, dólar 30%, cíclicos/ouro 15% e Treasuries 15%. O fator Treasury corrobora o contexto cruzado e yields sozinhos não definem regime. O regime usa histerese, limiares distintos de entrada/saída e 3 buckets consecutivos de 1 min para confirmação. Breadth classifica cada fator pelo Z-score e mostra cobertura observada. Divergência exige Z-score de 5 min acima de 0,75σ nos dois ativos e persistência por 2 amostras.",
        "limitations": ["Pressão em moedas é inferida por preços; não mede fluxo financeiro real nem posições de participantes.", "Scores, breadth e divergências descrevem associação de curto prazo, não causalidade nem recomendação.", "Alertas e preferências ficam no navegador do usuário; notificações externas/email e sincronização entre dispositivos não estão configuradas.", "Histerese e persistência são mantidas em memória no serviço e recomeçam após reinício/deploy.", "Contratos TradFi BingX e HIP-3 da Hyperliquid não equivalem a preços oficiais à vista ou futuros de bolsa; o oráculo e as sessões dependem do deployer.", "WBRL/USDC é um token de real empacotado negociado contra USDC; o par tem liquidez reduzida e não equivale a uma cotação oficial ou direta de BRL/USDT.", "2Y/10Y/30Y representam yields (não preços dos títulos); TLT, EWZ, DRAM e SOXL são contratos sintéticos perpétuos e podem divergir dos ativos subjacentes.", "Histórico de 1 min para Hyperliquid começa na ativação da coleta; o painel aquece até acumular amostras sincronizadas suficientes."],
    }


def _build_yield_curve(histories: dict[str, list[dict[str, Any]]], live: dict[str, dict[str, Any]], now_ms: int) -> dict[str, Any]:
    def spread(long_symbol: str, short_symbol: str) -> dict[str, Any]:
        long_points = {point["time"]: point["price"] for point in histories.get(long_symbol, [])}
        short_points = {point["time"]: point["price"] for point in histories.get(short_symbol, [])}
        common = sorted(set(long_points) & set(short_points))
        long_live, short_live = live.get(long_symbol), live.get(short_symbol)
        both_live = bool(long_live and short_live and now_ms - int(long_live.get("receivedAt", 0)) <= 90_000 and now_ms - int(short_live.get("receivedAt", 0)) <= 90_000)
        current = (float(long_live["price"]) - float(short_live["price"])) * 100 if both_live else (long_points[common[-1]] - short_points[common[-1]]) * 100 if common else None
        prior_time = next((timestamp for timestamp in reversed(common) if timestamp <= now_ms - 5 * 60_000), None)
        prior = (long_points[prior_time] - short_points[prior_time]) * 100 if prior_time is not None else None
        return {"spreadBps": round(current, 2) if current is not None else None, "change5mBps": round(current - prior, 2) if current is not None and prior is not None else None, "status": "live" if current is not None and both_live else "history" if current is not None else "warming"}

    two_ten = spread("HL:para:10Y", "HL:para:2Y")
    ten_thirty = spread("HL:para:30Y", "HL:para:10Y")
    live_count = sum(1 for symbol in ("HL:para:2Y", "HL:para:10Y", "HL:para:30Y") if live.get(symbol) and now_ms - int(live[symbol].get("receivedAt", 0)) <= 90_000)
    has_history = any(histories.get(symbol) for symbol in ("HL:para:2Y", "HL:para:10Y", "HL:para:30Y"))
    return {
        "spreadBps": two_ten["spreadBps"],
        "change5mBps": two_ten["change5mBps"],
        "longSpreadBps": ten_thirty["spreadBps"],
        "longChange5mBps": ten_thirty["change5mBps"],
        "status": "live" if live_count == 3 else "history" if has_history else "warming",
        "label": "Inclinação 10Y−2Y e 30Y−10Y",
    }
