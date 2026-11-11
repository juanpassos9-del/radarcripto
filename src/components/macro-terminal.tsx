"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Activity, ArrowLeft, ArrowUpRight, Database, Gauge, LogOut, Radio, Waves } from "lucide-react";
import { fmtPrice, fmtTime, getJson, getMarketWsToken } from "@/lib/market";

type MacroAsset = {
  key: string; symbol: string; label: string; category: string; color: string;
  available: boolean; price: number | null; change5mPct: number | null; change30mPct: number | null;
  acceleration5mPp: number | null; change5mBps: number | null; change30mBps: number | null; acceleration5mBps: number | null;
  unit?: string; source?: string; z5m: number | null; sampleCount: number; ageMs: number | null; status: string;
};
type MacroSeries = { key: string; label: string; color: string; values: { time: number; value: number }[] };
type MacroSnapshot = {
  asOf: number; source: string; status: string;
  storage: { engine: string; durable: boolean; retentionDays: number; oldestSample: number | null; trackedAssets: number; assetsWithHistory: number };
  regime: { label: string; score: number | null; coverage: number; components: { key: string; label: string; value: number | null; weight: number }[] };
  usd: { state: string; strengthScore: number | null };
  oil: { state: string; pressureScore: number | null };
  assets: MacroAsset[]; accelerators: MacroAsset[];
  relationships: { left: string; right: string; label: string; correlation: number | null; observations: number; windowMinutes: number }[];
  insights: { tone: string; title: string; detail: string }[];
  outlook: { group: string; bias: string; title: string; detail: string }[];
  chart: { windowMinutes: number; series: MacroSeries[] };
  yieldCurve?: { spreadBps: number | null; change5mBps: number | null; longSpreadBps?: number | null; longChange5mBps?: number | null; status?: string; label?: string };
  method: string; limitations: string[];
};

function MacroLineChart({ series }: { series: MacroSeries[] }) {
  const width = 1000, height = 330, left = 46, right = 18, top = 18, bottom = 26;
  const all = series.flatMap(item => item.values);
  if (all.length < 4) return <div className="macro-chart-empty">Aguardando amostras sincronizadas da BingX e Hyperliquid para desenhar as relações de preço.</div>;
  const times = all.map(point => point.time), values = all.map(point => point.value);
  const minTime = Math.min(...times), maxTime = Math.max(...times);
  const minValue = Math.min(0, ...values), maxValue = Math.max(0, ...values);
  const pad = Math.max((maxValue - minValue) * 0.12, 0.005);
  const yMin = minValue - pad, yMax = maxValue + pad;
  const x = (time: number) => left + (time - minTime) / Math.max(1, maxTime - minTime) * (width - left - right);
  const y = (value: number) => top + (yMax - value) / Math.max(1e-8, yMax - yMin) * (height - top - bottom);
  const ticks = Array.from({ length: 5 }, (_, index) => yMin + (yMax - yMin) * index / 4);
  return <div className="macro-chart-wrap">
    <svg className="macro-line-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="Variação percentual normalizada dos ativos nos últimos 60 minutos">
      {ticks.map((tick, index) => <g key={index}><line x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} className={Math.abs(tick) < 1e-6 ? "macro-zero-line" : "macro-grid-line"}/><text x={left - 8} y={y(tick) + 3} textAnchor="end" className="macro-axis-label">{tick.toFixed(2)}%</text></g>)}
      <line x1={left} x2={left} y1={top} y2={height - bottom} className="macro-grid-line"/>
      {series.map(item => <polyline key={item.key} fill="none" stroke={item.color} strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round" filter="url(#macroGlow)" points={item.values.map(point => `${x(point.time)},${y(point.value)}`).join(" ")}><title>{item.label}</title></polyline>)}
      <defs><filter id="macroGlow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.2" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
      <text x={left} y={height - 5} className="macro-axis-label">{fmtTime(minTime)}</text><text x={width - right} y={height - 5} textAnchor="end" className="macro-axis-label">{fmtTime(maxTime)}</text>
    </svg>
    <div className="macro-legend">{series.map(item => <span key={item.key}><i style={{ background: item.color, boxShadow: `0 0 8px ${item.color}` }}/>{item.label}</span>)}</div>
  </div>;
}

export default function MacroTerminal() {
  const [snapshot, setSnapshot] = useState<MacroSnapshot | null>(null);
  const [quotes, setQuotes] = useState<Record<string, number>>({});
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const [showMethod, setShowMethod] = useState(false);

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let retryTimer = 0;
    let refreshTimer = 0;
    let delay = 1000;
    let refreshToken = false;
    const refresh = () => getJson<MacroSnapshot>("/api/tradfi/macro")
      .then(data => { if (active) { setSnapshot(data); setError(""); } })
      .catch((cause: unknown) => { if (active) setError(cause instanceof Error ? `Falha ao carregar o Macro Quant: ${cause.message}` : "Falha ao carregar o Macro Quant. Tente novamente."); });
    const connect = async () => {
      try {
        const token = await getMarketWsToken(refreshToken);
        refreshToken = false;
        if (!active) return;
        const apiOrigin = process.env.NEXT_PUBLIC_MARKET_API_URL?.replace(/\/+$/, "") || window.location.origin;
        const url = new URL("/ws/tradfi/macro", apiOrigin.replace(/^http/, "ws"));
        socket = new WebSocket(url, ["nexus-market-v1", token]);
        socket.onopen = () => { setConnected(true); delay = 1000; };
        socket.onmessage = event => {
          try {
            const message = JSON.parse(event.data) as { type?: string; data?: { symbol?: string; price?: number } };
            if (message.type === "quote" && message.data?.symbol && typeof message.data.price === "number") {
              setQuotes(current => ({ ...current, [message.data!.symbol!]: message.data!.price! }));
            }
          } catch { /* Ignore non-quote feed messages. */ }
        };
        socket.onclose = event => {
          if (!active) return;
          setConnected(false);
          refreshToken = event.code === 4401;
          retryTimer = window.setTimeout(connect, delay);
          delay = Math.min(delay * 2, 30_000);
        };
        socket.onerror = () => socket?.close();
      } catch {
        if (!active) return;
        setConnected(false);
        retryTimer = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 30_000);
      }
    };
    void refresh();
    void connect();
    refreshTimer = window.setInterval(refresh, 15_000);
    return () => { active = false; window.clearTimeout(retryTimer); window.clearInterval(refreshTimer); socket?.close(); };
  }, []);

  const chartSeries = useMemo(() => (snapshot?.chart.series ?? []).map(series => {
    const latestPrice = quotes[snapshot?.assets.find(asset => asset.key === series.key)?.symbol ?? ""];
    if (!latestPrice || !series.values.length) return series;
    const asset = snapshot?.assets.find(item => item.key === series.key);
    const basisPrice = asset?.price;
    if (!basisPrice) return series;
    const last = series.values[series.values.length - 1];
    const value = last.value + (latestPrice / basisPrice - 1) * 100;
    return { ...series, values: [...series.values.slice(0, -1), { time: Date.now(), value }] };
  }), [snapshot, quotes]);
  const regimeClass = snapshot?.regime.label === "RISK-ON" ? "positive" : snapshot?.regime.label === "RISK-OFF" ? "negative" : "neutral";
  const markerLeft = `${Math.max(0, Math.min(100, ((snapshot?.regime.score ?? 0) + 100) / 2))}%`;
  return <main className="shell macro-shell">
    <header className="topbar">
      <Link href="/tradfi" className="tradfi-back" aria-label="Voltar a Índices e Forex"><ArrowLeft size={15}/></Link>
      <div className="brand"><div className="logo">N</div><div><strong>NEXUS</strong><small>MACRO INTELLIGENCE</small></div></div>
      <nav className="nav"><Link href="/">Cripto Spot</Link><Link href="/tradfi">Índices & Forex</Link><a className="active" aria-current="page" href="/macro">Macro Quant</a></nav>
      <div className="topright"><div className="live"><span className={`dot ${connected ? "" : "tradfi-offline"}`}/>{connected ? "MACRO · STREAM API AO VIVO" : "RECONECTANDO API"}</div><button className="iconbtn" title="Sair" aria-label="Sair" onClick={() => { window.location.href = "/api/auth/logout"; }}><LogOut size={16}/></button></div>
    </header>
    <div className="wrap macro-wrap">
      <div className="macro-heading"><div><div className="eyebrow">NEXUS · REGIME & FLUXO MACRO</div><h1>Macro Quant <span>01</span></h1><p>Leitura quantitativa de moedas, índices, commodities, juros e ETFs sintéticos.</p></div><div className="macro-clock"><Radio size={13}/><span>{snapshot ? `ATUALIZADO ${fmtTime(snapshot.asOf)}` : error ? "API INDISPONÍVEL" : "INICIANDO FEED"}</span><i className={connected ? "on" : ""}/></div></div>
      {error && <div className="dataerror macro-error-banner" role="alert"><span>{error}</span>{!snapshot && <button onClick={() => window.location.reload()}>Tentar novamente</button>}</div>}

      <section className="macro-overview">
        <article className={`macro-regime-card ${regimeClass}`}>
          <div className="macro-card-kicker"><Gauge size={14}/> REGIME DE MERCADO <span>{snapshot?.regime.coverage ?? 0}% COBERTURA</span></div>
          <div className="macro-regime-main"><strong>{snapshot?.regime.label ?? "AQUECENDO"}</strong><b>{snapshot?.regime.score != null ? `${snapshot.regime.score > 0 ? "+" : ""}${snapshot.regime.score}` : "—"}<small>/100</small></b></div>
          <div className="macro-gauge"><i style={{ left: markerLeft }}/></div>
          <div className="macro-gauge-labels"><span>RISK-OFF</span><span>MISTO</span><span>RISK-ON</span></div>
          <div className="macro-components">{snapshot?.regime.components.map(component => <div key={component.key}><span>{component.label}</span><strong className={component.value == null ? "muted" : component.value >= 0 ? "pos" : "neg"}>{component.value == null ? "—" : `${component.value > 0 ? "+" : ""}${component.value}`}</strong></div>)}</div>
        </article>
        <article className="macro-metric-card usd"><div className="macro-card-kicker"><Waves size={14}/> PRESSÃO NO DÓLAR</div><strong>{snapshot?.usd.state ?? "CALCULANDO"}</strong><div className="macro-metric-value">{snapshot?.usd.strengthScore == null ? "—" : `${snapshot.usd.strengthScore > 0 ? "+" : ""}${snapshot.usd.strengthScore}`}<small>/100</small></div><p>DXY + cesta EUR, GBP, AUD, NZD e JPY</p></article>
        <article className="macro-metric-card oil"><div className="macro-card-kicker"><Activity size={14}/> PRESSÃO DE ENERGIA</div><strong>{snapshot?.oil.state ?? "CALCULANDO"}</strong><div className="macro-metric-value">{snapshot?.oil.pressureScore == null ? "—" : `${snapshot.oil.pressureScore > 0 ? "+" : ""}${snapshot.oil.pressureScore}`}<small>/100</small></div><p>Brent & WTI · proxy de custo e inflação</p></article>
        <article className="macro-metric-card storage"><div className="macro-card-kicker"><Database size={14}/> HISTÓRICO SINCRONIZADO</div><strong>{snapshot?.storage.assetsWithHistory ?? 0}<small> / {snapshot?.storage.trackedAssets ?? 19} ativos</small></strong><div className="macro-storage-line">{snapshot?.storage.engine.toUpperCase() ?? "—"}<span className={snapshot?.storage.durable ? "pos" : "warning"}>{snapshot?.storage.durable ? "DURÁVEL" : "VOLÁTIL"}</span></div><p>1 amostra/min · retenção {snapshot?.storage.retentionDays ?? 7} dias</p></article>
      </section>

      <section className="macro-yield-strip" aria-label="Curva de juros dos Treasuries">
        {["UST2Y", "UST10Y", "UST30Y"].map(key => {
          const asset = snapshot?.assets.find(item => item.key === key);
          const movement = asset?.change5mBps;
          const fallback = key === "UST2Y" ? "Treasury 2Y · yield" : key === "UST10Y" ? "Treasury 10Y · yield" : "Treasury 30Y · yield";
          return <article key={key}><small>{asset?.label ?? fallback}</small><strong>{asset?.price == null ? "AQUECENDO" : `${asset.price.toFixed(3)}%`}</strong><span className={movement == null ? "muted" : movement >= 0 ? "neg" : "pos"}>{movement == null ? "—" : `${movement > 0 ? "+" : ""}${movement.toFixed(2)} bps · 5 min`}</span></article>;
        })}
        <article><small>INCLINAÇÃO · 10Y−2Y</small><strong>{snapshot?.yieldCurve?.spreadBps == null ? "AQUECENDO" : `${snapshot.yieldCurve.spreadBps.toFixed(2)} bps`}</strong><span>{snapshot?.yieldCurve?.change5mBps == null ? "— · sem base sincronizada" : `${snapshot.yieldCurve.change5mBps > 0 ? "+" : ""}${snapshot.yieldCurve.change5mBps.toFixed(2)} bps · 5 min`}</span></article>
        <article><small>INCLINAÇÃO LONGA · 30Y−10Y</small><strong>{snapshot?.yieldCurve?.longSpreadBps == null ? "AQUECENDO" : `${snapshot.yieldCurve.longSpreadBps.toFixed(2)} bps`}</strong><span>{snapshot?.yieldCurve?.longChange5mBps == null ? "— · sem base sincronizada" : `${snapshot.yieldCurve.longChange5mBps > 0 ? "+" : ""}${snapshot.yieldCurve.longChange5mBps.toFixed(2)} bps · 5 min`}</span></article>
      </section>

      <div className="macro-grid">
        <section className="panel macro-chart-panel">
          <div className="panelhead macro-panel-head"><span className="paneltitle">MAPA DE PREÇOS · VARIAÇÃO NORMALIZADA</span><span className="macro-tag"><i/> ÚLTIMOS 60 MIN</span></div>
          <p className="macro-chart-subtitle">Cada linha começa em 0% no início da janela. A escala comum evidencia divergência e confluência entre os ativos.</p>
          <MacroLineChart series={chartSeries}/>
          <div className="macro-chart-foot"><span>HORÁRIO SÃO PAULO · {snapshot ? fmtTime(snapshot.asOf) : "—"}</span><span>{snapshot?.chart.series.length ?? 0} SÉRIES COM COBERTURA</span></div>
        </section>
        <section className="panel macro-insight-panel">
          <div className="panelhead"><span className="paneltitle">LEITURA DO REGIME</span><span className="macro-tag"><i/></span></div>
          <div className="macro-insights">{snapshot?.insights.map((insight, index) => <article className={`macro-insight ${insight.tone}`} key={`${insight.title}-${index}`}><span className="macro-insight-mark"><ArrowUpRight size={15}/></span><div><strong>{insight.title}</strong><p>{insight.detail}</p></div></article>) ?? <div className="loading">Carregando fatores macro observados…</div>}</div>
          <div className="macro-insight-note">Viés estimado por preços recentes; confirme com o comportamento do próprio índice e com o horário de negociação.</div>
        </section>
      </div>

      <div className="macro-grid macro-lower">
        <section className="panel macro-assets-panel">
          <div className="panelhead"><span className="paneltitle">ACELERAÇÃO · ÚLTIMOS 5 MINUTOS</span><span className="macro-tag">VS. 5 MIN ANTERIORES</span></div>
          <div className="macro-table-head"><span>ATIVO / FATOR</span><span>PREÇO</span><span>5 MIN</span><span>30 MIN</span><span>ACELERAÇÃO</span></div>
          <div className="macro-assets-list">{snapshot?.assets.filter(item => item.available).map(asset => {
            const price = quotes[asset.symbol] ?? asset.price;
            const isYield = asset.unit === "yield_pct";
            const change5m = isYield ? asset.change5mBps : asset.change5mPct;
            const change30m = isYield ? asset.change30mBps : asset.change30mPct;
            const acceleration = isYield ? asset.acceleration5mBps : asset.acceleration5mPp;
            const unit = isYield ? " bps" : "%";
            return <div className="macro-asset-row" key={asset.key}><span className="macro-asset-label"><i style={{ background: asset.color }}/><strong>{asset.label}</strong><small>{asset.source ?? "BingX USDT-M"} · {asset.status === "live" ? "AO VIVO" : "HISTÓRICO"}</small></span><strong className="mono">{price == null ? "—" : isYield ? `${price.toFixed(3)}%` : fmtPrice(price, asset.category === "fx" ? 4 : 2)}</strong><span className={`mono ${change5m == null ? "muted" : change5m >= 0 ? "pos" : "neg"}`}>{change5m == null ? "—" : `${change5m > 0 ? "+" : ""}${change5m.toFixed(isYield ? 2 : 3)}${unit}`}</span><span className={`mono ${change30m == null ? "muted" : change30m >= 0 ? "pos" : "neg"}`}>{change30m == null ? "—" : `${change30m > 0 ? "+" : ""}${change30m.toFixed(isYield ? 2 : 3)}${unit}`}</span><span className={`mono ${acceleration == null ? "muted" : acceleration >= 0 ? "pos" : "neg"}`}>{acceleration == null ? "AQUECENDO" : `${acceleration > 0 ? "+" : ""}${acceleration.toFixed(isYield ? 2 : 3)}${isYield ? " bps" : " pp"}`}</span></div>;
          }) ?? <div className="loading">Aguardando histórico sincronizado…</div>}</div>
          <div className="macro-table-foot">Yields Hyperliquid: nível em %, mudanças em pontos-base. Outros ativos: retorno percentual. Aceleração compara os últimos 5 min com os 5 min anteriores.</div>
        </section>
        <section className="panel macro-relationships-panel">
          <div className="panelhead"><span className="paneltitle">RELAÇÕES OBSERVADAS · PEARSON</span><span className="macro-tag">30 MIN</span></div>
          <div className="macro-corr-list">{snapshot?.relationships.map(item => <div className="macro-corr-row" key={item.label}><span>{item.label}<small>{item.observations} pontos sincronizados</small></span><div className="macro-corr-meter"><i style={{ left: `${((item.correlation ?? 0) + 1) * 50}%` }}/></div><strong className={item.correlation == null ? "muted" : item.correlation <= -0.35 ? "pos" : item.correlation >= 0.35 ? "neg" : ""}>{item.correlation == null ? "AQUECENDO" : item.correlation.toFixed(2)}</strong></div>) ?? <div className="loading">Correlação disponível após acumular amostras.</div>}</div>
          <div className="macro-corr-note">Correlação positiva indica movimento conjunto; negativa indica movimento oposto. Amostras curtas podem mudar rapidamente.</div>
        </section>
      </div>

      <section className="macro-outlook-section">
        <div className="macro-section-heading"><div><div className="eyebrow">MAPA DE EXPOSIÇÃO · CURTO PRAZO</div><h2>Quem pode se beneficiar e o que merece atenção</h2></div><span>VIÉS OBSERVADO · NÃO É PREVISÃO</span></div>
        <div className="macro-outlook-grid">{snapshot?.outlook.map(item => <article className={`macro-outlook-card ${item.bias}`} key={item.group}><small>{item.group}</small><strong>{item.title}</strong><p>{item.detail}</p><span>{item.bias === "favorable" ? "MOMENTUM FAVORÁVEL" : item.bias === "caution" ? "PRESSÃO / CAUTELA" : "OBSERVAR CONFIRMAÇÃO"}</span></article>) ?? <div className="loading">Aguardando fatores alinhados para estimar exposição…</div>}</div>
      </section>

      <section className="macro-method">
        <button onClick={() => setShowMethod(value => !value)}><Activity size={13}/>{showMethod ? "OCULTAR MÉTODO E LIMITAÇÕES" : "MÉTODO, COBERTURA E LIMITAÇÕES"}<span>{snapshot?.storage.engine.toUpperCase()} · {snapshot?.storage.durable ? "RETENÇÃO DURÁVEL" : "ARMAZENAMENTO NÃO DURÁVEL"}</span></button>
        {showMethod && <div className="macro-method-content"><p>{snapshot?.method}</p><ul>{snapshot?.limitations.map(item => <li key={item}>{item}</li>)}</ul><p>Histórico: {snapshot?.storage.oldestSample ? `desde ${fmtTime(snapshot.storage.oldestSample)}` : "a coleta está começando"}. Fonte: {snapshot?.source}.</p></div>}
      </section>
      <footer className="tradfi-footer"><Link href="/tradfi">← Índices, Forex & Commodities</Link><span>MACRO QUANT · MODELO OBSERVACIONAL · SEM ORDENS</span></footer>
    </div>
  </main>;
}
