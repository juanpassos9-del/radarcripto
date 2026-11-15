"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, LogOut, Search, Wifi } from "lucide-react";
import MarketChart from "@/components/market-chart";
import { fmtCompact, fmtPrice, fmtTime, getJson, getMarketWsToken, type BreakoutSignalData, type Candle, type ChartOverlays, type ReversalSignalData, type Trade, type TrendSignalData, type VolatilityZones, type VwapDeviationSignalData } from "@/lib/market";

type Instrument = { symbol: string; name: string; category: "indices" | "forex" | "commodities" | "stocks"; minimumNotional?: number; pricePrecision?: number; quoteVolume24h?: number };
type TradfiMarket = {
  source: string; receivedAt: number;
  ticker: { price: number; change24h: number; high24h: number | null; low24h: number | null; quoteVolume24h: number; eventTime: number };
  book: { bids: { price: number; quantity: number; total: number }[]; asks: { price: number; quantity: number; total: number }[]; spread: number; imbalance: number; status: string };
  trades: Trade[];
};
type TradfiIndicators = { zones: VolatilityZones; overlays: ChartOverlays; reversal: ReversalSignalData; breakout: BreakoutSignalData; trend: TrendSignalData; vwapDeviation: VwapDeviationSignalData; limitations: string[]; receivedAt: number };
type WatchQuote = { symbol: string; price: number | null; previousClose: number | null; changePercent: number | null; referenceTime: number | null; quoteVolume24h?: number | null };

const intervals = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const emptySignals: never[] = [];

export default function TradfiTerminal() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [watchQuotes, setWatchQuotes] = useState<Record<string, WatchQuote>>({});
  const [category, setCategory] = useState<"indices" | "forex" | "commodities" | "stocks">("indices");
  const [symbol, setSymbol] = useState("NCSISP5002USD-USDT");
  const [interval, setInterval] = useState("5m");
  const [search, setSearch] = useState("");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [indicators, setIndicators] = useState<TradfiIndicators | null>(null);
  const [indicatorError, setIndicatorError] = useState("");
  const [showGarch, setShowGarch] = useState(true);
  const [showHv, setShowHv] = useState(true);
  const [showVwaps, setShowVwaps] = useState(true);
  const [showPreviousLevels, setShowPreviousLevels] = useState(true);
  const [showVolumeAnomalies, setShowVolumeAnomalies] = useState(true);
  const [showReversal, setShowReversal] = useState(true);
  const [showBreakout, setShowBreakout] = useState(true);
  const [showTrend, setShowTrend] = useState(true);
  const [showVwapDeviation, setShowVwapDeviation] = useState(true);
  const [dvVolumeHigh, setDvVolumeHigh] = useState(1.8);
  const [dvVolumeLow, setDvVolumeLow] = useState(0.7);
  const [dvSpreadWide, setDvSpreadWide] = useState(1.5);
  const [dvSpreadNarrow, setDvSpreadNarrow] = useState(0.7);
  const [dvCloseExtreme, setDvCloseExtreme] = useState(0.7);
  const [dvAveragePeriods, setDvAveragePeriods] = useState(20);
  const [snapshot, setSnapshot] = useState<TradfiMarket | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    getJson<{ instruments: Instrument[] }>("/api/tradfi/contracts").then(data => {
      if (!active) return;
      setInstruments(data.instruments);
      if (!data.instruments.some(item => item.symbol === symbol)) {
        const firstIndex = data.instruments.find(item => item.category === "indices");
        if (firstIndex) setSymbol(firstIndex.symbol);
      }
    }).catch(() => { if (active) setError("Não foi possível carregar o catálogo de instrumentos."); });
    return () => { active = false; };
  // Initial contract selection is only a fallback until the live catalogue loads.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    getJson<{ candles: Candle[] }>(`/api/tradfi/candles?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=300`)
      .then(data => { if (active) { setCandles(data.candles); setLoading(false); } })
      .catch(() => { if (active) { setCandles([]); setLoading(false); setError("Candles indisponíveis neste momento. O contrato pode estar fora da sessão."); } });
    return () => { active = false; };
  }, [symbol, interval]);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ symbol, interval, dv_volume_high: String(dvVolumeHigh), dv_volume_low: String(dvVolumeLow), dv_spread_wide: String(dvSpreadWide), dv_spread_narrow: String(dvSpreadNarrow), dv_close_extreme: String(dvCloseExtreme), dv_average_periods: String(dvAveragePeriods) });
    const refresh = () => getJson<TradfiIndicators>(`/api/tradfi/indicators?${params}`)
      .then(data => { if (active) { setIndicators(data); setIndicatorError(""); } })
      .catch(() => { if (active) { setIndicators(null); setIndicatorError("Indicadores indisponíveis ou com histórico insuficiente para este contrato/sessão."); } });
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol, interval, dvVolumeHigh, dvVolumeLow, dvSpreadWide, dvSpreadNarrow, dvCloseExtreme, dvAveragePeriods]);

  useEffect(() => {
    let active = true;
    const refresh = () => getJson<TradfiMarket>(`/api/tradfi/market?symbol=${encodeURIComponent(symbol)}`)
      .then(data => { if (active) { setSnapshot(data); setError(""); } })
      .catch(() => { if (active) setError("Feed temporariamente indisponível para este contrato."); });
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol]);

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let reconnectTimer = 0;
    let pollTimer = 0;
    let retryDelay = 1000;
    let refreshToken = false;
    const refresh = () => getJson<{ quotes: WatchQuote[] }>(`/api/tradfi/watchlist?category=${category}`)
      .then(data => { if (active) setWatchQuotes(current => ({ ...current, ...Object.fromEntries(data.quotes.map(quote => [quote.symbol, quote])) })); })
      .catch(() => undefined);
    const startFallbackPolling = () => {
      if (!pollTimer) {
        void refresh();
        pollTimer = window.setInterval(refresh, 15_000);
      }
    };
    const connect = async () => {
      try {
        const token = await getMarketWsToken(refreshToken);
        refreshToken = false;
        if (!active) return;
        const apiOrigin = process.env.NEXT_PUBLIC_MARKET_API_URL?.replace(/\/+$/, "") || window.location.origin;
        const wsOrigin = apiOrigin.replace(/^http/, "ws");
        const url = new URL("/ws/tradfi/watchlist", wsOrigin);
        url.searchParams.set("category", category);
        socket = new WebSocket(url, ["nexus-market-v1", token]);
        socket.onopen = () => {
          retryDelay = 1000;
          if (pollTimer) window.clearInterval(pollTimer);
          pollTimer = 0;
          void refresh();
        };
        socket.onmessage = event => {
          try {
            const message = JSON.parse(event.data) as { type?: string; data?: WatchQuote };
            if (message.type === "quote" && message.data?.symbol && active) {
              setWatchQuotes(current => ({ ...current, [message.data!.symbol]: message.data! }));
            }
          } catch { /* Ignore malformed or non-quote feed messages. */ }
        };
        socket.onclose = event => {
          if (!active) return;
          refreshToken = event.code === 4401;
          startFallbackPolling();
          reconnectTimer = window.setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30_000);
        };
        socket.onerror = () => socket?.close();
      } catch {
        if (!active) return;
        startFallbackPolling();
        reconnectTimer = window.setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30_000);
      }
    };
    void refresh();
    void connect();
    return () => {
      active = false;
      window.clearTimeout(reconnectTimer);
      window.clearInterval(pollTimer);
      socket?.close();
    };
  }, [category]);

  const visible = useMemo(() => {
    const matching = instruments.filter(item => item.category === category && `${item.name} ${item.symbol}`.toLowerCase().includes(search.toLowerCase()));
    if (category === "stocks") matching.sort((a, b) => (watchQuotes[b.symbol]?.quoteVolume24h ?? b.quoteVolume24h ?? 0) - (watchQuotes[a.symbol]?.quoteVolume24h ?? a.quoteVolume24h ?? 0));
    return matching;
  }, [instruments, category, search, watchQuotes]);
  const selected = instruments.find(item => item.symbol === symbol);
  const chartVwapDeviationSignals = useMemo(() => {
    const candleTimes = new Set(candles.map(candle => candle.time));
    return showVwapDeviation ? (indicators?.vwapDeviation.signals ?? []).filter(signal => candleTimes.has(signal.time)) : [];
  }, [candles, indicators, showVwapDeviation]);
  const chartTrendSignals = useMemo(() => {
    const supported = interval === "5m" && (selected?.category === "indices" || selected?.category === "forex");
    if (!showTrend || !supported) return [];
    const candleTimes = new Set(candles.map(candle => candle.time));
    return (indicators?.trend.signals ?? []).filter(signal => candleTimes.has(signal.time));
  }, [candles, indicators, interval, selected?.category, showTrend]);
  const trendAvailable = interval === "5m" && (selected?.category === "indices" || selected?.category === "forex");
  const bids = snapshot?.book.bids.slice(0, 10) ?? [];
  const asks = snapshot?.book.asks.slice(0, 10) ?? [];
  const maxDepth = Math.max(1, ...bids.map(row => row.total), ...asks.map(row => row.total));

  return <main className="shell">
    <header className="topbar">
      <Link href="/" className="tradfi-back" aria-label="Voltar ao Terminal Spot"><ArrowLeft size={15}/></Link>
      <div className="brand"><div className="logo">N</div><div><strong>NEXUS</strong><small>MARKET INTELLIGENCE</small></div></div>
      <nav className="nav"><Link href="/">Cripto Spot</Link><a className="active" aria-current="page" href="/tradfi">Índices & Forex</a><Link href="/macro">Macro Quant</Link></nav>
      <div className="topright"><div className="live"><span className={`dot ${snapshot ? "" : "tradfi-offline"}`}/>{snapshot ? `${selected?.category === "stocks" ? "HYPERLIQUID HIP-3" : "BINGX PERP"} · AO VIVO` : "CONECTANDO FEED"}</div><button className="iconbtn" title="Sair" aria-label="Sair" onClick={() => { window.location.href = "/api/auth/logout"; }}><LogOut size={16}/></button></div>
    </header>
    <div className="wrap tradfi-wrap">
      <div className="heading"><div><div className="eyebrow">BINGX + HYPERLIQUID · MERCADOS TRADFI</div><h1>Índices, Forex, Commodities & Stocks</h1></div><div className="headmeta"><span className="badge">PERPÉTUOS MARGEM USDT</span><span className="badge">CONTRATOS PÚBLICOS</span></div></div>
      <div className="tradfi-layout">
        <aside className="panel tradfi-sidebar">
          <div className="tradfi-tabs tradfi-tabs-stocks"><button className={category === "indices" ? "selected" : ""} onClick={() => setCategory("indices")}>ÍNDICES <b>{instruments.filter(item => item.category === "indices").length}</b></button><button className={category === "forex" ? "selected" : ""} onClick={() => setCategory("forex")}>FOREX <b>{instruments.filter(item => item.category === "forex").length}</b></button><button className={category === "commodities" ? "selected" : ""} onClick={() => setCategory("commodities")}>COMMOD. <b>{instruments.filter(item => item.category === "commodities").length}</b></button><button className={category === "stocks" ? "selected" : ""} onClick={() => { setCategory("stocks"); const first = instruments.find(item => item.category === "stocks"); if (first) setSymbol(first.symbol); }}>STOCKS <b>{instruments.filter(item => item.category === "stocks").length}</b></button></div>
          <label className="tradfi-search"><Search size={14}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Buscar instrumento"/></label>
          <div className="tradfi-list">{visible.map(item => { const quote = watchQuotes[item.symbol]; const change = quote?.changePercent; return <button key={item.symbol} className={`tradfi-instrument ${symbol === item.symbol ? "selected" : ""}`} onClick={() => setSymbol(item.symbol)}><span className="tradfi-instrument-name"><strong>{item.name}</strong><small>{item.symbol}</small></span><span className="tradfi-instrument-quote"><strong className="mono">{quote?.price != null ? fmtPrice(quote.price, item.pricePrecision ?? 2) : "—"}</strong><small className={`mono ${change == null ? "" : change >= 0 ? "pos" : "neg"}`}>{change != null ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : "calculando"}</small></span></button>; })}{visible.length === 0 && <div className="loading">Nenhum instrumento listado.</div>}</div>
          <div className="tradfi-source-note">{category === "stocks" ? "Ações perpétuas HIP-3 · variação de 24h conforme prevDayPx da Hyperliquid." : "Cotação atual · variação contra o último fechamento diário da BingX (UTC)."}</div>
        </aside>
        <section className="tradfi-main">
          <div className="panel tradfi-chart-panel">
            <div className="tradfi-assetbar"><div><div className="eyebrow">{selected?.category === "stocks" ? "HYPERLIQUID HIP-3 · DEX XYZ" : "BINGX · USDT-M PERPETUAL"}</div><h2>{selected?.name ?? symbol}</h2><small>{symbol} · {selected?.category === "forex" ? "PAR CAMBIAL" : selected?.category === "commodities" ? "COMMODITY" : selected?.category === "stocks" ? "AÇÃO PERPÉTUA" : "ÍNDICE"}</small></div><div className="tradfi-last"><strong>{snapshot ? fmtPrice(snapshot.ticker.price, selected?.pricePrecision ?? 2) : "—"}</strong><span className={(snapshot?.ticker.change24h ?? 0) >= 0 ? "pos" : "neg"}>{snapshot ? `${snapshot.ticker.change24h >= 0 ? "+" : ""}${snapshot.ticker.change24h.toFixed(2)}%` : "—"}</span></div></div>
            <div className="charttools"><span className="subtle">PERÍODO</span>{intervals.map(value => <button key={value} className={interval === value ? "active" : ""} onClick={() => setInterval(value)}>{value.toUpperCase()}</button>)}<span className="tradfi-chart-source">{selected?.category === "stocks" ? "HYPERLIQUID" : "BINGX"} · {fmtTime(snapshot?.receivedAt)}</span></div>
            <div className="charttools tradfi-indicator-tools" aria-label="Indicadores do gráfico">
              <button className={`volume-toggle ${showGarch ? "active" : ""}`} onClick={() => setShowGarch(value => !value)}>GARCH</button>
              <button className={`volume-toggle ${showHv ? "active" : ""}`} onClick={() => setShowHv(value => !value)}>HV252</button>
              <button className={`volume-toggle ${showVwaps ? "active" : ""}`} onClick={() => setShowVwaps(value => !value)}>VWAP D/S/M</button>
              <button className={`volume-toggle ${showPreviousLevels ? "active" : ""}`} onClick={() => setShowPreviousLevels(value => !value)}>H/L anteriores</button>
              <button className={`volume-toggle ${showVolumeAnomalies ? "active" : ""}`} onClick={() => setShowVolumeAnomalies(value => !value)}>Vol. alto</button>
              <button className={`volume-toggle ${showReversal ? "active" : ""}`} onClick={() => setShowReversal(value => !value)}>Reversão</button>
              <button className={`volume-toggle ${showBreakout ? "active" : ""}`} onClick={() => setShowBreakout(value => !value)}>Rompimento</button>
              <button className={`volume-toggle ${showVwapDeviation ? "active" : ""}`} onClick={() => setShowVwapDeviation(value => !value)}>DV</button>
              <button className={`volume-toggle ${showTrend && trendAvailable ? "active" : ""}`} onClick={() => setShowTrend(value => !value)} disabled={!trendAvailable} title={trendAvailable ? "Sinais TREND calculados em candles fechados de 5 minutos" : "TREND disponível em 5 minutos para índices e Forex"}>TREND</button>
              <span className="tradfi-chart-source">{indicators?.zones.status === "ready" ? `σ diário · ${indicators.zones.observations ?? 0} retornos` : "Aguardando histórico diário"}</span>
            </div>
            <div className="tradfi-chartarea">{loading ? <div className="loading">Carregando candles de {selected?.category === "stocks" ? "Hyperliquid" : "BingX"}…</div> : candles.length ? <MarketChart key={`${symbol}:${interval}`} candles={candles} symbol={symbol} interval={interval} zones={indicators?.zones ?? null} showGarch={showGarch} showHv={showHv} showVolumeAnomalies={showVolumeAnomalies} overlays={indicators?.overlays ?? null} showVwaps={showVwaps} showPreviousLevels={showPreviousLevels} signals={showReversal ? indicators?.reversal.signals ?? emptySignals : emptySignals} breakoutSignals={showBreakout ? indicators?.breakout.signals ?? emptySignals : emptySignals} trendSignals={chartTrendSignals} vwapDeviationSignals={chartVwapDeviationSignals}/> : <div className="chartfallback">Sem candles para a sessão atual.</div>}</div>
            <div className="tradfi-signal-summary">
              <div><span className="tradfi-signal-label">RADAR DE REVERSÃO · MODELO INDEPENDENTE</span><strong className={indicators?.reversal.latest?.direction === "BUY" ? "pos" : indicators?.reversal.latest?.direction === "SELL" ? "neg" : ""}>{indicators?.reversal.latest ? `${indicators.reversal.latest.direction} · ${indicators.reversal.latest.score}/100 · ${indicators.reversal.latest.source} ${indicators.reversal.latest.sigma}σ` : "Sem sinal confirmado recente"}</strong><small>{indicators?.reversal.latest?.pattern ?? "Bandas diárias GARCH/HV252 · mínimo 1σ · candle fechado"}</small></div>
              <div><span className="tradfi-signal-label">ROMPIMENTO APÓS COMPRESSÃO · MODELO INDEPENDENTE</span><strong className={indicators?.breakout.latest?.direction === "BUY" ? "pos" : indicators?.breakout.latest?.direction === "SELL" ? "neg" : ""}>{indicators?.breakout.latest ? `${indicators.breakout.latest.direction} · ${indicators.breakout.latest.volumeRatio.toFixed(2)}× volume mediano · ${indicators.breakout.latest.distanceAtr.toFixed(2)} ATR` : indicators?.breakout.status === "insufficient_history" ? `Histórico insuficiente · ${indicators.breakout.observedCandles ?? 0}/${indicators.breakout.requiredCandles ?? "—"} candles` : "Sem rompimento confirmado recente"}</strong><small>Compressão de volatilidade, fechamento além da faixa e expansão de volume</small></div>
              <div><span className="tradfi-signal-label">VWAP REJECTION · DV 1/2/3</span><strong className={indicators?.vwapDeviation.latest?.direction === "BUY" ? "pos" : indicators?.vwapDeviation.latest?.direction === "SELL" ? "neg" : ""}>{indicators?.vwapDeviation.latest ? `${indicators.vwapDeviation.latest.direction} · DV ${indicators.vwapDeviation.latest.sigma} · ${indicators.vwapDeviation.latest.modelLabels.join(" + ")}` : indicators?.vwapDeviation.status === "unsupported_interval" ? "Disponível em períodos intradiários" : indicators?.vwapDeviation.status === "insufficient_history" ? `Aguardando médias · ${indicators.vwapDeviation.observedCandles ?? 0} candles` : "Sem sinal confirmado recente"}</strong><small>{indicators?.vwapDeviation.latest ? `RVOL ${indicators.vwapDeviation.latest.volumeRatio.toFixed(2)}× · amplitude ${indicators.vwapDeviation.latest.relativeSpread.toFixed(2)}× · CLV ${indicators.vwapDeviation.latest.closeLocation.toFixed(2)}` : "Absorção, upthrust/shakeout e reteste; candles fechados"}</small><div className="tradfi-dv-settings"><label>RVOL alto<input type="number" min="1" max="10" step="0.1" value={dvVolumeHigh} onChange={event => setDvVolumeHigh(Math.min(10, Math.max(1, Number(event.target.value) || 1.8)))} /></label><label>RVOL baixo<input type="number" min="0" max="2" step="0.1" value={dvVolumeLow} onChange={event => setDvVolumeLow(Math.min(2, Math.max(0, Number(event.target.value) || 0.7)))} /></label><label>Range largo<input type="number" min="0.1" max="10" step="0.1" value={dvSpreadWide} onChange={event => setDvSpreadWide(Math.min(10, Math.max(0.1, Number(event.target.value) || 1.5)))} /></label><label>Range estreito<input type="number" min="0" max="2" step="0.1" value={dvSpreadNarrow} onChange={event => setDvSpreadNarrow(Math.min(2, Math.max(0, Number(event.target.value) || 0.7)))} /></label><label>CLV extremo<input type="number" min="0.5" max="1" step="0.05" value={dvCloseExtreme} onChange={event => setDvCloseExtreme(Math.min(1, Math.max(0.5, Number(event.target.value) || 0.7)))} /></label><label>Média<input type="number" min="5" max="100" step="1" value={dvAveragePeriods} onChange={event => setDvAveragePeriods(Math.min(100, Math.max(5, Number(event.target.value) || 20)))} /></label></div></div>
            </div>
            <div className="tradfi-stats"><div><span>MÁX. 24H</span><strong>{snapshot?.ticker.high24h ? fmtPrice(snapshot.ticker.high24h, selected?.pricePrecision ?? 2) : "—"}</strong></div><div><span>MÍN. 24H</span><strong>{snapshot?.ticker.low24h ? fmtPrice(snapshot.ticker.low24h, selected?.pricePrecision ?? 2) : "—"}</strong></div><div><span>VOLUME COTADO · 24H</span><strong>{snapshot ? `$${fmtCompact(snapshot.ticker.quoteVolume24h)}` : "—"}</strong></div><div><span>FONTE / STATUS</span><strong>{snapshot?.book.status === "live" ? selected?.category === "stocks" ? "HYPERLIQUID AO VIVO" : "BINGX AO VIVO" : "AGUARDANDO"}</strong></div></div>
          </div>
          <div className="tradfi-bottom">
            <section className="panel"><div className="panelhead"><span className="paneltitle">Times & trades · {selected?.category === "stocks" ? "Hyperliquid HIP-3" : "BingX Perp"}</span><span className={`badge ${snapshot?.trades.length ? "good" : ""}`}>{snapshot?.trades.length ?? 0} NEGÓCIOS</span></div><div className="tradfi-tape-head"><span>HORA · UTC−3</span><span>PREÇO</span><span>QTD.</span><span>AGRESSOR</span></div><div className="tradfi-tape">{snapshot?.trades.slice(0, 12).map(trade => <div className="tradfi-tape-row" key={trade.id}><span className="mono">{fmtTime(trade.time)}</span><span className="mono">{fmtPrice(trade.price, selected?.pricePrecision ?? 2)}</span><span className="mono">{trade.quantity}</span><strong className={trade.side === "buy" ? "pos" : "neg"}>{trade.side === "buy" ? "▲ COMPRA" : "▼ VENDA"}</strong></div>)}{!snapshot?.trades.length && <div className="loading">Aguardando negociações da sessão…</div>}</div></section>
            <section className="panel"><div className="panelhead"><span className="paneltitle">Book de ofertas · {selected?.category === "stocks" ? "Hyperliquid HIP-3" : "BingX Perp"}</span><span className={`badge ${snapshot?.book.status === "live" ? "good" : ""}`}>{snapshot?.book.status === "live" ? "AO VIVO" : "PAUSADO"}</span></div><div className="tradfi-book-summary"><span>SPREAD<strong>{snapshot ? fmtPrice(snapshot.book.spread, selected?.pricePrecision ?? 2) : "—"}</strong></span><span>IMBALANCE<strong>{snapshot ? snapshot.book.imbalance.toFixed(2) : "—"}</strong></span></div><div className="tradfi-book-head"><span>QTD.</span><span>PREÇO</span><span>QTD.</span></div><div className="tradfi-book">{asks.slice().reverse().map((row,index) => <div className="tradfi-book-row ask" key={`a-${index}`}><span className="mono">{fmtCompact(row.quantity)}</span><strong className="mono neg">{fmtPrice(row.price, selected?.pricePrecision ?? 2)}</strong><i style={{ "--depth": `${Math.min(100, row.total / maxDepth * 100)}%` } as React.CSSProperties}/><span/></div>)}<div className="tradfi-book-mid">{snapshot ? fmtPrice(snapshot.ticker.price, selected?.pricePrecision ?? 2) : "—"}</div>{bids.map((row,index) => <div className="tradfi-book-row bid" key={`b-${index}`}><span className="mono">{fmtCompact(row.quantity)}</span><strong className="mono pos">{fmtPrice(row.price, selected?.pricePrecision ?? 2)}</strong><i style={{ "--depth": `${Math.min(100, row.total / maxDepth * 100)}%` } as React.CSSProperties}/><span/></div>)}{!snapshot?.book.bids.length && <div className="loading">Book indisponível nesta sessão.</div>}</div></section>
          </div>
          <div className="tradfi-disclaimer"><Wifi size={12}/> Dados e indicadores calculados com OHLCV público do {selected?.category === "stocks" ? "perpétuo Hyperliquid HIP-3" : "perpétuo BingX"}. VWAP estimada por preço típico e volume dos candles; não equivale à VWAP de negócios individuais. Candles diários/níveis usam UTC e a sessão do instrumento pode ter pausas. Sinais são determinísticos, independentes do Spot e não enviam ordens.</div>
          {indicatorError && <div className="dataerror">{indicatorError}</div>}
          {error && <div className="dataerror">{error}</div>}
        </section>
      </div>
      <footer className="tradfi-footer"><Link href="/">← Voltar ao Terminal Spot</Link><span>BINGX + HYPERLIQUID PERPÉTUOS · APENAS ANÁLISE</span></footer>
    </div>
  </main>;
}
