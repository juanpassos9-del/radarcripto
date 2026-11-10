"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, LogOut, Search, Wifi } from "lucide-react";
import MarketChart from "@/components/market-chart";
import { fmtCompact, fmtPrice, fmtTime, getJson, type BreakoutSignalData, type Candle, type ChartOverlays, type ReversalSignalData, type Trade, type VolatilityZones } from "@/lib/market";

type Instrument = { symbol: string; name: string; category: "indices" | "forex" | "commodities"; minimumNotional?: number; pricePrecision?: number };
type TradfiMarket = {
  source: string; receivedAt: number;
  ticker: { price: number; change24h: number; high24h: number; low24h: number; quoteVolume24h: number; eventTime: number };
  book: { bids: { price: number; quantity: number; total: number }[]; asks: { price: number; quantity: number; total: number }[]; spread: number; imbalance: number; status: string };
  trades: Trade[];
};
type TradfiIndicators = { zones: VolatilityZones; overlays: ChartOverlays; reversal: ReversalSignalData; breakout: BreakoutSignalData; limitations: string[]; receivedAt: number };

const intervals = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const emptySignals: never[] = [];

export default function TradfiTerminal() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [category, setCategory] = useState<"indices" | "forex" | "commodities">("indices");
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
    }).catch(() => { if (active) setError("Não foi possível carregar os contratos públicos da BingX."); });
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
    const refresh = () => getJson<TradfiIndicators>(`/api/tradfi/indicators?symbol=${encodeURIComponent(symbol)}&interval=${interval}`)
      .then(data => { if (active) { setIndicators(data); setIndicatorError(""); } })
      .catch(() => { if (active) { setIndicators(null); setIndicatorError("Indicadores BingX indisponíveis ou com histórico insuficiente para este contrato/sessão."); } });
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol, interval]);

  useEffect(() => {
    let active = true;
    const refresh = () => getJson<TradfiMarket>(`/api/tradfi/market?symbol=${encodeURIComponent(symbol)}`)
      .then(data => { if (active) { setSnapshot(data); setError(""); } })
      .catch(() => { if (active) setError("Feed BingX pausado ou temporariamente indisponível para este contrato."); });
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol]);

  const visible = useMemo(() => instruments.filter(item => item.category === category && `${item.name} ${item.symbol}`.toLowerCase().includes(search.toLowerCase())), [instruments, category, search]);
  const selected = instruments.find(item => item.symbol === symbol);
  const bids = snapshot?.book.bids.slice(0, 10) ?? [];
  const asks = snapshot?.book.asks.slice(0, 10) ?? [];
  const maxDepth = Math.max(1, ...bids.map(row => row.total), ...asks.map(row => row.total));

  return <main className="shell">
    <header className="topbar">
      <Link href="/" className="tradfi-back" aria-label="Voltar ao Terminal Spot"><ArrowLeft size={15}/></Link>
      <div className="brand"><div className="logo">N</div><div><strong>NEXUS</strong><small>MARKET INTELLIGENCE</small></div></div>
      <nav className="nav"><Link href="/">Cripto Spot</Link><a className="active" aria-current="page" href="/tradfi">Índices & Forex</a></nav>
      <div className="topright"><div className="live"><span className={`dot ${snapshot ? "" : "tradfi-offline"}`}/>{snapshot ? "BINGX PERP · AO VIVO" : "CONECTANDO FEED"}</div><button className="iconbtn" title="Sair" aria-label="Sair" onClick={() => { window.location.href = "/api/auth/logout"; }}><LogOut size={16}/></button></div>
    </header>
    <div className="wrap tradfi-wrap">
      <div className="heading"><div><div className="eyebrow">BINGX · MERCADOS TRADFI</div><h1>Índices mundiais & Forex</h1></div><div className="headmeta"><span className="badge">PERPÉTUOS MARGEM USDT</span><span className="badge">CONTRATOS PÚBLICOS</span></div></div>
      <div className="tradfi-layout">
        <aside className="panel tradfi-sidebar">
          <div className="tradfi-tabs tradfi-tabs-three"><button className={category === "indices" ? "selected" : ""} onClick={() => setCategory("indices")}>ÍNDICES <b>{instruments.filter(item => item.category === "indices").length}</b></button><button className={category === "forex" ? "selected" : ""} onClick={() => setCategory("forex")}>FOREX <b>{instruments.filter(item => item.category === "forex").length}</b></button><button className={category === "commodities" ? "selected" : ""} onClick={() => setCategory("commodities")}>COMMODITIES <b>{instruments.filter(item => item.category === "commodities").length}</b></button></div>
          <label className="tradfi-search"><Search size={14}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Buscar instrumento"/></label>
          <div className="tradfi-list">{visible.map(item => <button key={item.symbol} className={`tradfi-instrument ${symbol === item.symbol ? "selected" : ""}`} onClick={() => setSymbol(item.symbol)}><span><strong>{item.name}</strong><small>{item.symbol}</small></span><span className={`mono ${((snapshot?.ticker.change24h ?? 0) >= 0) ? "pos" : "neg"}`}>{symbol === item.symbol && snapshot ? `${snapshot.ticker.change24h >= 0 ? "+" : ""}${snapshot.ticker.change24h.toFixed(2)}%` : "·"}</span></button>)}{visible.length === 0 && <div className="loading">Nenhum instrumento listado.</div>}</div>
          <div className="tradfi-source-note">Lista atualizada a partir dos contratos públicos habilitados pela BingX.</div>
        </aside>
        <section className="tradfi-main">
          <div className="panel tradfi-chart-panel">
            <div className="tradfi-assetbar"><div><div className="eyebrow">BINGX · USDT-M PERPETUAL</div><h2>{selected?.name ?? symbol}</h2><small>{symbol} · {selected?.category === "forex" ? "PAR CAMBIAL" : selected?.category === "commodities" ? "COMMODITY" : "ÍNDICE"}</small></div><div className="tradfi-last"><strong>{snapshot ? fmtPrice(snapshot.ticker.price, selected?.pricePrecision ?? 2) : "—"}</strong><span className={(snapshot?.ticker.change24h ?? 0) >= 0 ? "pos" : "neg"}>{snapshot ? `${snapshot.ticker.change24h >= 0 ? "+" : ""}${snapshot.ticker.change24h.toFixed(2)}%` : "—"}</span></div></div>
            <div className="charttools"><span className="subtle">PERÍODO</span>{intervals.map(value => <button key={value} className={interval === value ? "active" : ""} onClick={() => setInterval(value)}>{value.toUpperCase()}</button>)}<span className="tradfi-chart-source">BINGX · {fmtTime(snapshot?.receivedAt)}</span></div>
            <div className="charttools tradfi-indicator-tools" aria-label="Indicadores do gráfico">
              <button className={`volume-toggle ${showGarch ? "active" : ""}`} onClick={() => setShowGarch(value => !value)}>GARCH</button>
              <button className={`volume-toggle ${showHv ? "active" : ""}`} onClick={() => setShowHv(value => !value)}>HV252</button>
              <button className={`volume-toggle ${showVwaps ? "active" : ""}`} onClick={() => setShowVwaps(value => !value)}>VWAP D/S/M</button>
              <button className={`volume-toggle ${showPreviousLevels ? "active" : ""}`} onClick={() => setShowPreviousLevels(value => !value)}>H/L anteriores</button>
              <button className={`volume-toggle ${showVolumeAnomalies ? "active" : ""}`} onClick={() => setShowVolumeAnomalies(value => !value)}>Vol. alto</button>
              <button className={`volume-toggle ${showReversal ? "active" : ""}`} onClick={() => setShowReversal(value => !value)}>Reversão</button>
              <button className={`volume-toggle ${showBreakout ? "active" : ""}`} onClick={() => setShowBreakout(value => !value)}>Rompimento</button>
              <span className="tradfi-chart-source">{indicators?.zones.status === "ready" ? `σ diário · ${indicators.zones.observations ?? 0} retornos` : "Aguardando histórico diário"}</span>
            </div>
            <div className="tradfi-chartarea">{loading ? <div className="loading">Carregando candles da BingX…</div> : candles.length ? <MarketChart key={`${symbol}:${interval}`} candles={candles} symbol={symbol} interval={interval} zones={indicators?.zones ?? null} showGarch={showGarch} showHv={showHv} showVolumeAnomalies={showVolumeAnomalies} overlays={indicators?.overlays ?? null} showVwaps={showVwaps} showPreviousLevels={showPreviousLevels} signals={showReversal ? indicators?.reversal.signals ?? emptySignals : emptySignals} breakoutSignals={showBreakout ? indicators?.breakout.signals ?? emptySignals : emptySignals}/> : <div className="chartfallback">Sem candles para a sessão atual.</div>}</div>
            <div className="tradfi-signal-summary">
              <div><span className="tradfi-signal-label">RADAR DE REVERSÃO · MODELO INDEPENDENTE</span><strong className={indicators?.reversal.latest?.direction === "BUY" ? "pos" : indicators?.reversal.latest?.direction === "SELL" ? "neg" : ""}>{indicators?.reversal.latest ? `${indicators.reversal.latest.direction} · ${indicators.reversal.latest.score}/100 · ${indicators.reversal.latest.source} ${indicators.reversal.latest.sigma}σ` : "Sem sinal confirmado recente"}</strong><small>{indicators?.reversal.latest?.pattern ?? "Bandas diárias GARCH/HV252 · mínimo 1σ · candle fechado"}</small></div>
              <div><span className="tradfi-signal-label">ROMPIMENTO APÓS COMPRESSÃO · MODELO INDEPENDENTE</span><strong className={indicators?.breakout.latest?.direction === "BUY" ? "pos" : indicators?.breakout.latest?.direction === "SELL" ? "neg" : ""}>{indicators?.breakout.latest ? `${indicators.breakout.latest.direction} · ${indicators.breakout.latest.volumeRatio.toFixed(2)}× volume mediano · ${indicators.breakout.latest.distanceAtr.toFixed(2)} ATR` : indicators?.breakout.status === "insufficient_history" ? `Histórico insuficiente · ${indicators.breakout.observedCandles ?? 0}/${indicators.breakout.requiredCandles ?? "—"} candles` : "Sem rompimento confirmado recente"}</strong><small>Compressão de volatilidade, fechamento além da faixa e expansão de volume</small></div>
            </div>
            <div className="tradfi-stats"><div><span>MÁX. 24H</span><strong>{snapshot ? fmtPrice(snapshot.ticker.high24h, selected?.pricePrecision ?? 2) : "—"}</strong></div><div><span>MÍN. 24H</span><strong>{snapshot ? fmtPrice(snapshot.ticker.low24h, selected?.pricePrecision ?? 2) : "—"}</strong></div><div><span>VOLUME COTADO · 24H</span><strong>{snapshot ? `$${fmtCompact(snapshot.ticker.quoteVolume24h)}` : "—"}</strong></div><div><span>FONTE / STATUS</span><strong>{snapshot?.book.status === "live" ? "BINGX AO VIVO" : "AGUARDANDO"}</strong></div></div>
          </div>
          <div className="tradfi-bottom">
            <section className="panel"><div className="panelhead"><span className="paneltitle">Times & trades · BingX Perp</span><span className={`badge ${snapshot?.trades.length ? "good" : ""}`}>{snapshot?.trades.length ?? 0} NEGÓCIOS</span></div><div className="tradfi-tape-head"><span>HORA · UTC−3</span><span>PREÇO</span><span>QTD.</span><span>AGRESSOR</span></div><div className="tradfi-tape">{snapshot?.trades.slice(0, 12).map(trade => <div className="tradfi-tape-row" key={trade.id}><span className="mono">{fmtTime(trade.time)}</span><span className="mono">{fmtPrice(trade.price, selected?.pricePrecision ?? 2)}</span><span className="mono">{trade.quantity}</span><strong className={trade.side === "buy" ? "pos" : "neg"}>{trade.side === "buy" ? "▲ COMPRA" : "▼ VENDA"}</strong></div>)}{!snapshot?.trades.length && <div className="loading">Aguardando negociações da sessão…</div>}</div></section>
            <section className="panel"><div className="panelhead"><span className="paneltitle">Book de ofertas · BingX Perp</span><span className={`badge ${snapshot?.book.status === "live" ? "good" : ""}`}>{snapshot?.book.status === "live" ? "AO VIVO" : "PAUSADO"}</span></div><div className="tradfi-book-summary"><span>SPREAD<strong>{snapshot ? fmtPrice(snapshot.book.spread, selected?.pricePrecision ?? 2) : "—"}</strong></span><span>IMBALANCE<strong>{snapshot ? snapshot.book.imbalance.toFixed(2) : "—"}</strong></span></div><div className="tradfi-book-head"><span>QTD.</span><span>PREÇO</span><span>QTD.</span></div><div className="tradfi-book">{asks.slice().reverse().map((row,index) => <div className="tradfi-book-row ask" key={`a-${index}`}><span className="mono">{fmtCompact(row.quantity)}</span><strong className="mono neg">{fmtPrice(row.price, selected?.pricePrecision ?? 2)}</strong><i style={{ "--depth": `${Math.min(100, row.total / maxDepth * 100)}%` } as React.CSSProperties}/><span/></div>)}<div className="tradfi-book-mid">{snapshot ? fmtPrice(snapshot.ticker.price, selected?.pricePrecision ?? 2) : "—"}</div>{bids.map((row,index) => <div className="tradfi-book-row bid" key={`b-${index}`}><span className="mono">{fmtCompact(row.quantity)}</span><strong className="mono pos">{fmtPrice(row.price, selected?.pricePrecision ?? 2)}</strong><i style={{ "--depth": `${Math.min(100, row.total / maxDepth * 100)}%` } as React.CSSProperties}/><span/></div>)}{!snapshot?.book.bids.length && <div className="loading">Book indisponível nesta sessão.</div>}</div></section>
          </div>
          <div className="tradfi-disclaimer"><Wifi size={12}/> Dados e indicadores calculados com OHLCV público do perpétuo BingX. VWAP estimada por preço típico e volume dos candles; não equivale à VWAP de negócios individuais. Candles diários/níveis usam UTC e a sessão do instrumento pode ter pausas. Sinais são determinísticos, independentes do Spot e não enviam ordens.</div>
          {indicatorError && <div className="dataerror">{indicatorError}</div>}
          {error && <div className="dataerror">{error}</div>}
        </section>
      </div>
      <footer className="tradfi-footer"><Link href="/">← Voltar ao Terminal Spot</Link><span>BINGX PERPÉTUOS · APENAS ANÁLISE</span></footer>
    </div>
  </main>;
}
