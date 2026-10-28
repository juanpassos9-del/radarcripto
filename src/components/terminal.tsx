"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, CircleHelp, Crosshair, Database, Maximize2, Radio, Search, Settings2, Wifi } from "lucide-react";
import MarketChart from "@/components/market-chart";
import { fmtCompact, fmtPrice, fmtTime, getJson, type BookData, type Candle, type ChartOverlays, type MarketSnapshot, type ReversalSignalData, type Trade, type VolatilityZones } from "@/lib/market";

const assets = [
  "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
  "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
  "LTCUSDT", "BCHUSDT", "SUIUSDT", "TRXUSDT", "NEARUSDT",
  "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "FILUSDT",
  "UNIUSDT", "ETCUSDT", "ATOMUSDT", "AAVEUSDT", "WIFUSDT",
];
const intervals = ["1m", "5m", "15m", "1h", "4h", "1d"];
const nameOf = (symbol: string) => symbol.replace("USDT", "");

export default function Terminal() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [interval, setInterval] = useState("5m");
  const [market] = useState<"futures">("futures");
  const selectedKeyRef = useRef("");
  selectedKeyRef.current = `${symbol}|${interval}|${market}`;
  const [markets, setMarkets] = useState<Record<string, MarketSnapshot>>({});
  const [candles, setCandles] = useState<Candle[]>([]);
  const [book, setBook] = useState<BookData | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [reversalData, setReversalData] = useState<ReversalSignalData | null>(null);
  const [reversalError, setReversalError] = useState(false);
  const [buyEnabled, setBuyEnabled] = useState(true);
  const [sellEnabled, setSellEnabled] = useState(true);
  const [garchSignalEnabled, setGarchSignalEnabled] = useState(true);
  const [hvSignalEnabled, setHvSignalEnabled] = useState(true);
  const [volumeFilterEnabled, setVolumeFilterEnabled] = useState(true);
  const [minSigma, setMinSigma] = useState(1.0);
  const [touchTolerancePct, setTouchTolerancePct] = useState(0.1);
  const [minReversalScore, setMinReversalScore] = useState(50);
  const [cooldownCandles, setCooldownCandles] = useState(10);
  const reversalRequestRef = useRef(0);
  const [volatility, setVolatility] = useState<VolatilityZones | null>(null);
  const [showGarchZones, setShowGarchZones] = useState(true);
  const [showHvZones, setShowHvZones] = useState(true);
  const [showVolumeAnomalies, setShowVolumeAnomalies] = useState(true);
  const [chartOverlays, setChartOverlays] = useState<ChartOverlays | null>(null);
  const [showAnchoredVwaps, setShowAnchoredVwaps] = useState(true);
  const [showPreviousLevels, setShowPreviousLevels] = useState(true);
  const [fear, setFear] = useState<{ value: number | null; label: string; status: string; source: string } | null>(null);
  const [funding, setFunding] = useState<{ rate: number | null; markPrice: number | null; indexPrice: number | null; openInterest: number | null; nextFundingTime: number | null; status: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [clock, setClock] = useState<number | null>(null);
  const [apiState, setApiState] = useState<"checking" | "connected" | "error">("checking");
  const [apiMessage, setApiMessage] = useState("");
  const [alertsOn, setAlertsOn] = useState(false);

  useEffect(() => {
    const updateClock = () => setClock(Date.now());
    updateClock();
    const timer = window.setInterval(updateClock, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const loadMarkets = useCallback(async () => {
    try {
      const data = await getJson<{ markets: MarketSnapshot[] }>(`/api/markets?market=${market}`);
      setMarkets(Object.fromEntries(data.markets.map(item => [item.symbol, item])));
      setApiState("connected"); setApiMessage("");
    } catch (error) { setApiState("error"); setApiMessage(error instanceof Error ? error.message : "API local indisponível"); }
  }, [market]);

  const loadAsset = useCallback(async (isFirst = false) => {
    const requestKey = `${symbol}|${interval}|${market}`;
    const [candleResult, bookResult, tradeResult] = await Promise.allSettled([
      getJson<{ candles: Candle[] }>(`/api/candles?symbol=${symbol}&interval=${interval}&market=${market}&limit=240`),
      getJson<BookData>(`/api/book?symbol=${symbol}&market=${market}&limit=16`),
      getJson<{ trades: Trade[] }>(`/api/trades?symbol=${symbol}&market=${market}&limit=24`),
    ]);
    if (selectedKeyRef.current !== requestKey) return;
    if (candleResult.status === "fulfilled") setCandles(candleResult.value.candles);
    else setApiMessage(`Candles: ${candleResult.reason instanceof Error ? candleResult.reason.message : "indisponíveis"}`);
    if (bookResult.status === "fulfilled") setBook(bookResult.value);
    else setBook(null);
    if (tradeResult.status === "fulfilled") {
      setTrades(tradeResult.value.trades);
    }
    if (isFirst) setLoading(false);
  }, [symbol, interval, market]);

  const loadReversalSignals = useCallback(async () => {
    const requestId = ++reversalRequestRef.current;
    const params = new URLSearchParams({
      symbol, interval, market,
      min_sigma: String(minSigma), touch_tolerance: String(touchTolerancePct / 100),
      min_score: String(minReversalScore), cooldown: String(cooldownCandles), buy_enabled: String(buyEnabled), sell_enabled: String(sellEnabled),
      garch_enabled: String(garchSignalEnabled), hv_enabled: String(hvSignalEnabled), volume_enabled: String(volumeFilterEnabled),
    });
    try {
      const data = await getJson<ReversalSignalData>(`/api/reversal-signals?${params}`);
      if (requestId === reversalRequestRef.current) { setReversalData(data); setReversalError(false); }
    } catch {
      if (requestId === reversalRequestRef.current) { setReversalData(null); setReversalError(true); }
    }
  }, [symbol, interval, market, minSigma, touchTolerancePct, minReversalScore, cooldownCandles, buyEnabled, sellEnabled, garchSignalEnabled, hvSignalEnabled, volumeFilterEnabled]);

  useEffect(() => {
    setReversalData(null);
    void loadReversalSignals();
  }, [loadReversalSignals]);

  useEffect(() => {
    let active = true;
    setVolatility(null);
    getJson<VolatilityZones>(`/api/volatility?symbol=${symbol}&market=${market}`)
      .then(value => { if (active) setVolatility(value); })
      .catch(() => { if (active) setVolatility({ symbol, status: "unavailable" }); });
    return () => { active = false; };
  }, [symbol, market]);

  useEffect(() => {
    let active = true;
    setChartOverlays(null);
    const refresh = () => getJson<ChartOverlays>(`/api/chart-overlays?symbol=${symbol}&interval=${interval}&market=${market}`)
      .then(value => { if (active) setChartOverlays(value); })
      .catch(() => { if (active) setChartOverlays(null); });
    void refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol, interval, market]);

  useEffect(() => {
    loadMarkets();
    getJson<{ value: number | null; label: string; status: string; source: string }>("/api/sentiment").then(setFear).catch(() => setFear({ value: null, label: "Indisponível", status: "unavailable", source: "Alternative.me" }));
    const id = window.setInterval(loadMarkets, 15000);
    return () => window.clearInterval(id);
  }, [loadMarkets]);

  useEffect(() => {
    let active = true;
    let polling = false;
    setLoading(true);
    setCandles([]); setBook(null); setTrades([]);
    setReversalData(null);
    loadAsset(true);
    const marketApiUrl = process.env.NEXT_PUBLIC_MARKET_API_URL;
    const streamUrl = marketApiUrl
      ? `${marketApiUrl.replace(/^http:/, "ws:").replace(/^https:/, "wss:").replace(/\/+$/, "")}/ws/market`
      : process.env.NEXT_PUBLIC_MARKET_WS_URL || "ws://127.0.0.1:8001/ws/market";
    let socket: WebSocket | null = null; let poll: number | null = null;
    const connect = () => {
      if (!active) return;
      socket = new WebSocket(`${streamUrl}?symbol=${symbol}&market=${market}&interval=${interval}`);
      socket.onmessage = (event) => {
        if (!active || selectedKeyRef.current !== `${symbol}|${interval}|${market}`) return;
        try {
            const message = JSON.parse(event.data) as { type: string; data: unknown; closed?: boolean };
          if (message.type === "trade") {
            const trade = message.data as Trade;
            setTrades(current => [trade, ...current.filter(t => t.id !== trade.id)].slice(0, 60));
          } else if (message.type === "book") setBook(message.data as BookData);
          else if (message.type === "market") {
            const snapshot = message.data as MarketSnapshot;
            setMarkets(current => ({ ...current, [snapshot.symbol]: snapshot }));
          } else if (message.type === "candle") {
            const candle = message.data as Candle;
            setCandles(current => { const next = [...current]; const i = next.findIndex(c => c.time === candle.time); if (i >= 0) next[i] = candle; else next.push(candle); return next.slice(-260); });
            if (message.closed) void loadReversalSignals();
          }
        } catch { /* invalid event is ignored; REST refresh continues */ }
      };
      socket.onopen = () => { if (poll) { window.clearInterval(poll); poll = null; } };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (!active || poll) return;
        poll = window.setInterval(() => {
          if (!active || polling) return;
          polling = true;
          void loadAsset().finally(() => { polling = false; });
        }, 3000);
      };
    };
    connect();
    return () => { active = false; socket?.close(); if (poll) window.clearInterval(poll); };
  }, [symbol, interval, market, loadAsset, loadReversalSignals]);

  useEffect(() => {
    let live = true;
    const refresh = () => getJson<typeof funding>(`/api/derivatives?symbol=${symbol}`).then(data => { if (live) setFunding(data); }).catch(() => { if (live) setFunding(null); });
    refresh(); const timer = window.setInterval(refresh, 10000);
    return () => { live = false; window.clearInterval(timer); };
  }, [symbol, market]);

  const current = markets[symbol];
  const latestReversal = reversalData?.latest ?? null;
  const reversalCandidate = reversalData?.lastCandidate ?? null;
  const chartSignals = useMemo(() => {
    const candleTimes = new Set(candles.map(candle => candle.time));
    return (reversalData?.signals ?? []).filter(item => item.qualified && candleTimes.has(item.time));
  }, [candles, reversalData]);
  const sigmaDistance = current?.price != null && volatility?.reference != null && volatility.garch?.dailyVol ? Math.log(current.price / volatility.reference) / volatility.garch.dailyVol : null;
  const sigmaZone = sigmaDistance == null ? "Sem dados" : Math.abs(sigmaDistance) < 0.5 ? "Neutra" : Math.abs(sigmaDistance) < 1 ? "Moderada" : Math.abs(sigmaDistance) < 2 ? "Atenção" : Math.abs(sigmaDistance) < 3 ? "Extrema" : "Anomalia";
  const bestBid = book?.bids[0]?.price ?? null;
  const bestAsk = book?.asks[0]?.price ?? null;
  const cvd = useMemo(() => trades.reduce((sum, trade) => sum + (trade.side === "buy" ? 1 : -1) * trade.quantity, 0), [trades]);
  const buyVolume = useMemo(() => trades.filter(t => t.side === "buy").reduce((n,t) => n+t.quantity,0), [trades]);
  const sellVolume = useMemo(() => trades.filter(t => t.side === "sell").reduce((n,t) => n+t.quantity,0), [trades]);
  const coverage = Math.min(100, Math.round((trades.length / 24) * 100));
  const selectedFutures = true;
  const directionClass = (value: number | null | undefined) => value != null && value >= 0 ? "pos" : "neg";

  const createPriceAlert = async () => {
    if (current?.price == null) return;
    if ("Notification" in window && Notification.permission === "default") {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") return;
    }
    const thresholdText = window.prompt(`Preço de ${nameOf(symbol)} para alertar (ex.: ${current.price * 1.01})`);
    if (!thresholdText) return;
    const threshold = Number(thresholdText);
    if (!Number.isFinite(threshold) || threshold <= 0) { window.alert("Informe um preço válido."); return; }
    const key = `nexus-alert-${market}-${symbol}`;
    window.localStorage.setItem(key, JSON.stringify({ symbol, market, threshold, createdAt: Date.now() }));
    setAlertsOn(true);
    window.alert(`Alerta local criado para ${symbol} em ${fmtPrice(threshold)}.`);
  };

  useEffect(() => {
    if (!current?.price || !alertsOn || typeof window === "undefined") return;
    for (const asset of assets) {
      const m = markets[asset]; if (!m?.price) continue;
      const key = `nexus-alert-futures-${asset}`; const raw = window.localStorage.getItem(key); if (!raw) continue;
      try {
        const alert = JSON.parse(raw) as { threshold: number };
        if (asset === symbol && m.price >= alert.threshold) {
          if ("Notification" in window && Notification.permission === "granted") new Notification(`NEXUS Perp · ${asset}`, { body: `Preço ${fmtPrice(m.price)} atingiu ${fmtPrice(alert.threshold)}.` });
          window.localStorage.removeItem(key);
        }
      } catch { window.localStorage.removeItem(key); }
    }
  }, [markets, alertsOn, market, symbol, current?.price]);

  return <main className="shell">
    <header className="topbar">
      <div className="brand"><div className="logo">N</div><div><strong>NEXUS</strong><small>MARKET FLOW TERMINAL</small></div></div>
      <nav className="nav"><button className="active">Terminal</button><button onClick={() => document.getElementById("flow")?.scrollIntoView({ behavior: "smooth" })}>Fluxo</button><button onClick={() => document.getElementById("data")?.scrollIntoView({ behavior: "smooth" })}>Dados</button></nav>
      <div className="topright"><div className="live"><span className={`dot ${apiState === "error" ? "neg" : ""}`} />{apiState === "connected" ? "STREAM AO VIVO" : apiState === "error" ? "API OFFLINE" : "CONECTANDO"}</div><span className="exchange">BINANCE USDⓈ-M PERPETUAL</span><button className="iconbtn" title="Pesquisar ativo"><Search size={16}/></button><button className="iconbtn" title="Configurações"><Settings2 size={16}/></button></div>
    </header>

    <div className="wrap">
      <div className="heading"><div><div className="eyebrow">DERIVATIVES INTELLIGENCE / 01</div><h1>Perpetual Market Flow</h1></div><div className="headmeta"><span className="badge good"><Radio size={10} style={{ display: "inline", verticalAlign: "-2px" }}/> &nbsp;USDⓈ-M PERPETUAL</span><span className="badge">UTC−3 · SÃO PAULO</span><span className="badge">{clock == null ? "--:--:--" : fmtTime(clock)}</span></div></div>

      <section className="tickerbar">
        {["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"].map((asset, index) => {
          const m = markets[asset];
          return <div className="panel ticker" key={asset} style={{ textAlign: "left", borderColor: symbol === asset ? "#28776e" : undefined }}>
            <div className="ticktop"><span>{nameOf(asset)}/USDT</span><span>{index === 0 ? "●" : ""}</span></div>
            <div className="tickval mono">{fmtPrice(m?.price, m?.price && m.price < 10 ? 4 : 2)}</div>
            <div className={`tickdelta ${directionClass(m?.change24h)}`}>{m?.change24h == null ? "—" : `${m.change24h >= 0 ? "+" : ""}${m.change24h.toFixed(2)}%`}</div>
          </div>;
        })}
      </section>

      <div className="layout">
        <aside className="panel watchlist"><div className="panelhead"><span className="paneltitle">Watchlist</span><span className="badge">25 PERPÉTUOS</span></div>
          {assets.map((asset) => { const m = markets[asset]; const ticker = nameOf(asset); const icon = asset === "BTCUSDT" ? "₿" : asset === "ETHUSDT" ? "Ξ" : ticker.slice(0, 1); return <button key={asset} className={`watchrow ${symbol === asset ? "selected" : ""}`} onClick={() => setSymbol(asset)}>
            <div className="coinicon">{icon}</div><div><div className="coinname">{ticker} <span style={{ color: "#697687", fontWeight: 400 }}>/ USDT</span></div><div className="coinsub">Binance USDⓈ-M Perp</div></div>
            <div><div className="coinprice mono">{fmtPrice(m?.price, m?.price && m.price < 10 ? 4 : 2)}</div><div className={`change ${directionClass(m?.change24h)}`}>{m?.change24h == null ? "—" : `${m.change24h >= 0 ? "+" : ""}${m.change24h.toFixed(2)}%`}</div></div>
          </button>; })}
          <div className="watchfoot subtle">Ativos perpétuos USDⓈ-M · Binance</div>
        </aside>

        <section className="panel mainchart"><div className="assetbar"><div><h2>{nameOf(symbol)} <span style={{ color: "#7b8795", fontWeight: 400 }}>/ USDT</span></h2><span className="subtle">BINANCE · {selectedFutures ? "PERPETUAL" : "SPOT"}</span></div><span className="largeprice mono">{fmtPrice(current?.price, current?.price && current.price < 10 ? 4 : 2)}</span><span className={`asset-change ${directionClass(current?.change24h)}`}>{current?.change24h == null ? "—" : `${current.change24h >= 0 ? "+" : ""}${current.change24h.toFixed(2)}%`}</span><button className="iconbtn" title="Tela cheia" onClick={() => document.querySelector(".mainchart")?.requestFullscreen()}><Maximize2 size={14}/></button></div>
          <div className="charttools"><span className="subtle">TF</span>{intervals.map(tf => <button key={tf} className={interval === tf ? "active" : ""} onClick={() => setInterval(tf)}>{tf.toUpperCase()}</button>)}<button className={showGarchZones ? "active" : ""} onClick={() => setShowGarchZones(v => !v)}>GARCH</button><button className={showHvZones ? "active" : ""} onClick={() => setShowHvZones(v => !v)}>HV252</button><button className={`volume-toggle ${showAnchoredVwaps ? "active" : ""}`} title="Exibe VWAPs ancoradas no início do dia, semana e mês UTC" onClick={() => setShowAnchoredVwaps(v => !v)}>VWAP D/S/M</button><button className={`volume-toggle ${showPreviousLevels ? "active" : ""}`} title="Exibe máximas e mínimas do dia, semana e mês anteriores (UTC)" onClick={() => setShowPreviousLevels(v => !v)}>H/L ANT.</button><button className={`volume-toggle ${showVolumeAnomalies ? "active" : ""}`} title="Destaca candles com volume de pelo menos 1,5× a média dos 20 candles anteriores" onClick={() => setShowVolumeAnomalies(v => !v)}>VOL ≥1,5×</button><span style={{ marginLeft: "auto" }} className="subtle"><Crosshair size={12}/></span></div>
          <div className="chart-overlay-strip"><div className="chart-overlay-legend">{showAnchoredVwaps && <><span><i style={{ background: "#f0bd61" }}/>VWAP D</span><span><i style={{ background: "#4d9bff" }}/>VWAP S</span><span><i style={{ background: "#bd82ff" }}/>VWAP M</span></>}{showPreviousLevels && <><span><i style={{ background: "linear-gradient(90deg,#ff667c 50%,#44d49a 50%)" }}/>D−1 H/L</span><span><i style={{ background: "linear-gradient(90deg,#ff9a58 50%,#28d6c5 50%)" }}/>S−1 H/L</span><span><i style={{ background: "linear-gradient(90deg,#e86cff 50%,#7aa2ff 50%)" }}/>M−1 H/L</span></>}</div></div>
          <div className="chartarea"><span className="chartlabel">{symbol} · {interval} · OHLCV PERP REAL · BINANCE · UTC</span><span className="zoneinfo">{volatility?.status === "ready" ? `GARCH D ${(volatility.garch!.dailyVol * 100).toFixed(2)}% · HV252 D ${volatility.hv252?.dailyVol == null ? "N/A" : `${(volatility.hv252.dailyVol * 100).toFixed(2)}%`} · ${sigmaDistance == null ? "—" : `${sigmaDistance >= 0 ? "+" : ""}${sigmaDistance.toFixed(2)}σ ${sigmaZone}`} · ref ${fmtPrice(volatility.reference)}` : volatility?.status === "unavailable" ? "Volatilidade diária indisponível · aguardando resposta da Binance" : volatility?.status === "insufficient_history" ? `Histórico diário insuficiente (${volatility.observations ?? 0}/${volatility.required ?? 101} retornos)` : "Calculando zonas GARCH(1,1) e HV252…"}</span>{loading ? <div className="loading">Carregando candles da Binance…</div> : candles.length ? <MarketChart key={`${symbol}:${interval}`} candles={candles} symbol={symbol} interval={interval} zones={volatility} showGarch={showGarchZones} showHv={showHvZones} showVolumeAnomalies={showVolumeAnomalies} overlays={chartOverlays} showVwaps={showAnchoredVwaps} showPreviousLevels={showPreviousLevels} signals={chartSignals}/> : <div className="chartfallback">Candles indisponíveis · verifique a conexão com a API local</div>}</div>
          <div className="stats"><div className="stat"><div className="statlabel">MÁX. 24H · CONTRATO</div><div className="statvalue mono">{fmtPrice(current?.high24h)}</div></div><div className="stat"><div className="statlabel">MÍN. 24H · CONTRATO</div><div className="statvalue mono">{fmtPrice(current?.low24h)}</div></div><div className="stat"><div className="statlabel">VOLUME 24H · {nameOf(symbol)}</div><div className="statvalue mono">{fmtCompact(current?.volume24h)}</div></div><div className="stat"><div className="statlabel">FUNDING · PRÓXIMO CICLO</div><div className={`statvalue mono ${funding?.rate != null ? directionClass(funding.rate) : ""}`}>{funding?.rate != null ? `${(funding.rate * 100).toFixed(4)}%` : "—"}</div></div></div>
        </section>

        <aside className="rightstack">
          <section className="panel"><div className="panelhead"><span className="paneltitle">Perpetual order book</span><span className={`badge ${book?.status === "live" ? "good" : ""}`}>{book?.status === "live" ? "LIVE" : book?.status?.toUpperCase() ?? "AGUARDANDO"}</span></div>
            <div className="booksummary"><div><span>SPREAD</span><strong className="mono">{book ? fmtPrice(book.spread, 4) : "—"}</strong></div><div style={{ textAlign: "right" }}><span>IMBALANCE</span><strong className={`mono ${book ? directionClass(book.imbalance) : ""}`}>{book ? `${book.imbalance >= 0 ? "+" : ""}${book.imbalance.toFixed(2)}` : "—"}</strong></div></div>
            <div className="bookhead"><span>QTD.</span><span>PREÇO</span><span>TOTAL</span></div>
            {book?.asks?.slice().reverse().slice(0, 7).map((l,i) => <div className="bookrow" key={`a${i}`}><span className="mono">{l.quantity.toFixed(4)}</span><span className="mono neg">{fmtPrice(l.price, 2)}</span><span className="mono depth" style={{ "--depth": `${Math.min(100, l.total / Math.max(...book.asks.map(x => x.total),1) * 80)}%` } as React.CSSProperties}>{fmtCompact(l.total)}</span></div>)}
            <div className="midspread mono">{book && bestBid != null && bestAsk != null ? fmtPrice((bestBid + bestAsk) / 2) : "Conectando ao book…"}</div>
            {book?.bids?.slice(0,7).map((l,i) => <div className="bookrow" key={`b${i}`}><span className="mono">{l.quantity.toFixed(4)}</span><span className="mono pos">{fmtPrice(l.price, 2)}</span><span className="mono depth bid" style={{ "--depth": `${Math.min(100, l.total / Math.max(...book.bids.map(x => x.total),1) * 80)}%` } as React.CSSProperties}>{fmtCompact(l.total)}</span></div>)}
            <div className="dataerror">Profundidade atual via snapshot + deltas; histórico de liquidez começa com esta sessão.</div>
          </section>

          <section className="panel" id="reversal-signals"><div className="panelhead"><span className="paneltitle">Reversão · {nameOf(symbol)}</span><span className={`badge ${latestReversal?.direction === "BUY" ? "good" : latestReversal?.direction === "SELL" ? "neg" : ""}`}>{latestReversal ? `ÚLTIMO ${latestReversal.direction}` : "SEM SINAL"}</span></div>
            <div className="reversal-body">
              {reversalError ? <div className="loading">Sinais de reversão indisponíveis no momento.</div> : latestReversal ? <><div className={`reversal-score ${latestReversal.direction === "BUY" ? "pos" : "neg"}`}><strong>{latestReversal.direction}</strong><span>{latestReversal.strength.toUpperCase()} · {latestReversal.score}/100</span></div><div className="reversal-description">{latestReversal.pattern} · {latestReversal.source} · {latestReversal.sigma.toFixed(1)}σ</div><div className="reversal-description">Banda {fmtPrice(latestReversal.bandPrice)} · candle {fmtTime(latestReversal.time * 1000)}</div></> : reversalCandidate ? <><div className="reversal-score muted"><strong>{reversalCandidate.direction} · NÃO VALIDADO</strong><span>{reversalCandidate.strength.toUpperCase()} · {reversalCandidate.score}/100</span></div><div className="reversal-description">{reversalCandidate.suppressedByCooldown ? `Bloqueado pelo cooldown de ${cooldownCandles} candles` : `Score abaixo do mínimo ou sinal preliminar · ${reversalCandidate.pattern}`}</div></> : <div className="loading compact">Aguardando toque em banda ≥{minSigma.toFixed(1)}σ e rejeição confirmada por candle fechado.</div>}
              <div className="signal-toggles">
                <button className={`signal-toggle buy ${buyEnabled ? "active" : ""}`} onClick={() => setBuyEnabled(value => !value)}>BUY</button>
                <button className={`signal-toggle sell ${sellEnabled ? "active" : ""}`} onClick={() => setSellEnabled(value => !value)}>SELL</button>
                <button className={`signal-toggle ${garchSignalEnabled ? "active" : ""}`} onClick={() => setGarchSignalEnabled(value => !value)}>GARCH</button>
                <button className={`signal-toggle ${hvSignalEnabled ? "active" : ""}`} onClick={() => setHvSignalEnabled(value => !value)}>HV252</button>
                <button className={`signal-toggle ${volumeFilterEnabled ? "active" : ""}`} onClick={() => setVolumeFilterEnabled(value => !value)}>VOLUME</button>
              </div>
              <div className="signal-config"><label>σ mínimo<input type="number" min="1" max="3" step="0.1" value={minSigma} onChange={event => setMinSigma(Math.min(3, Math.max(1, Number(event.target.value) || 1)))}/></label><label>Tolerância %<input type="number" min="0" max="2" step="0.05" value={touchTolerancePct} onChange={event => setTouchTolerancePct(Math.min(2, Math.max(0, Number(event.target.value) || 0)))}/></label><label>Score mínimo<input type="number" min="0" max="100" step="5" value={minReversalScore} onChange={event => setMinReversalScore(Math.min(100, Math.max(0, Number(event.target.value) || 0)))}/></label><label>Cooldown · candles<input type="number" min="0" max="500" step="1" value={cooldownCandles} onChange={event => setCooldownCandles(Math.min(500, Math.max(0, Number(event.target.value) || 0)))}/></label></div>
              <div className="reversal-foot">Candle fechado · volume ≥1,2× média de 20 · score mínimo {minReversalScore} · cooldown {cooldownCandles} candles. Sinais analíticos; não enviam ordens.</div>
            </div>
          </section>

        </aside>
      </div>

      <div className="bottomgrid" id="flow">
        <section className="panel"><div className="panelhead"><span className="paneltitle">Times & trades</span><span className="badge good"><span className="dot" style={{ display: "inline-block", width: 5, height: 5 }}/> &nbsp;BINANCE · AGGTRADES</span></div>
          <div className="tape"><div className="taperow header"><span>HORA · UTC−3</span><span>PREÇO</span><span>QTD. {nameOf(symbol)}</span><span>LADO AGRESSOR</span></div>
            {trades.slice(0,18).map((trade,i)=><div className="taperow" key={`${trade.id}-${i}`}><span className="mono" style={{ color: "#8290a0" }}>{fmtTime(trade.time)}</span><span className={`mono ${trade.side === "buy" ? "pos" : "neg"}`}>{fmtPrice(trade.price, 2)}</span><span className="mono" style={{ textAlign: "right" }}>{trade.quantity.toFixed(5)}</span><span style={{ textAlign: "right", color: trade.side === "buy" ? "#44d49a" : "#ff667c" }}>{trade.side === "buy" ? "▲ COMPRA" : "▼ VENDA"}</span></div>)}
            {trades.length === 0 && <div className="loading">Aguardando negociações observadas…</div>}
          </div><div className="disclaimer">O lado agressor é inferido do campo da exchange (maker side). Agregação e CVD representam apenas a amostra observada nesta sessão; IDs deduplicados.</div>
        </section>

        <section className="panel" id="data"><div className="panelhead"><span className="paneltitle">Fluxo observado</span><Database size={13} color="#8794a2"/></div>
          <div className="coverage"><div className="coverrow"><span>CVD PERP · AMOSTRA</span><strong className={`mono ${cvd >= 0 ? "pos" : "neg"}`}>{cvd >= 0 ? "+" : ""}{cvd.toFixed(5)} {nameOf(symbol)}</strong></div><div className="coverrow"><span>Agressão compradora · perp</span><span className="mono pos">{buyVolume.toFixed(4)}</span></div><div className="coverrow"><span>Agressão vendedora · perp</span><span className="mono neg">{sellVolume.toFixed(4)}</span></div><div className="coveragebar"><div style={{ width: `${coverage}%` }}/></div><div className="coverrow"><span>Trades perp visíveis</span><span className="mono">{trades.length} / 24</span></div><div className="coverrow"><span>Cobertura do coletor</span><span className="mono">Sessão atual</span></div>
            <hr style={{ border: 0, borderTop: "1px solid #202833", margin: "9px 0" }}/>
            <div className="coverrow"><span>Sentimento · Fear & Greed</span><strong className="mono" style={{ color: fear?.value != null ? "#d5c080" : "#8996a5" }}>{fear?.value != null ? `${fear.value} · ${fear.label}` : "Indisponível"}</strong></div>
            <div className="coverrow"><span>Open interest · contratos</span><span className="mono">{fmtCompact(funding?.openInterest)}</span></div><div className="coverrow"><span>Mark price</span><span className="mono">{fmtPrice(funding?.markPrice)}</span></div><div className="coverrow"><span>Index price</span><span className="mono">{fmtPrice(funding?.indexPrice)}</span></div><div className="coverrow"><span>Próximo funding · UTC−3</span><span className="mono">{funding?.nextFundingTime ? fmtTime(funding.nextFundingTime) : "—"}</span></div>
            <div className="footnote">{book?.status === "live" ? `Book recebido ${fmtTime(book.receivedAt)} · ${book.eventTime ? `${Math.max(0, book.receivedAt-book.eventTime)} ms de latência` : "sem timestamp de exchange"}` : "Book aguardando conexão"}</div>
          </div>
        </section>
      </div>

      <div className="statusbar"><span><Wifi size={11} color={apiState === "connected" ? "#44d49a" : "#f0bd61"}/> {apiState === "connected" ? "API conectada" : apiState === "error" ? "API indisponível" : "Conectando API"}</span><span>Binance USDⓈ-M perpetual · isolated feed</span><span>Snapshot {fmtTime(current?.receivedAt)}</span><span>Latência {current?.latencyMs != null ? `${current.latencyMs} ms` : "—"}</span>{apiMessage && <span className="neg" title={apiMessage}>{apiMessage.slice(0, 85)}</span>}<button className={`alertbutton ${alertsOn ? "enabled" : ""}`} onClick={async () => { if (!alertsOn && "Notification" in window) { const permission = await Notification.requestPermission(); if (permission !== "granted") return; } setAlertsOn(v => !v); }}><Bell size={11}/>{alertsOn ? "Alertas ativos" : "Ativar alertas"}</button><span className="right">SOMENTE ANÁLISE · NÃO ENVIA ORDENS</span><CircleHelp size={12}/></div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}><button className="tinybtn" onClick={createPriceAlert}><Bell size={11} style={{ display: "inline", verticalAlign: "-2px" }}/> &nbsp;Criar alerta de preço · {fmtPrice(current?.price)}</button></div>
    </div>
  </main>;
}
