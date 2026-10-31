"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, CircleHelp, Crosshair, Database, LogOut, Maximize2, Radio, Search, Settings2, Wifi } from "lucide-react";
import MarketChart from "@/components/market-chart";
import FlowHistory from "@/components/flow-history";
import MarketScanner from "@/components/market-scanner";
import MarketAlerts from "@/components/market-alerts";
import MarketContext from "@/components/market-context";
import { fmtCompact, fmtPrice, fmtTime, getJson, getMarketWsToken, marketDataLabel, type BookData, type BreakoutSignalData, type Candle, type ChartOverlays, type MarketSnapshot, type ReversalSignalData, type ScannerFlowSnapshot, type Trade, type VolatilityZones } from "@/lib/market";

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
  const [market] = useState<"spot">("spot");
  const selectedKeyRef = useRef("");
  selectedKeyRef.current = `${symbol}|${interval}|${market}`;
  const [markets, setMarkets] = useState<Record<string, MarketSnapshot>>({});
  const [scannerFlow, setScannerFlow] = useState<ScannerFlowSnapshot | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [book, setBook] = useState<BookData | null>(null);
  const [bookThreshold, setBookThreshold] = useState(10_000);
  const [showAllBookLevels, setShowAllBookLevels] = useState(false);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradeSources, setTradeSources] = useState<Record<string, string>>({});
  const [tradeHealth, setTradeHealth] = useState<Record<string, { status: string; lastTradeAt?: number | null; reconnects?: number; error?: string | null }>>({});
  const [flowQuality, setFlowQuality] = useState<{ status: string; connected: boolean; lastEventTime: number | null; lastLatencyMs: number | null; events: number; reconnects: number; possibleIdGaps: number; backfillRowsQueued?: number; backfillSkippedIds?: number; backfillFailures?: number; storedTrades: number; oldestTradeTime: number | null; storedFlowBars?: number; oldestFlowBarTime?: number | null; aggregateRetentionDays?: number | null; queueDropped: number; storage: string; storageBytes: number; storageDurable: boolean; rawRetentionHours: number } | null>(null);
  const [reversalData, setReversalData] = useState<ReversalSignalData | null>(null);
  const [breakoutData, setBreakoutData] = useState<BreakoutSignalData | null>(null);
  const [breakoutError, setBreakoutError] = useState(false);
  const [breakoutBuyEnabled, setBreakoutBuyEnabled] = useState(true);
  const [breakoutSellEnabled, setBreakoutSellEnabled] = useState(true);
  const [breakoutPercentile, setBreakoutPercentile] = useState(20);
  const [breakoutVolumeMultiple, setBreakoutVolumeMultiple] = useState(1.5);
  const breakoutRequestRef = useRef(0);
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
  const [marketInfo, setMarketInfo] = useState<{ market: string; source: string; price: number | null; markPrice: number | null; indexPrice: number | null; fundingRate: number | null; openInterest: number | null; nextFundingTime: number | null; receivedAt: number; status: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [clock, setClock] = useState<number | null>(null);
  const [apiState, setApiState] = useState<"checking" | "connected" | "error">("checking");
  const [apiMessage, setApiMessage] = useState("");
  const [betaLimitMessage, setBetaLimitMessage] = useState("");

  const logout = async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.replace("/login");
  };

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
      getJson<{ trades: Trade[]; sources?: Record<string, string>; tradeHealth?: typeof tradeHealth }>(`/api/trades?symbol=${symbol}&market=${market}&limit=24`),
    ]);
    if (selectedKeyRef.current !== requestKey) return;
    if (candleResult.status === "fulfilled") setCandles(candleResult.value.candles);
    else setApiMessage(`Candles: ${candleResult.reason instanceof Error ? candleResult.reason.message : "indisponíveis"}`);
    if (bookResult.status === "fulfilled") setBook(bookResult.value);
    else setBook(null);
    if (tradeResult.status === "fulfilled") {
      setTrades(tradeResult.value.trades);
      setTradeSources(tradeResult.value.sources ?? {});
      setTradeHealth(tradeResult.value.tradeHealth ?? {});
    }
    if (isFirst) setLoading(false);
  }, [symbol, interval, market]);

  // External venue feeds are collected by the API process. Refresh their shared
  // samples and health independently while Binance remains on the browser WS.
  useEffect(() => {
    let active = true;
    const refresh = () => getJson<{ trades: Trade[]; sources?: Record<string, string>; tradeHealth?: typeof tradeHealth }>(`/api/trades?symbol=${symbol}&market=${market}&limit=60`)
      .then(data => {
        if (!active) return;
        setTradeSources(data.sources ?? {});
        setTradeHealth(data.tradeHealth ?? {});
        setTrades(current => {
          const unique = new Map<string, Trade>();
          for (const trade of [...current, ...data.trades]) unique.set(`${trade.source ?? "Binance"}:${trade.id}`, trade);
          return [...unique.values()].sort((a, b) => b.time - a.time).slice(0, 60);
        });
      })
      .catch(() => undefined);
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol, market]);

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

  const loadBreakoutSignals = useCallback(async () => {
    const requestId = ++breakoutRequestRef.current;
    const params = new URLSearchParams({
      symbol, interval, market, compression_percentile: String(breakoutPercentile),
      volume_multiple: String(breakoutVolumeMultiple), buy_enabled: String(breakoutBuyEnabled), sell_enabled: String(breakoutSellEnabled),
    });
    try {
      const data = await getJson<BreakoutSignalData>(`/api/breakout-signals?${params}`);
      if (requestId === breakoutRequestRef.current) { setBreakoutData(data); setBreakoutError(false); }
    } catch {
      if (requestId === breakoutRequestRef.current) { setBreakoutData(null); setBreakoutError(true); }
    }
  }, [symbol, interval, market, breakoutPercentile, breakoutVolumeMultiple, breakoutBuyEnabled, breakoutSellEnabled]);

  useEffect(() => {
    setBreakoutData(null);
    void loadBreakoutSignals();
    const timer = window.setInterval(() => void loadBreakoutSignals(), 30_000);
    return () => window.clearInterval(timer);
  }, [loadBreakoutSignals]);

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
    const refresh = () => getJson<ScannerFlowSnapshot>("/api/scanner-flow")
      .then(data => { if (active) setScannerFlow(data); })
      .catch(() => { if (active) setScannerFlow(null); });
    void refresh();
    const timer = window.setInterval(refresh, 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    let active = true;
    const refresh = () => getJson<NonNullable<typeof flowQuality>>(`/api/flow-quality?symbol=${symbol}`).then(data => { if (active) setFlowQuality(data); }).catch(() => { if (active) setFlowQuality(null); });
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol]);

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
    const connect = async () => {
      if (!active) return;
      try {
        const token = await getMarketWsToken();
        if (!active) return;
        const params = new URLSearchParams({ symbol, market, interval });
        socket = new WebSocket(`${streamUrl}?${params}`, ["nexus-market-v1", token]);
      } catch {
        if (!active || poll) return;
        poll = window.setInterval(() => {
          if (!active || polling) return;
          polling = true;
          void loadAsset().finally(() => { polling = false; });
        }, 3000);
        return;
      }
      socket.onmessage = (event) => {
        if (!active || selectedKeyRef.current !== `${symbol}|${interval}|${market}`) return;
        try {
            const message = JSON.parse(event.data) as { type: string; data: unknown; closed?: boolean };
          if (message.type === "trade") {
            const trade = message.data as Trade;
            const tradeKey = `${trade.source ?? "Binance"}:${trade.id}`;
            setTrades(current => [trade, ...current.filter(t => `${t.source ?? "Binance"}:${t.id}` !== tradeKey)].slice(0, 60));
          } else if (message.type === "book") setBook(message.data as BookData);
          else if (message.type === "market") {
            const snapshot = message.data as MarketSnapshot;
            setMarkets(current => ({ ...current, [snapshot.symbol]: snapshot }));
          } else if (message.type === "candle") {
            const candle = message.data as Candle;
            setCandles(current => { const next = [...current]; const i = next.findIndex(c => c.time === candle.time); if (i >= 0) next[i] = candle; else next.push(candle); return next.slice(-260); });
            if (message.closed) { void loadReversalSignals(); void loadBreakoutSignals(); }
          }
        } catch { /* invalid event is ignored; REST refresh continues */ }
      };
      socket.onopen = () => { setBetaLimitMessage(""); if (poll) { window.clearInterval(poll); poll = null; } };
      socket.onerror = () => socket?.close();
      socket.onclose = (event) => {
        if (event.code === 1013) {
          setBetaLimitMessage("Limite de sessões ao vivo do beta atingido. Tente novamente mais tarde.");
          return;
        }
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
  }, [symbol, interval, market, loadAsset, loadReversalSignals, loadBreakoutSignals]);

  useEffect(() => {
    let live = true;
    const refresh = () => getJson<typeof marketInfo>(`/api/market-info?symbol=${symbol}&market=${market}`).then(data => { if (live) setMarketInfo(data); }).catch(() => { if (live) setMarketInfo(null); });
    refresh(); const timer = window.setInterval(refresh, 10000);
    return () => { live = false; window.clearInterval(timer); };
  }, [symbol, market]);

  const current = markets[symbol];
  const latestReversal = reversalData?.latest ?? null;
  const reversalCandidate = reversalData?.lastCandidate ?? null;
  const latestBreakout = breakoutData?.latest ?? null;
  const chartSignals = useMemo(() => {
    const candleTimes = new Set(candles.map(candle => candle.time));
    return (reversalData?.signals ?? []).filter(item => item.qualified && candleTimes.has(item.time));
  }, [candles, reversalData]);
  const chartBreakoutSignals = useMemo(() => {
    const candleTimes = new Set(candles.map(candle => candle.time));
    return (breakoutData?.signals ?? []).filter(item => candleTimes.has(item.time));
  }, [candles, breakoutData]);
  const sigmaDistance = current?.price != null && volatility?.reference != null && volatility.garch?.dailyVol ? Math.log(current.price / volatility.reference) / volatility.garch.dailyVol : null;
  const sigmaZone = sigmaDistance == null ? "Sem dados" : Math.abs(sigmaDistance) < 0.5 ? "Neutra" : Math.abs(sigmaDistance) < 1 ? "Moderada" : Math.abs(sigmaDistance) < 2 ? "Atenção" : Math.abs(sigmaDistance) < 3 ? "Extrema" : "Anomalia";
  const bestBid = book?.bids[0]?.price ?? null;
  const bestAsk = book?.asks[0]?.price ?? null;
  const liveBookSources = Object.values(book?.sources ?? {}).filter(status => status === "live").length;
  const visibleAsks = useMemo(() => {
    const levels = book?.asks?.slice().reverse() ?? [];
    return (showAllBookLevels ? levels : levels.filter(level => level.price * level.quantity >= bookThreshold)).slice(0, 7);
  }, [book, bookThreshold, showAllBookLevels]);
  const visibleBids = useMemo(() => {
    const levels = book?.bids ?? [];
    return (showAllBookLevels ? levels : levels.filter(level => level.price * level.quantity >= bookThreshold)).slice(0, 7);
  }, [book, bookThreshold, showAllBookLevels]);
  const bookMaxNotional = useMemo(() => Math.max(1, ...visibleAsks.map(level => level.price * level.quantity), ...visibleBids.map(level => level.price * level.quantity)), [visibleAsks, visibleBids]);
  const cumulativeAsks = useMemo(() => {
    let total = 0;
    return visibleAsks.map(level => { total += level.price * level.quantity; return total; });
  }, [visibleAsks]);
  const cumulativeBids = useMemo(() => {
    let total = 0;
    return visibleBids.map(level => { total += level.price * level.quantity; return total; });
  }, [visibleBids]);
  const cvd = useMemo(() => trades.reduce((sum, trade) => sum + (trade.side === "buy" ? 1 : -1) * trade.quantity, 0), [trades]);
  const buyVolume = useMemo(() => trades.filter(t => t.side === "buy").reduce((n,t) => n+t.quantity,0), [trades]);
  const sellVolume = useMemo(() => trades.filter(t => t.side === "sell").reduce((n,t) => n+t.quantity,0), [trades]);
  const tradeOutliers = useMemo(() => {
    const notionalsBySource = new Map<string, number[]>();
    for (const trade of trades) {
      const source = trade.source ?? "Binance";
      const values = notionalsBySource.get(source) ?? [];
      values.push(trade.price * trade.quantity);
      notionalsBySource.set(source, values);
    }
    const medians = new Map<string, number>();
    for (const [source, values] of notionalsBySource) {
      if (values.length < 5) continue;
      const sorted = values.slice().sort((a, b) => a - b);
      const middle = Math.floor(sorted.length / 2);
      medians.set(source, sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2);
    }
    const outliers = new Map<string, { notional: number; median: number; ratio: number }>();
    for (const trade of trades) {
      const source = trade.source ?? "Binance";
      const median = medians.get(source);
      const notional = trade.price * trade.quantity;
      if (median == null || median <= 0) continue;
      const threshold = Math.max(10_000, median * 5);
      if (notional >= threshold) outliers.set(`${source}:${trade.id}`, { notional, median, ratio: notional / median });
    }
    return outliers;
  }, [trades]);
  const coverage = Math.min(100, Math.round((trades.length / 60) * 100));
  const directionClass = (value: number | null | undefined) => value != null && value >= 0 ? "pos" : "neg";

  return <main className="shell">
    <header className="topbar">
      <div className="brand"><div className="logo">N</div><div><strong>NEXUS</strong><small>MARKET FLOW TERMINAL</small></div></div>
      <nav className="nav"><button className="active" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>Terminal</button><button onClick={() => document.getElementById("scanner")?.scrollIntoView({ behavior: "smooth" })}>Scanner</button><button onClick={() => document.getElementById("alerts")?.scrollIntoView({ behavior: "smooth" })}>Alertas</button><button onClick={() => document.getElementById("context")?.scrollIntoView({ behavior: "smooth" })}>Contexto</button><button onClick={() => document.getElementById("flow")?.scrollIntoView({ behavior: "smooth" })}>Fluxo</button><button onClick={() => document.getElementById("data")?.scrollIntoView({ behavior: "smooth" })}>Dados</button></nav>
      <div className="topright"><div className="live"><span className={`dot ${apiState === "error" ? "neg" : ""}`} />{apiState === "connected" ? "STREAM AO VIVO" : apiState === "error" ? "API OFFLINE" : "CONECTANDO"}</div><span className="exchange">{marketDataLabel}</span><button className="iconbtn" title="Pesquisar ativo"><Search size={16}/></button><button className="iconbtn" title="Configurações"><Settings2 size={16}/></button><button className="iconbtn" title="Sair" aria-label="Sair" onClick={logout}><LogOut size={16}/></button></div>
    </header>

    <div className="wrap">
      <div className="heading"><div><div className="eyebrow">SPOT MARKET INTELLIGENCE / 01</div><h1>Spot Market Flow</h1></div><div className="headmeta"><span className="badge good"><Radio size={10} style={{ display: "inline", verticalAlign: "-2px" }}/> &nbsp;BINANCE SPOT</span><span className="badge">UTC−3 · SÃO PAULO</span><span className="badge">{clock == null ? "--:--:--" : fmtTime(clock)}</span></div></div>

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
        <aside className="panel watchlist"><div className="panelhead"><span className="paneltitle">Watchlist</span><span className="badge">25 SPOT</span></div>
          {assets.map((asset) => { const m = markets[asset]; const ticker = nameOf(asset); const icon = asset === "BTCUSDT" ? "₿" : asset === "ETHUSDT" ? "Ξ" : ticker.slice(0, 1); return <button key={asset} className={`watchrow ${symbol === asset ? "selected" : ""}`} onClick={() => setSymbol(asset)}>
            <div className="coinicon">{icon}</div><div><div className="coinname">{ticker} <span style={{ color: "#697687", fontWeight: 400 }}>/ USDT</span></div><div className="coinsub">Binance Spot</div></div>
            <div><div className="coinprice mono">{fmtPrice(m?.price, m?.price && m.price < 10 ? 4 : 2)}</div><div className={`change ${directionClass(m?.change24h)}`}>{m?.change24h == null ? "—" : `${m.change24h >= 0 ? "+" : ""}${m.change24h.toFixed(2)}%`}</div></div>
          </button>; })}
          <div className="watchfoot subtle">Mercado à vista USDT · Binance</div>
        </aside>

        <div className="chart-flow-column">
        <section className="panel mainchart"><div className="assetbar"><div><h2>{nameOf(symbol)} <span style={{ color: "#7b8795", fontWeight: 400 }}>/ USDT</span></h2><span className="subtle">BINANCE · SPOT</span></div><span className="largeprice mono">{fmtPrice(current?.price, current?.price && current.price < 10 ? 4 : 2)}</span><span className={`asset-change ${directionClass(current?.change24h)}`}>{current?.change24h == null ? "—" : `${current.change24h >= 0 ? "+" : ""}${current.change24h.toFixed(2)}%`}</span><button className="iconbtn" title="Tela cheia" onClick={() => document.querySelector(".mainchart")?.requestFullscreen()}><Maximize2 size={14}/></button></div>
          <div className="charttools"><span className="subtle">TF</span>{intervals.map(tf => <button key={tf} className={interval === tf ? "active" : ""} onClick={() => setInterval(tf)}>{tf.toUpperCase()}</button>)}<button className={showGarchZones ? "active" : ""} onClick={() => setShowGarchZones(v => !v)}>GARCH</button><button className={showHvZones ? "active" : ""} onClick={() => setShowHvZones(v => !v)}>HV252</button><button className={`volume-toggle ${showAnchoredVwaps ? "active" : ""}`} title="Exibe VWAPs ancoradas no início do dia, semana e mês UTC" onClick={() => setShowAnchoredVwaps(v => !v)}>VWAP D/S/M</button><button className={`volume-toggle ${showPreviousLevels ? "active" : ""}`} title="Exibe máximas e mínimas do dia, semana e mês anteriores (UTC)" onClick={() => setShowPreviousLevels(v => !v)}>H/L ANT.</button><button className={`volume-toggle ${showVolumeAnomalies ? "active" : ""}`} title="Destaca candles com volume de pelo menos 1,5× a média dos 20 candles anteriores" onClick={() => setShowVolumeAnomalies(v => !v)}>VOL ≥1,5×</button><span style={{ marginLeft: "auto" }} className="subtle"><Crosshair size={12}/></span></div>
          <div className="chart-overlay-strip"><div className="chart-overlay-legend">{showAnchoredVwaps && <><span><i style={{ background: "#f0bd61" }}/>VWAP D</span><span><i style={{ background: "#4d9bff" }}/>VWAP S</span><span><i style={{ background: "#bd82ff" }}/>VWAP M</span></>}{showPreviousLevels && <><span><i style={{ background: "linear-gradient(90deg,#ff667c 50%,#44d49a 50%)" }}/>D−1 H/L</span><span><i style={{ background: "linear-gradient(90deg,#ff9a58 50%,#28d6c5 50%)" }}/>S−1 H/L</span><span><i style={{ background: "linear-gradient(90deg,#e86cff 50%,#7aa2ff 50%)" }}/>M−1 H/L</span></>}</div></div>
          <div className="chartarea"><span className="chartlabel">{symbol} · {interval} · OHLCV SPOT · BINANCE · UTC</span><span className="zoneinfo">{volatility?.status === "ready" ? `GARCH D ${(volatility.garch!.dailyVol * 100).toFixed(2)}% · HV252 D ${volatility.hv252?.dailyVol == null ? "N/A" : `${(volatility.hv252.dailyVol * 100).toFixed(2)}%`} · ${sigmaDistance == null ? "—" : `${sigmaDistance >= 0 ? "+" : ""}${sigmaDistance.toFixed(2)}σ ${sigmaZone}`} · ref ${fmtPrice(volatility.reference)}` : volatility?.status === "unavailable" ? "Volatilidade diária indisponível · aguardando resposta da Binance" : volatility?.status === "insufficient_history" ? `Histórico diário insuficiente (${volatility.observations ?? 0}/${volatility.required ?? 101} retornos)` : "Calculando zonas GARCH(1,1) e HV252…"}</span>{loading ? <div className="loading">Carregando candles da Binance…</div> : candles.length ? <MarketChart key={`${symbol}:${interval}`} candles={candles} symbol={symbol} interval={interval} zones={volatility} showGarch={showGarchZones} showHv={showHvZones} showVolumeAnomalies={showVolumeAnomalies} overlays={chartOverlays} showVwaps={showAnchoredVwaps} showPreviousLevels={showPreviousLevels} signals={chartSignals} breakoutSignals={chartBreakoutSignals}/> : <div className="chartfallback">Candles indisponíveis · verifique a conexão com a API local</div>}</div>
          <div className="stats"><div className="stat"><div className="statlabel">MÁX. 24H · SPOT</div><div className="statvalue mono">{fmtPrice(current?.high24h)}</div></div><div className="stat"><div className="statlabel">MÍN. 24H · SPOT</div><div className="statvalue mono">{fmtPrice(current?.low24h)}</div></div><div className="stat"><div className="statlabel">VOLUME 24H · {nameOf(symbol)}</div><div className="statvalue mono">{fmtCompact(current?.volume24h)}</div></div><div className="stat"><div className="statlabel">PREÇO SPOT</div><div className="statvalue mono">{fmtPrice(marketInfo?.price)}</div></div></div>
        </section>

      <div className="bottomgrid" id="flow">
        <section className="panel"><div className="panelhead"><span className="paneltitle">Times & trades agregado</span><span className={`badge ${Object.values(tradeSources).filter(status => status === "live").length === 3 ? "good" : ""}`} title={["Binance", "Kraken", "Coinbase"].map(venue => { const health = tradeHealth[venue]; return `${venue}: ${health?.status ?? tradeSources[venue] ?? "connecting"}; último trade ${health?.lastTradeAt ? fmtTime(health.lastTradeAt) : "sem trade recebido"}${health?.reconnects ? `; reconexões ${health.reconnects}` : ""}${health?.error ? `; ${health.error}` : ""}`; }).join("\n")}>{["Binance", "Kraken", "Coinbase"].map(venue => `${venue}: ${(tradeSources[venue] ?? "connecting").toUpperCase()}`).join(" · ")}</span></div>
          <div className="tape"><div className="taperow header"><span>HORA · UTC−3</span><span>PREÇO</span><span>QTD. / VALOR</span><span>LADO AGRESSOR</span><span>ORIGEM</span></div>
            {trades.slice(0,18).map((trade,i)=>{const source=trade.source??"Binance";const id=`${source}:${trade.id}`;const anomaly=tradeOutliers.get(id);const notional=trade.price*trade.quantity;return <div className={`taperow ${anomaly?"trade-outlier":""}`} key={`${id}-${i}`} title={anomaly?`Valor ≈ $${fmtCompact(anomaly.notional)} · ${anomaly.ratio.toFixed(1)}× mediana de ${source} (janela de até 24 trades)`:undefined}><span className="mono" style={{ color: "#8290a0" }}>{fmtTime(trade.time)}</span><span className={`mono ${trade.side === "buy" ? "pos" : "neg"}`}>{fmtPrice(trade.price, 2)}</span><span className="trade-quantity"><strong className="mono">{trade.quantity.toFixed(5)}</strong><small>≈ ${fmtCompact(notional)}</small></span><span style={{ textAlign: "right", color: trade.side === "buy" ? "#44d49a" : "#ff667c" }}>{trade.side === "buy" ? "▲ COMPRA" : "▼ VENDA"}</span><span className="trade-origin"><span>{source}</span>{anomaly&&<b className="trade-anomaly">ALTO · {anomaly.ratio.toFixed(0)}×</b>}</span></div>})}
            {trades.length === 0 && <div className="loading">Aguardando negociações observadas…</div>}
          </div><div className="disclaimer">ALTO = notional ≥5× mediana da corretora e ≥US$ 10 mil, usando até 24 trades recentes (mínimo de 5 por corretora). USD ≈ USDT. É um destaque estatístico de execução, não identifica participante ou intenção.</div>
        </section>

        <section className="panel" id="data"><div className="panelhead"><span className="paneltitle">Fluxo observado</span><Database size={13} color="#8794a2"/></div>
          <div className="coverage"><div className="coverrow"><span>CVD SPOT · AMOSTRA VISÍVEL</span><strong className={`mono ${cvd >= 0 ? "pos" : "neg"}`}>{cvd >= 0 ? "+" : ""}{cvd.toFixed(5)} {nameOf(symbol)}</strong></div><div className="coverrow"><span>Agressão compradora · spot</span><span className="mono pos">{buyVolume.toFixed(4)}</span></div><div className="coverrow"><span>Agressão vendedora · spot</span><span className="mono neg">{sellVolume.toFixed(4)}</span></div><div className="coveragebar"><div style={{ width: `${coverage}%` }}/></div><div className="coverrow"><span>Trades na fita</span><span className="mono">{trades.length} / 60</span></div><div className="coverrow"><span>Coletor Binance</span><span className={`mono ${flowQuality?.connected ? "pos" : "neg"}`}>{flowQuality?.connected ? "AO VIVO" : flowQuality?.status?.toUpperCase() ?? "AGUARDANDO"}</span></div><div className="coverrow"><span>Último evento · latência</span><span className="mono">{flowQuality?.lastEventTime ? `${fmtTime(flowQuality.lastEventTime)} · ${flowQuality.lastLatencyMs ?? "—"} ms` : "—"}</span></div><div className="coverrow"><span>Eventos · reconexões</span><span className="mono">{flowQuality?.events ?? 0} · {flowQuality?.reconnects ?? 0}</span></div><div className="coverrow"><span>Saltos · trades recuperados · falhas</span><span className="mono">{flowQuality?.possibleIdGaps ?? 0} · {flowQuality?.backfillRowsQueued ?? 0} · {flowQuality?.backfillFailures ?? 0}</span></div><div className="coverrow"><span>Trades brutos · retenção</span><span className="mono">{flowQuality?.storedTrades ?? 0} · {flowQuality?.rawRetentionHours ?? "—"} h</span></div><div className="coverrow"><span>Barras de fluxo · retenção</span><span className="mono">{flowQuality?.storedFlowBars ?? 0} · {flowQuality?.aggregateRetentionDays ?? "—"} d</span></div><div className="coverrow"><span>Armazenamento</span><span className="mono">{flowQuality ? `${flowQuality.storage.toUpperCase()} · ${(flowQuality.storageBytes / 1048576).toFixed(1)} MB` : "—"}</span></div><div className="coverrow"><span>Fila descartada</span><span className={`mono ${flowQuality?.queueDropped ? "neg" : "pos"}`}>{flowQuality?.queueDropped ?? 0}</span></div>{flowQuality?.storage === "sqlite" && !flowQuality.storageDurable && <div className="dataerror">SQLite no disco local do serviço: no Render sem volume persistente, o histórico pode sumir em reinícios ou deploys.</div>}{flowQuality?.storage === "postgres" && flowQuality.storageBytes >= 400 * 1024 * 1024 && <div className="dataerror">Banco próximo da cota gratuita de 500 MB. Reduza a retenção em FLOW_RETENTION_HOURS e FLOW_AGG_RETENTION_DAYS.</div>}
            <hr style={{ border: 0, borderTop: "1px solid #202833", margin: "9px 0" }}/>
            <div className="coverrow"><span>Sentimento · Fear & Greed</span><strong className="mono" style={{ color: fear?.value != null ? "#d5c080" : "#8996a5" }}>{fear?.value != null ? `${fear.value} · ${fear.label}` : "Indisponível"}</strong></div>
            <div className="coverrow"><span>Mercado</span><span className="mono">À vista · sem alavancagem</span></div><div className="coverrow"><span>Preço de referência spot</span><span className="mono">{fmtPrice(marketInfo?.price)}</span></div>
            <div className="footnote">{book?.status === "live" ? `Book recebido ${fmtTime(book.receivedAt)} · ${book.eventTime ? `${Math.max(0, book.receivedAt-book.eventTime)} ms de latência` : "sem timestamp de exchange"}` : "Book aguardando conexão"}</div>
          </div>
          <FlowHistory symbol={symbol}/>
        </section>
      </div>

        </div>



        <aside className="rightstack">
          <section className="panel"><div className="panelhead"><span className="paneltitle">Spot order book · 3 corretoras</span><span className={`badge ${book?.status === "live" ? "good" : ""}`}>{liveBookSources}/3 FONTES</span></div>
            <div className="booksummary"><div><span>GAP INDICATIVO · MELHOR BID/ASK</span><strong className="mono">{book ? fmtPrice(book.spread, 4) : "—"}</strong></div><div style={{ textAlign: "right" }}><span>IMBALANCE</span><strong className={`mono ${book ? directionClass(book.imbalance) : ""}`}>{book ? `${book.imbalance >= 0 ? "+" : ""}${book.imbalance.toFixed(2)}` : "—"}</strong></div></div>
            <div className="bookfilters"><label>Filtro mínimo<select value={showAllBookLevels ? "all" : String(bookThreshold)} onChange={event => { const value = event.target.value; setShowAllBookLevels(value === "all"); if (value !== "all") setBookThreshold(Number(value)); }}><option value="all">Todos os níveis</option><option value="10000">10 mil USDT</option><option value="50000">50 mil USDT</option><option value="100000">100 mil USDT</option><option value="250000">250 mil USDT</option><option value="500000">500 mil USDT</option></select></label><span>Valor visível = preço × quantidade</span></div>
            <div className="depthbook">
              <div className="depthbook-head"><span>QTD. COMPRA</span><span>TOTAL</span><span>COMPRA</span><span>VENDA</span><span>TOTAL</span><span>QTD. VENDA</span></div>
              <div className="depthbook-grid">
                <div className="depthbook-side left">{visibleBids.map((level, index) => <div className="depthbook-row" key={`b${index}`} title={`Fontes: ${(level.sources ?? []).join(", ")}`}><span className="book-qty mono">{fmtCompact(level.quantity)}</span><span className="book-total mono">{fmtCompact(cumulativeBids[index])}</span><span className="book-price mono pos">{fmtPrice(level.price, 2)}</span><span className="book-bar bid" style={{ "--book-depth": `${Math.min(100, (level.price * level.quantity / bookMaxNotional) * 100)}%` } as React.CSSProperties}/></div>)}{visibleBids.length === 0 && <div className="bookempty">Sem ofertas de compra nesta amostra.</div>}</div>
                <div className="depthbook-side right">{visibleAsks.map((level, index) => <div className="depthbook-row" key={`a${index}`} title={`Fontes: ${(level.sources ?? []).join(", ")}`}><span className="book-price mono neg">{fmtPrice(level.price, 2)}</span><span className="book-bar ask" style={{ "--book-depth": `${Math.min(100, (level.price * level.quantity / bookMaxNotional) * 100)}%` } as React.CSSProperties}/><span className="book-total mono">{fmtCompact(cumulativeAsks[index])}</span><span className="book-qty mono">{fmtCompact(level.quantity)}</span></div>)}{visibleAsks.length === 0 && <div className="bookempty">Sem ofertas de venda nesta amostra.</div>}</div>
              </div>
              <div className="depthbook-mid"><span className="mono pos">{bestBid != null ? fmtPrice(bestBid, 2) : "—"}</span><span>{book && bestBid != null && bestAsk != null ? `GAP ${fmtPrice(book.spread, 4)}` : "CONECTANDO"}</span><span className="mono neg">{bestAsk != null ? fmtPrice(bestAsk, 2) : "—"}</span></div>
              <div className="depthbook-foot">BINANCE + KRAKEN + COINBASE · USD ≈ USDT · FAIXAS DE 0,1 PB</div>
            </div>
            <div className="dataerror">Book indicativo: o gap pode ser negativo entre corretoras e não representa arbitragem executável. Cada nível mostra as fontes ao passar o cursor; USD ≈ USDT.</div>
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

          <section className="panel" id="breakout-signals"><div className="panelhead"><span className="paneltitle">Rompimento · {nameOf(symbol)}</span><span className={`badge ${latestBreakout?.direction === "BUY" ? "good" : latestBreakout?.direction === "SELL" ? "neg" : ""}`}>{breakoutError ? "INDISPONÍVEL" : latestBreakout ? `ÚLTIMO ${latestBreakout.direction}` : "SEM SINAL"}</span></div>
            <div className="reversal-body">
              {breakoutError ? <div className="loading">Estratégia de rompimento indisponível no momento.</div> : breakoutData?.status === "insufficient_history" ? <div className="loading compact">Histórico insuficiente: {breakoutData.observedCandles ?? 0}/{breakoutData.requiredCandles ?? 0} candles necessários.</div> : latestBreakout ? <><div className={`reversal-score ${latestBreakout.direction === "BUY" ? "pos" : "neg"}`}><strong>{latestBreakout.direction === "BUY" ? "ROMPIMENTO ↑" : "ROMPIMENTO ↓"}</strong><span>VOLUME {latestBreakout.volumeRatio.toFixed(2)}×</span></div><div className="reversal-description">Fechamento {fmtPrice(latestBreakout.price)} · faixa {fmtPrice(latestBreakout.rangeLow)}—{fmtPrice(latestBreakout.rangeHigh)}</div><div className="reversal-description">Compressão confirmada · candle {fmtTime(latestBreakout.time * 1000)}</div></> : <div className="loading compact">Aguardando compressão recente, fechamento além da faixa e volume confirmado.</div>}
              <div className="signal-toggles"><button className={`signal-toggle buy ${breakoutBuyEnabled ? "active" : ""}`} onClick={() => setBreakoutBuyEnabled(value => !value)}>BUY</button><button className={`signal-toggle sell ${breakoutSellEnabled ? "active" : ""}`} onClick={() => setBreakoutSellEnabled(value => !value)}>SELL</button></div>
              <div className="signal-config"><label>Compressão · percentil<input type="number" min="1" max="50" step="1" value={breakoutPercentile} onChange={event => setBreakoutPercentile(Math.min(50, Math.max(1, Number(event.target.value) || 20)))}/></label><label>Volume mínimo · × média<input type="number" min="1" max="10" step="0.1" value={breakoutVolumeMultiple} onChange={event => setBreakoutVolumeMultiple(Math.min(10, Math.max(1, Number(event.target.value) || 1.5)))}/></label></div>
              <div className="reversal-foot">BB(20) comprimida no percentil {breakoutPercentile} dos últimos 100 candles · rompimento da faixa de 20 candles · volume ≥{breakoutVolumeMultiple.toFixed(1)}× média dos 20 anteriores · cooldown 10 candles. Regra independente da reversão; análise spot, sem ordens.</div>
            </div>
          </section>

        </aside>
      </div>

      <div className="scanner-insights">
        <MarketScanner symbols={assets} markets={markets} flow={scannerFlow} selectedSymbol={symbol} onSelect={setSymbol}/>
        <div className="insights-grid"><MarketAlerts symbols={assets} markets={markets} flow={scannerFlow}/><MarketContext markets={markets}/></div>
      </div>

      <div className="statusbar"><span><Wifi size={11} color={apiState === "connected" ? "#44d49a" : "#f0bd61"}/> {apiState === "connected" ? "API conectada" : apiState === "error" ? "API indisponível" : "Conectando API"}</span><span>{marketDataLabel}</span><span>Snapshot {fmtTime(current?.receivedAt)}</span><span>Latência {current?.latencyMs != null ? `${current.latencyMs} ms` : "—"}</span>{(betaLimitMessage || apiMessage) && <span className="neg" title={betaLimitMessage || apiMessage}>{(betaLimitMessage || apiMessage).slice(0, 85)}</span>}<button className="alertbutton" onClick={() => document.getElementById("alerts")?.scrollIntoView({ behavior: "smooth" })}><Bell size={11}/>Configurar alertas</button><span className="right">SOMENTE ANÁLISE · NÃO ENVIA ORDENS</span><CircleHelp size={12}/></div>
    </div>
  </main>;
}
