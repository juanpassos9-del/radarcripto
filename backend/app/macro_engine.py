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
    "HL:xyz:EWZ": {"key": "EWZ", "label": "EWZ sintético", "category": "equity", "color": "#57d69a", "source": "Hyperliquid · XYZ"},
    "HL:xyz:TLT": {"key": "TLT", "label": "TLT sintético", "category": "bond", "color": "#e3bd65", "source": "Hyperliquid · XYZ"},
}


def _bounded(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _near_price(points: list[dict[str, Any]], target_ms: int, tolerance_ms: int = 120_000) -> dict[str, Any] | None:
    match = next((point for point in reversed(points) if point["time"] <= target_ms), None)
    return match if match and target_ms - match["time"] <= tolerance_ms else None


def _pearson(left: list[float], right: list[float]) -> float | None:
    count = min(len(left), len(right))
    if count < 6:
        return None
    left, right = left[-count:], right[-count:]
    mean_left, mean_right = sum(left) / count, sum(right) / count
    covariance = sum((x - mean_left) * (y - mean_right) for x, y in zip(left, right))
    variance_left = sum((x - mean_left) ** 2 for x in left)
    variance_right = sum((y - mean_right) ** 2 for y in right)
    denominator = math.sqrt(variance_left * variance_right)
    return _bounded(covariance / denominator) if denominator else None


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
            measures[metadata["key"]] = {"symbol": symbol, **metadata, "available": False, "price": None, "change5mPct": None, "change30mPct": None, "acceleration5mPp": None, "z5m": None, "sampleCount": 0, "ageMs": None, "status": "unavailable"}
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
        at_5m = _near_price(points, now_ms - 5 * 60_000)
        at_10m = _near_price(points, now_ms - 10 * 60_000)
        at_30m = _near_price(points, now_ms - 30 * 60_000)
        change_5m = (latest["price"] / at_5m["price"] - 1) * 100 if latest and at_5m else None
        prior_5m = (at_5m["price"] / at_10m["price"] - 1) * 100 if at_5m and at_10m else None
        change_30m = (latest["price"] / at_30m["price"] - 1) * 100 if latest and at_30m else None
        acceleration = change_5m - prior_5m if change_5m is not None and prior_5m is not None else None
        is_yield = metadata.get("unit") == "yield_pct"
        change_5m_bps = (latest["price"] - at_5m["price"]) * 100 if is_yield and latest and at_5m else None
        change_30m_bps = (latest["price"] - at_30m["price"]) * 100 if is_yield and latest and at_30m else None
        prior_5m_bps = (at_5m["price"] - at_10m["price"]) * 100 if is_yield and at_5m and at_10m else None
        acceleration_bps = change_5m_bps - prior_5m_bps if change_5m_bps is not None and prior_5m_bps is not None else None
        recent = [point for point in points if now_ms - 60 * 60_000 <= point["time"] <= now_ms]
        one_minute_returns = [((right["price"] - left["price"]) * 100 if is_yield else (right["price"] / left["price"] - 1) * 100) for left, right in zip(recent, recent[1:]) if left["price"] > 0]
        z5m = None
        normalized_change_5m = change_5m_bps if is_yield else change_5m
        if normalized_change_5m is not None and len(one_minute_returns) >= 12:
            mean = sum(one_minute_returns) / len(one_minute_returns)
            variance = sum((value - mean) ** 2 for value in one_minute_returns) / len(one_minute_returns)
            sigma_5m = math.sqrt(variance * 5)
            if sigma_5m > 1e-8:
                z5m = normalized_change_5m / sigma_5m
        if first:
            oldest_sample = first["time"] if oldest_sample is None else min(oldest_sample, first["time"])
        measures[metadata["key"]] = {
            "symbol": symbol, **metadata, "available": True,
            "price": latest["price"] if latest else None,
            "change5mPct": round(change_5m, 4) if change_5m is not None else None,
            "change30mPct": round(change_30m, 4) if change_30m is not None else None,
            "acceleration5mPp": round(acceleration, 4) if acceleration is not None else None,
            "change5mBps": round(change_5m_bps, 3) if change_5m_bps is not None else None,
            "change30mBps": round(change_30m_bps, 3) if change_30m_bps is not None else None,
            "acceleration5mBps": round(acceleration_bps, 3) if acceleration_bps is not None else None,
            "z5m": round(z5m, 3) if z5m is not None else None,
            "sampleCount": sample_count, "ageMs": age_ms,
            "status": "live" if has_live else "history" if latest else "warming",
        }
        if metadata["key"] in {"DXY", "EURUSD", "SP500", "NASDAQ100", "WTI", "GOLD", "COPPER", "EWZ", "TLT"} and len(recent) >= 2:
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
    rates_signal = mean_available([signal("UST2Y"), signal("UST10Y")])
    components = [
        {"key": "equities", "label": "Índices", "value": round(equity_breadth * 100) if equity_breadth is not None else None, "weight": 0.40},
        {"key": "usd", "label": "Dólar", "value": round(-usd_strength * 100) if usd_strength is not None else None, "weight": 0.30},
        {"key": "cyclicals", "label": "Cíclicos / ouro", "value": round(cyclicals * 100) if cyclicals is not None else None, "weight": 0.15},
        {"key": "rates", "label": "Juros EUA", "value": round(rates_signal * 100) if rates_signal is not None else None, "weight": 0.15},
    ]
    active_components = [item for item in components if item["value"] is not None]
    risk_score = round(sum(item["value"] * item["weight"] for item in active_components) / sum(item["weight"] for item in active_components)) if active_components else None
    coverage = round(sum(item["weight"] for item in active_components) * 100)
    regime = "RISK-ON" if risk_score is not None and risk_score >= 20 else "RISK-OFF" if risk_score is not None and risk_score <= -20 else "MISTO" if risk_score is not None else "AQUECENDO"
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

    def returns_for(key: str, target: list[int]) -> dict[int, float]:
        points = histories.get(measures.get(key, {}).get("symbol", ""), [])
        indexed = {point["time"]: point["price"] for point in points}
        result: dict[int, float] = {}
        for timestamp in target:
            current = indexed.get(timestamp)
            previous = indexed.get(timestamp - 60_000)
            if current and previous:
                result[timestamp] = (current - previous) * 100 if measures.get(key, {}).get("unit") == "yield_pct" else (current / previous - 1) * 100
        return result

    relationships = []
    for left, right, label in [("DXY", "EURUSD", "Dólar × EUR/USD"), ("DXY", "SP500", "Dólar × S&P 500"), ("WTI", "SP500", "Petróleo × S&P 500"), ("GOLD", "DXY", "Ouro × dólar"), ("UST2Y", "SP500", "Treasury 2Y × S&P 500"), ("UST10Y", "TLT", "Treasury 10Y × TLT"), ("DXY", "EWZ", "Dólar × EWZ")]:
        left_times = histories.get(measures.get(left, {}).get("symbol", ""), [])
        right_times = histories.get(measures.get(right, {}).get("symbol", ""), [])
        shared = sorted({point["time"] for point in left_times} & {point["time"] for point in right_times})[-31:]
        left_returns, right_returns = returns_for(left, shared), returns_for(right, shared)
        aligned = sorted(set(left_returns) & set(right_returns))
        fresh = bool(aligned and now_ms - aligned[-1] <= 3 * 60_000)
        corr = _pearson([left_returns[timestamp] for timestamp in aligned], [right_returns[timestamp] for timestamp in aligned]) if fresh else None
        relationships.append({"left": left, "right": right, "label": label, "correlation": round(corr, 3) if corr is not None else None, "observations": len(aligned), "windowMinutes": 30})

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
    if rates_signal is not None:
        rates_state = "juros pressionando risco" if rates_signal <= -0.2 else "juros aliviando" if rates_signal >= 0.2 else "juros sem direção clara"
        insights.append({"tone": "warning" if rates_signal <= -0.2 else "positive" if rates_signal >= 0.2 else "neutral", "title": f"Treasuries: {rates_state}", "detail": "Variação de yields de 2Y/10Y é convertida para pontos-base. A relação com ações depende do motivo do movimento (inflação, crescimento ou expectativa de Fed)."})
    if not insights:
        insights.append({"tone": "neutral", "title": "Sinais macro sem confluência", "detail": "Os fatores disponíveis estão mistos ou ainda não têm histórico sincronizado suficiente para uma leitura direcional."})

    accelerators = sorted(
        [item for item in measures.values() if item.get("acceleration5mPp") is not None or item.get("acceleration5mBps") is not None],
        key=lambda item: abs(item.get("acceleration5mBps") if item.get("acceleration5mBps") is not None else item["acceleration5mPp"]), reverse=True,
    )[:6]
    usable_samples = sum(1 for item in measures.values() if item.get("sampleCount", 0) > 0)
    return {
        "asOf": now_ms, "source": "BingX TradFi + Hyperliquid HIP-3 · cotações ao vivo; amostras alinhadas de 1 minuto",
        "storage": {"engine": storage, "durable": durable, "retentionDays": 7, "oldestSample": oldest_sample, "trackedAssets": len(MACRO_ASSETS), "assetsWithHistory": usable_samples},
        "status": "live" if live_count else "history" if usable_samples else "warming",
        "regime": {"label": regime, "score": risk_score, "coverage": coverage, "components": components},
        "usd": {"state": usd_state, "strengthScore": round(usd_strength * 100) if usd_strength is not None else None},
        "oil": {"state": oil_state, "pressureScore": round(oil_pressure * 100) if oil_pressure is not None else None},
        "assets": list(measures.values()), "accelerators": accelerators, "outlook": outlook,
        "yieldCurve": _build_yield_curve(histories, live, now_ms),
        "relationships": relationships, "insights": insights, "chart": {"windowMinutes": 60, "series": chart_series},
        "method": "Score composto de variações de 5 min normalizadas pela volatilidade de 1 min; pesos: índices 40%, dólar 30%, cíclicos/ouro 15% e yields EUA 15%. Yields são medidos em pontos-base; o sinal de yields crescentes é tratado como possível pressão sobre risco, sem inferir causalidade. Correlações Pearson usam amostras de 1 min sincronizadas em até 30 min.",
        "limitations": ["Pressão em moedas é inferida por preços; não mede fluxo financeiro real nem posições de participantes.", "Os scores descrevem associação de curto prazo, não causalidade nem recomendação.", "Contratos TradFi BingX e HIP-3 da Hyperliquid não equivalem a preços oficiais à vista ou futuros de bolsa; o oráculo e as sessões dependem do deployer.", "2Y/10Y representam yields (não preço dos títulos); TLT e EWZ são contratos sintéticos perpétuos e podem divergir dos ETFs subjacentes.", "Histórico de 1 min para Hyperliquid começa na ativação da coleta; o painel aquece até acumular amostras sincronizadas suficientes."],
    }


def _build_yield_curve(histories: dict[str, list[dict[str, Any]]], live: dict[str, dict[str, Any]], now_ms: int) -> dict[str, Any]:
    two_symbol, ten_symbol = "HL:para:2Y", "HL:para:10Y"
    two_points = {point["time"]: point["price"] for point in histories.get(two_symbol, [])}
    ten_points = {point["time"]: point["price"] for point in histories.get(ten_symbol, [])}
    common = sorted(set(two_points) & set(ten_points))
    two_live, ten_live = live.get(two_symbol), live.get(ten_symbol)
    both_live = bool(two_live and ten_live and now_ms - int(two_live.get("receivedAt", 0)) <= 90_000 and now_ms - int(ten_live.get("receivedAt", 0)) <= 90_000)
    current_two = two_live["price"] if both_live else (two_points[common[-1]] if common else None)
    current_ten = ten_live["price"] if both_live else (ten_points[common[-1]] if common else None)
    previous_time = next((timestamp for timestamp in reversed(common) if timestamp <= now_ms - 5 * 60_000), None)
    current_spread = (float(current_ten) - float(current_two)) * 100 if current_ten is not None and current_two is not None else None
    prior_spread = (ten_points[previous_time] - two_points[previous_time]) * 100 if previous_time is not None else None
    live_spread = bool(current_spread is not None and both_live)
    return {"spreadBps": round(current_spread, 2) if current_spread is not None else None, "change5mBps": round(current_spread - prior_spread, 2) if current_spread is not None and prior_spread is not None else None, "status": "live" if live_spread else "history" if current_spread is not None else "warming", "label": "Inclinação 10Y−2Y"}
