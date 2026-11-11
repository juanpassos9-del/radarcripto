"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowLeft, ArrowUpRight, Bell, BellRing, Database, Gauge, LogOut, Radio, Waves } from "lucide-react";
import { fmtCompact, fmtPrice, fmtTime, getJson, getMarketWsToken } from "@/lib/market";

type MacroAsset = {
  key: string; symbol: string; label: string; category: string; color: string;
  available: boolean; price: number | null; change5mPct: number | null; change30mPct: number | null;
  acceleration5mPp: number | null; change5mBps: number | null; change30mBps: number | null; acceleration5mBps: number | null;
  unit?: string; source?: string; quoteVolume24h?: number | null; z5m: number | null;
  windows?: Record<string, { change: number | null; velocityPerMinute: number | null; z: number | null; direction: string }>;
  sampleCount: number; ageMs: number | null; status: string;
};
type RotationPoint = { asset: MacroAsset; x: number; y: number; strength: number; acceleration: number };
type MacroSeries = { key: string; label: string; color: string; values: { time: number; value: number }[] };
type BreadthGroup = { label: string; advancing: number; neutral: number; declining: number; available: number; total: number; score: number | null; coverage: number };
type AlertPreferences = { enabled: boolean; regime: boolean; exceptionalMove: boolean; divergence: boolean; multiClass: boolean; zThreshold: number; cooldownMinutes: number };
type MacroAlert = { id: string; createdAt: number; title: string; detail: string; tone: "positive" | "risk" | "warning" };
const DEFAULT_ALERT_PREFERENCES: AlertPreferences = { enabled: true, regime: true, exceptionalMove: true, divergence: true, multiClass: true, zThreshold: 2, cooldownMinutes: 10 };
const CHART_SERIES_COLORS: Record<string, string> = {
  DXY: "#45E1D0",
  EURUSD: "#B78CFF",
  SP500: "#FFD166",
  NASDAQ100: "#4DA3FF",
  WTI: "#FF6B6B",
  GOLD: "#F4E04D",
  COPPER: "#D88B55",
  EWZ: "#55E6A5",
  DRAM: "#75D7FF",
  SOXL: "#FF80BF",
};
type MacroSnapshot = {
  asOf: number; source: string; status: string;
  storage: { engine: string; durable: boolean; retentionDays: number; oldestSample: number | null; trackedAssets: number; assetsWithHistory: number };
  regime: { label: string; candidate: string; qualified?: boolean; pendingSamples?: number; requiredSamples?: number; transitionPending?: boolean; score: number | null; coverage: number; components: { key: string; label: string; value: number | null; weight: number }[] };
  breadth: { indices: BreadthGroup; usd: BreadthGroup; energy: BreadthGroup; metals: BreadthGroup; yields: BreadthGroup; confirmation: { direction: string; classes: string[]; votes: Record<string, number | null> } };
  usd: { state: string; strengthScore: number | null };
  oil: { state: string; pressureScore: number | null };
  assets: MacroAsset[]; accelerators: MacroAsset[];
  divergences: { id: string; leftKey: string; rightKey: string; label: string; leftZ5m: number; rightZ5m: number; strength: number; note: string; persistenceMinutes?: number; confirmed?: boolean }[];
  insights: { tone: string; title: string; detail: string }[];
  outlook: { group: string; bias: string; title: string; detail: string }[];
  chart: { windowMinutes: number; series: MacroSeries[]; dailyWindowHours?: number; dailySeries?: MacroSeries[] };
  yieldCurve?: { spreadBps: number | null; change5mBps: number | null; longSpreadBps?: number | null; longChange5mBps?: number | null; status?: string; label?: string };
  method: string; limitations: string[];
};

function MacroLineChart({ series, label, emptyMessage }: { series: MacroSeries[]; label: string; emptyMessage: string }) {
  const width = 1000, height = 330, left = 46, right = 18, top = 18, bottom = 26;
  const all = series.flatMap(item => item.values);
  if (all.length < 4) return <div className="macro-chart-empty">{emptyMessage}</div>;
  const times = all.map(point => point.time), values = all.map(point => point.value);
  const minTime = Math.min(...times), maxTime = Math.max(...times);
  const minValue = Math.min(0, ...values), maxValue = Math.max(0, ...values);
  const pad = Math.max((maxValue - minValue) * 0.12, 0.005);
  const yMin = minValue - pad, yMax = maxValue + pad;
  const x = (time: number) => left + (time - minTime) / Math.max(1, maxTime - minTime) * (width - left - right);
  const y = (value: number) => top + (yMax - value) / Math.max(1e-8, yMax - yMin) * (height - top - bottom);
  const ticks = Array.from({ length: 5 }, (_, index) => yMin + (yMax - yMin) * index / 4);
  const formatTime = (time: number) => label === "últimas 24 horas"
    ? new Intl.DateTimeFormat("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(time)
    : fmtTime(time);
  return <div className="macro-chart-wrap">
    <svg className="macro-line-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`Variação percentual normalizada dos ativos: ${label}`}>
      {ticks.map((tick, index) => <g key={index}><line x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} className={Math.abs(tick) < 1e-6 ? "macro-zero-line" : "macro-grid-line"}/><text x={left - 8} y={y(tick) + 3} textAnchor="end" className="macro-axis-label">{tick.toFixed(2)}%</text></g>)}
      <line x1={left} x2={left} y1={top} y2={height - bottom} className="macro-grid-line"/>
      {series.map(item => <polyline key={item.key} fill="none" stroke={item.color} strokeWidth="2.6" strokeLinejoin="round" strokeLinecap="round" points={item.values.map(point => `${x(point.time)},${y(point.value)}`).join(" ")}><title>{item.label}</title></polyline>)}
      <text x={left} y={height - 5} className="macro-axis-label">{formatTime(minTime)}</text><text x={width - right} y={height - 5} textAnchor="end" className="macro-axis-label">{formatTime(maxTime)}</text>
    </svg>
    <div className="macro-legend">{series.map(item => <span key={item.key}><i style={{ background: item.color, boxShadow: `0 0 8px ${item.color}` }}/>{item.label}</span>)}</div>
  </div>;
}

function MarketRotationRadar({ points, category, onCategoryChange }: { points: RotationPoint[]; category: string; onCategoryChange: (value: string) => void }) {
  const width = 900, height = 500, cx = width / 2, cy = height / 2, radiusX = 315, radiusY = 190;
  const categories = [
    ["all", "TODAS"], ["index", "ÍNDICES"], ["fx", "MOEDAS"], ["energy", "ENERGIA"],
    ["metal", "METAIS"], ["rates", "JUROS"], ["equity", "ETFs"], ["bond", "TÍTULOS"],
  ];
  const filtered = points.filter(point => category === "all" || point.asset.category === category);
  const x = (value: number) => cx + value * radiusX;
  const y = (value: number) => cy - value * radiusY;
  return <section className="panel macro-rotation-panel">
    <div className="panelhead macro-rotation-head"><div><span className="paneltitle">MARKET ROTATION RADAR</span><small>FORÇA RELATIVA × ACELERAÇÃO · Z-SCORE</small></div><div className="macro-rotation-filters" role="group" aria-label="Filtrar classe de ativos">
      {categories.map(([key, label]) => <button type="button" key={key} aria-pressed={category === key} onClick={() => onCategoryChange(key)}>{label}</button>)}
    </div></div>
    <div className="macro-rotation-chart-wrap">
      <svg className="macro-rotation-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Radar de força relativa e aceleração dos ativos macro">
        {[1 / 3, 2 / 3, 1].map((scale, index) => <ellipse key={index} cx={cx} cy={cy} rx={radiusX * scale} ry={radiusY * scale} className="macro-rotation-ring"/>)}
        <line x1={cx - radiusX} x2={cx + radiusX} y1={cy} y2={cy} className="macro-rotation-axis"/><line x1={cx} x2={cx} y1={cy - radiusY} y2={cy + radiusY} className="macro-rotation-axis"/>
        <text x={cx} y={30} textAnchor="middle" className="macro-rotation-quadrant">ACELERANDO</text>
        <text x={cx} y={height - 8} textAnchor="middle" className="macro-rotation-quadrant">DESACELERANDO</text>
        <text x={18} y={cy + 4} className="macro-rotation-quadrant">FRACO</text><text x={width - 18} y={cy + 4} textAnchor="end" className="macro-rotation-quadrant">FORTE</text>
        <text x={cx + radiusX - 5} y={cy + 18} textAnchor="end" className="macro-rotation-axis-label">FORÇA →</text>
        {filtered.map(({ asset, strength, acceleration, x: px, y: py }) => <g key={asset.key} className="macro-rotation-point">
          <circle cx={x(px)} cy={y(py)} r={5.5} fill={asset.color} stroke="#0b1218" strokeWidth="2"><title>{`${asset.label} · Força ${strength >= 0 ? "+" : ""}${strength.toFixed(2)}σ · aceleração ${acceleration >= 0 ? "+" : ""}${acceleration.toFixed(2)}σ · ${asset.status === "live" ? "ao vivo" : "histórico"} · amostras ${asset.sampleCount}`}</title></circle>
          <text x={x(px) + (px < 0 ? -9 : 9)} y={y(py) - 8} textAnchor={px < 0 ? "end" : "start"} fill={asset.color}>{asset.key}</text>
        </g>)}
      </svg>
      {!filtered.length && <div className="macro-rotation-empty">Aguardando amostras sincronizadas suficientes para esta classe.</div>}
    </div>
    <div className="macro-rotation-foot"><span>{filtered.length} / {points.length} ativos com força e aceleração válidas</span><span>FORTE = momentum padronizado positivo · FRACO = negativo</span></div>
    <p className="macro-rotation-note">Força = média do Z-score de 15 e 60 min; aceleração = Z-score de 5 min menos o de 15 min. Cada ativo é comparado à própria volatilidade. Para Treasuries, a direção representa alta/queda do yield. É uma leitura de preço, não de fluxo financeiro comprovado nem previsão.</p>
  </section>;
}

export default function MacroTerminal() {
  const [snapshot, setSnapshot] = useState<MacroSnapshot | null>(null);
  const [quotes, setQuotes] = useState<Record<string, number>>({});
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const [showMethod, setShowMethod] = useState(false);
  const [rotationCategory, setRotationCategory] = useState("all");
  const [alertPreferences, setAlertPreferences] = useState<AlertPreferences>(DEFAULT_ALERT_PREFERENCES);
  const [alertHistory, setAlertHistory] = useState<MacroAlert[]>([]);
  const [showAlertSettings, setShowAlertSettings] = useState(true);
  const [alertsReady, setAlertsReady] = useState(false);
  const [notificationSupported, setNotificationSupported] = useState(false);
  const [notificationStatus, setNotificationStatus] = useState("default");
  const previousRegime = useRef<string | null>(null);
  const previousConfirmation = useRef<string | null>(null);
  const alertCooldowns = useRef<Record<string, number>>({});

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

  useEffect(() => {
    try {
      const savedPreferences = localStorage.getItem("nexusMacroAlertPreferences:v1");
      const savedHistory = localStorage.getItem("nexusMacroAlertHistory:v1");
      const savedCooldowns = localStorage.getItem("nexusMacroAlertCooldowns:v1");
      if (savedPreferences) setAlertPreferences({ ...DEFAULT_ALERT_PREFERENCES, ...JSON.parse(savedPreferences) });
      if (savedHistory) setAlertHistory(JSON.parse(savedHistory));
      if (savedCooldowns) alertCooldowns.current = JSON.parse(savedCooldowns);
    } catch { /* Ignore invalid local settings and start from defaults. */ }
    setNotificationSupported("Notification" in window);
    if ("Notification" in window) setNotificationStatus(Notification.permission);
    setAlertsReady(true);
  }, []);

  useEffect(() => {
    if (alertsReady) localStorage.setItem("nexusMacroAlertPreferences:v1", JSON.stringify(alertPreferences));
  }, [alertPreferences, alertsReady]);

  useEffect(() => {
    if (!snapshot || !alertsReady || !alertPreferences.enabled) return;
    const now = Date.now();
    const cooldownMs = alertPreferences.cooldownMinutes * 60_000;
    const raised: MacroAlert[] = [];
    const raise = (id: string, title: string, detail: string, tone: MacroAlert["tone"]) => {
      if (now - (alertCooldowns.current[id] ?? 0) < cooldownMs) return;
      alertCooldowns.current[id] = now;
      raised.push({ id, createdAt: now, title, detail, tone });
    };

    const regime = snapshot.regime.label;
    if (alertPreferences.regime && previousRegime.current && previousRegime.current !== regime && previousRegime.current !== "AQUECENDO" && regime !== "AQUECENDO") {
      raise(`regime:${regime}`, `Regime confirmado: ${regime}`, `O estado confirmado mudou de ${previousRegime.current} para ${regime}. Score ${snapshot.regime.score ?? "—"}/100; cobertura ${snapshot.regime.coverage}%.`, regime === "RISK-ON" ? "positive" : regime === "RISK-OFF" ? "risk" : "warning");
    }
    previousRegime.current = regime;

    if (alertPreferences.exceptionalMove) {
      for (const asset of snapshot.assets) {
        const z = asset.windows?.["5"]?.z ?? asset.z5m;
        if (asset.status === "live" && z != null && Math.abs(z) >= alertPreferences.zThreshold) {
          raise(`z:${asset.key}`, `Movimento excepcional: ${asset.label}`, `Z-score de 5 min ${z > 0 ? "+" : ""}${z}σ; limiar configurado ${alertPreferences.zThreshold}σ.`, z > 0 ? "positive" : "risk");
        }
      }
    }
    if (alertPreferences.divergence) {
      for (const item of snapshot.divergences.filter(divergence => divergence.confirmed).slice(0, 4)) {
        raise(`divergence:${item.id}`, `Divergência persistente: ${item.label}`, `${item.note} Persistência de ${item.persistenceMinutes} amostras de 1 minuto; z5m ${item.leftZ5m}σ / ${item.rightZ5m}σ.`, "warning");
      }
    }
    const confirmation = snapshot.breadth.confirmation;
    if (alertPreferences.multiClass && confirmation.direction !== "MISTA" && previousConfirmation.current !== confirmation.direction) {
      raise(`multiclass:${confirmation.direction}`, `Confirmação entre classes: ${confirmation.direction}`, `Classes alinhadas: ${confirmation.classes.join(", ")}. Confirmação por preço, não fluxo financeiro observado.`, confirmation.direction === "RISK-ON" ? "positive" : "risk");
    }
    previousConfirmation.current = confirmation.direction === "MISTA" ? null : confirmation.direction;

    if (raised.length) {
      const nextHistory = [...raised.reverse(), ...alertHistory].slice(0, 40);
      setAlertHistory(nextHistory);
      localStorage.setItem("nexusMacroAlertHistory:v1", JSON.stringify(nextHistory));
      localStorage.setItem("nexusMacroAlertCooldowns:v1", JSON.stringify(alertCooldowns.current));
      if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
        for (const alert of raised) new Notification(alert.title, { body: alert.detail, tag: alert.id });
      }
    }
  }, [snapshot, alertPreferences, alertsReady, alertHistory]);

  const requestNotifications = async () => {
    if (!("Notification" in window)) return;
    const status = await Notification.requestPermission();
    setNotificationStatus(status);
  };
  const updateAlertPreferences = (patch: Partial<AlertPreferences>) => setAlertPreferences(current => ({ ...current, ...patch }));

  const chartSeries = useMemo(() => (snapshot?.chart.series ?? []).map(series => {
    const color = CHART_SERIES_COLORS[series.key] ?? series.color;
    const latestPrice = quotes[snapshot?.assets.find(asset => asset.key === series.key)?.symbol ?? ""];
    if (!latestPrice || !series.values.length) return { ...series, color };
    const asset = snapshot?.assets.find(item => item.key === series.key);
    const basisPrice = asset?.price;
    if (!basisPrice) return { ...series, color };
    const last = series.values[series.values.length - 1];
    const value = last.value + (latestPrice / basisPrice - 1) * 100;
    return { ...series, color, values: [...series.values.slice(0, -1), { time: Date.now(), value }] };
  }), [snapshot, quotes]);
  const dailyChartSeries = useMemo(() => (snapshot?.chart.dailySeries ?? []).map(series => {
    const color = CHART_SERIES_COLORS[series.key] ?? series.color;
    const latestPrice = quotes[snapshot?.assets.find(asset => asset.key === series.key)?.symbol ?? ""];
    if (!latestPrice || !series.values.length) return { ...series, color };
    const asset = snapshot?.assets.find(item => item.key === series.key);
    const basisPrice = asset?.price;
    if (!basisPrice) return { ...series, color };
    const last = series.values[series.values.length - 1];
    const value = last.value + (latestPrice / basisPrice - 1) * 100;
    return { ...series, color, values: [...series.values.slice(0, -1), { time: Date.now(), value }] };
  }), [snapshot, quotes]);
  const rotationPoints = useMemo(() => (snapshot?.assets ?? []).flatMap(asset => {
    const z5 = asset.windows?.["5"]?.z, z15 = asset.windows?.["15"]?.z, z60 = asset.windows?.["60"]?.z;
    if (!asset.available || z5 == null || z15 == null || z60 == null || asset.ageMs == null || asset.ageMs > 120_000) return [];
    const strength = (z15 + z60) / 2;
    const acceleration = z5 - z15;
    return [{ asset, strength, acceleration, x: Math.max(-1, Math.min(1, strength / 3)), y: Math.max(-1, Math.min(1, acceleration / 2)) }];
  }), [snapshot]);
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
          {snapshot?.regime.transitionPending && <div className="macro-regime-pending">CANDIDATO {snapshot.regime.candidate} · CONFIRMAÇÃO {snapshot.regime.pendingSamples ?? 0}/{snapshot.regime.requiredSamples ?? 3} AMOSTRAS DE 1 MIN</div>}
          <div className="macro-components">{snapshot?.regime.components.map(component => <div key={component.key}><span>{component.label}</span><strong className={component.value == null ? "muted" : component.value >= 0 ? "pos" : "neg"}>{component.value == null ? "—" : `${component.value > 0 ? "+" : ""}${component.value}`}</strong></div>)}</div>
        </article>
        <article className="macro-metric-card usd"><div className="macro-card-kicker"><Waves size={14}/> PRESSÃO NO DÓLAR</div><strong>{snapshot?.usd.state ?? "CALCULANDO"}</strong><div className="macro-metric-value">{snapshot?.usd.strengthScore == null ? "—" : `${snapshot.usd.strengthScore > 0 ? "+" : ""}${snapshot.usd.strengthScore}`}<small>/100</small></div><p>DXY + cesta EUR, GBP, AUD, NZD e JPY</p></article>
        <article className="macro-metric-card oil"><div className="macro-card-kicker"><Activity size={14}/> PRESSÃO DE ENERGIA</div><strong>{snapshot?.oil.state ?? "CALCULANDO"}</strong><div className="macro-metric-value">{snapshot?.oil.pressureScore == null ? "—" : `${snapshot.oil.pressureScore > 0 ? "+" : ""}${snapshot.oil.pressureScore}`}<small>/100</small></div><p>Brent & WTI · proxy de custo e inflação</p></article>
        <article className="macro-metric-card storage"><div className="macro-card-kicker"><Database size={14}/> HISTÓRICO SINCRONIZADO</div><strong>{snapshot?.storage.assetsWithHistory ?? 0}<small> / {snapshot?.storage.trackedAssets ?? 22} ativos</small></strong><div className="macro-storage-line">{snapshot?.storage.engine.toUpperCase() ?? "—"}<span className={snapshot?.storage.durable ? "pos" : "warning"}>{snapshot?.storage.durable ? "DURÁVEL" : "VOLÁTIL"}</span></div><p>1 amostra/min · retenção {snapshot?.storage.retentionDays ?? 7} dias</p></article>
      </section>

      <MarketRotationRadar points={rotationPoints} category={rotationCategory} onCategoryChange={setRotationCategory}/>

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

      <section className="macro-outlook-section">
        <div className="macro-section-heading"><div><div className="eyebrow">MAPA DE EXPOSIÇÃO · CURTO PRAZO</div><h2>Quem pode se beneficiar e o que merece atenção</h2></div><span>VIÉS OBSERVADO · NÃO É PREVISÃO</span></div>
        <div className="macro-outlook-grid">{snapshot?.outlook.map(item => <article className={`macro-outlook-card ${item.bias}`} key={item.group}><small>{item.group}</small><strong>{item.title}</strong><p>{item.detail}</p><span>{item.bias === "favorable" ? "MOMENTUM FAVORÁVEL" : item.bias === "caution" ? "PRESSÃO / CAUTELA" : "OBSERVAR CONFIRMAÇÃO"}</span></article>) ?? <div className="loading">Aguardando fatores alinhados para estimar exposição…</div>}</div>
      </section>

      <div className="macro-price-maps">
        <section className="panel macro-chart-panel">
          <div className="panelhead macro-panel-head"><span className="paneltitle">MAPA DE PREÇOS · INTRADIA</span><span className="macro-tag"><i/> ÚLTIMOS 60 MIN</span></div>
          <p className="macro-chart-subtitle">Cada linha começa em 0% no início da janela. A escala comum evidencia divergência e confluência entre os ativos.</p>
          <MacroLineChart series={chartSeries} label="últimos 60 minutos" emptyMessage="Aguardando amostras recentes sincronizadas para desenhar o mapa intradia."/>
          <div className="macro-chart-foot"><span>HORÁRIO SÃO PAULO · {snapshot ? fmtTime(snapshot.asOf) : "—"}</span><span>{snapshot?.chart.series.length ?? 0} SÉRIES COM COBERTURA</span></div>
        </section>
        <section className="panel macro-chart-panel">
          <div className="panelhead macro-panel-head"><span className="paneltitle">MAPA DE PREÇOS · DIÁRIO</span><span className="macro-tag"><i/> ÚLTIMAS 24 HORAS</span></div>
          <p className="macro-chart-subtitle">Rebase em 0% no preço de aproximadamente 24h atrás. O ativo aparece apenas com histórico próximo do período e cotação recente.</p>
          <MacroLineChart series={dailyChartSeries} label="últimas 24 horas" emptyMessage="Aquecendo histórico diário. Cada ativo entra após acumular uma base próxima de 24 horas."/>
          <div className="macro-chart-foot"><span>BASE MÍNIMA · 23H DE HISTÓRICO</span><span>{snapshot?.chart.dailySeries?.length ?? 0} SÉRIES COM COBERTURA</span></div>
        </section>
      </div>
      <section className="panel macro-insight-panel">
        <div className="panelhead"><span className="paneltitle">LEITURA DO REGIME</span><span className="macro-tag"><i/></span></div>
        <div className="macro-insights">{snapshot?.insights.map((insight, index) => <article className={`macro-insight ${insight.tone}`} key={`${insight.title}-${index}`}><span className="macro-insight-mark"><ArrowUpRight size={15}/></span><div><strong>{insight.title}</strong><p>{insight.detail}</p></div></article>) ?? <div className="loading">Carregando fatores macro observados…</div>}</div>
        <div className="macro-insight-note">Viés estimado por preços recentes; confirme com o comportamento do próprio índice e com o horário de negociação.</div>
      </section>

      <div className="macro-grid macro-lower">
        <section className="panel macro-assets-panel">
          <div className="panelhead"><span className="paneltitle">MOMENTUM NORMALIZADO · 1 / 5 / 15 / 60 MIN</span><span className="macro-tag">RETORNO · VELOCIDADE · Z-SCORE</span></div>
          <div className="macro-table-scroll"><div className="macro-table-head"><span>ATIVO / FATOR</span><span>PREÇO</span><span>1 MIN</span><span>5 MIN</span><span>15 MIN</span><span>60 MIN</span><span>Z 5M</span><span>ACELERAÇÃO</span></div>
          <div className="macro-assets-list">{snapshot?.assets.filter(item => item.available).sort((a, b) => Math.abs(b.windows?.["5"]?.z ?? 0) - Math.abs(a.windows?.["5"]?.z ?? 0)).map(asset => {
            const price = quotes[asset.symbol] ?? asset.price;
            const isYield = asset.unit === "yield_pct";
            const acceleration = isYield ? asset.acceleration5mBps : asset.acceleration5mPp;
            const unit = isYield ? " bps" : "%";
            const volume = asset.quoteVolume24h;
            const fmtWindow = (window: string) => {
              const value = asset.windows?.[window]?.change;
              return value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(isYield ? 2 : 3)}${unit}`;
            };
            const z5m = asset.windows?.["5"]?.z ?? asset.z5m;
            return <div className="macro-asset-row" key={asset.key}>
              <span className="macro-asset-label"><i style={{ background: asset.color }}/><strong>{asset.label}</strong><small>{asset.source ?? "BingX USDT-M"} · {asset.status === "live" ? "AO VIVO" : "HISTÓRICO"}{volume != null ? ` · Vol. 24h ${fmtCompact(volume)} USDC` : ""}</small></span>
              <strong className="mono">{price == null ? "—" : isYield ? `${price.toFixed(3)}%` : fmtPrice(price, asset.category === "fx" ? 4 : 2)}</strong>
              {(["1", "5", "15", "60"] as const).map(window => {
                const metric = asset.windows?.[window];
                const value = metric?.change;
                const velocity = metric?.velocityPerMinute;
                const z = metric?.z;
                return <span key={window} title={`Direção: ${metric?.direction ?? "indisponível"} · velocidade: ${velocity == null ? "—" : `${velocity.toFixed(4)}${unit}/min`} · intensidade: ${z == null ? "—" : `${z.toFixed(2)}σ`}`} className={`macro-window-value mono ${value == null ? "muted" : value >= 0 ? "pos" : "neg"}`}>
                  {fmtWindow(window)}<small className={z == null ? "muted" : Math.abs(z) >= 2 ? z > 0 ? "pos" : "neg" : ""}>{z == null ? "—" : `${z > 0 ? "+" : ""}${z.toFixed(1)}σ`}</small>
                </span>;
              })}
              <span className={`mono ${z5m == null ? "muted" : Math.abs(z5m) >= 2 ? z5m > 0 ? "pos" : "neg" : ""}`}>{z5m == null ? "—" : `${z5m > 0 ? "+" : ""}${z5m.toFixed(2)}σ`}</span>
              <span className={`mono ${acceleration == null ? "muted" : acceleration >= 0 ? "pos" : "neg"}`}>{acceleration == null ? "—" : `${acceleration > 0 ? "+" : ""}${acceleration.toFixed(isYield ? 2 : 3)}${isYield ? " bps" : " pp"}`}</span>
            </div>;
          }) ?? <div className="loading">Aguardando histórico sincronizado…</div>}</div>
          </div><div className="macro-table-foot">Z-score compara o movimento com a volatilidade recente do próprio ativo. Yields: variação em pontos-base; demais ativos: retorno percentual. Aceleração compara os dois blocos de 5 min anteriores. Arraste horizontalmente para ver todas as janelas.</div>
        </section>
      </div>

      <section className="panel macro-divergence-panel">
        <div className="panelhead"><span className="paneltitle">DIVERGÊNCIAS INTERMERCADO</span><span className="macro-tag">Z5M ≥ 0,75σ · CONFIRMAÇÃO EM 2 AMOSTRAS</span></div>
        <div className="macro-divergence-list">{snapshot?.divergences.length ? snapshot.divergences.map(item => <article key={item.id} className={item.confirmed ? "confirmed" : "pending"}><div><strong>{item.label}</strong><p>{item.note}</p></div><span className={item.confirmed ? "warning" : "muted"}>{item.confirmed ? `CONFIRMADA · ${item.persistenceMinutes} MIN` : `OBSERVANDO · ${item.persistenceMinutes}/2`}</span><small>Z5M {item.leftZ5m}σ / {item.rightZ5m}σ</small></article>) : <div className="macro-empty-note">Nenhuma divergência acima do limiar agora. Falta de divergência não confirma tendência.</div>}</div>
      </section>

      <section className="panel macro-alert-panel">
        <div className="panelhead"><span className="paneltitle"><Bell size={13}/> ALERTAS QUANTITATIVOS</span><div className="macro-alert-actions"><span className="macro-tag">{alertPreferences.enabled ? "MONITORAMENTO ATIVO" : "PAUSADO"}</span><button type="button" onClick={() => setShowAlertSettings(value => !value)} aria-expanded={showAlertSettings}>{showAlertSettings ? "OCULTAR CONFIGURAÇÃO" : "CONFIGURAR"}</button>{notificationSupported && notificationStatus !== "granted" && <button type="button" onClick={requestNotifications}><BellRing size={12}/> NOTIFICAÇÕES DO NAVEGADOR</button>}{notificationSupported && notificationStatus === "granted" && <span className="macro-tag">NOTIFICAÇÕES PERMITIDAS</span>}</div></div>
        {showAlertSettings && <div className="macro-alert-settings">
          <label className="macro-alert-toggle"><input type="checkbox" checked={alertPreferences.enabled} onChange={event => updateAlertPreferences({ enabled: event.target.checked })}/> Monitorar alertas</label>
          {([ ["regime", "Mudança de regime"], ["exceptionalMove", "Movimento excepcional por Z-score"], ["divergence", "Divergência intermercado confirmada"], ["multiClass", "Confirmação simultânea entre classes"] ] as const).map(([key, label]) => <label className="macro-alert-toggle" key={key}><input type="checkbox" checked={alertPreferences[key]} onChange={event => updateAlertPreferences({ [key]: event.target.checked })}/>{label}</label>)}
          <label className="macro-alert-range">Limiar de Z-score <strong>{alertPreferences.zThreshold.toFixed(2)}σ</strong><input type="range" min="1" max="4" step="0.25" value={alertPreferences.zThreshold} onChange={event => updateAlertPreferences({ zThreshold: Number(event.target.value) })}/></label>
          <label className="macro-alert-cooldown">Cooldown<select value={alertPreferences.cooldownMinutes} onChange={event => updateAlertPreferences({ cooldownMinutes: Number(event.target.value) })}><option value={5}>5 min</option><option value={10}>10 min</option><option value={15}>15 min</option><option value={30}>30 min</option></select></label>
        </div>}
        <div className="macro-alert-history" aria-live="polite">{alertHistory.length ? alertHistory.slice(0, 10).map(alert => <article className={`macro-alert-item ${alert.tone}`} key={`${alert.id}:${alert.createdAt}`}><div><strong>{alert.title}</strong><p>{alert.detail}</p></div><time>{new Intl.DateTimeFormat("pt-BR", { timeZone: "America/Sao_Paulo", hour: "2-digit", minute: "2-digit" }).format(alert.createdAt)}</time></article>) : <div className="macro-empty-note">Sem alertas recentes. Os alertas e preferências ficam salvos apenas neste navegador.</div>}</div>
      </section>

      <section className="macro-method">
        <button onClick={() => setShowMethod(value => !value)}><Activity size={13}/>{showMethod ? "OCULTAR MÉTODO E LIMITAÇÕES" : "MÉTODO, COBERTURA E LIMITAÇÕES"}<span>{snapshot?.storage.engine.toUpperCase()} · {snapshot?.storage.durable ? "RETENÇÃO DURÁVEL" : "ARMAZENAMENTO NÃO DURÁVEL"}</span></button>
        {showMethod && <div className="macro-method-content"><p>{snapshot?.method}</p><ul>{snapshot?.limitations.map(item => <li key={item}>{item}</li>)}</ul><p>Histórico: {snapshot?.storage.oldestSample ? `desde ${fmtTime(snapshot.storage.oldestSample)}` : "a coleta está começando"}. Fonte: {snapshot?.source}.</p></div>}
      </section>
      <footer className="tradfi-footer"><Link href="/tradfi">← Índices, Forex & Commodities</Link><span>MACRO QUANT · MODELO OBSERVACIONAL · SEM ORDENS</span></footer>
    </div>
  </main>;
}
