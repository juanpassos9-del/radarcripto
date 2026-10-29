"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Activity, Globe2, Landmark } from "lucide-react";
import { fmtCompact, fmtTime, getJson, type MarketContextData, type MarketSnapshot } from "@/lib/market";

export default function MarketContext({ markets }: { markets: Record<string, MarketSnapshot> }) {
  const [context, setContext] = useState<MarketContextData | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    let active = true;
    const refresh = () => getJson<MarketContextData>("/api/context")
      .then(data => { if (active) { setContext(data); setUnavailable(false); } })
      .catch(() => { if (active) setUnavailable(true); });
    void refresh();
    const timer = window.setInterval(refresh, 15 * 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const breadth = useMemo(() => {
    const values = Object.values(markets).filter(item => item.change24h != null);
    const rising = values.filter(item => (item.change24h ?? 0) > 0).length;
    const falling = values.filter(item => (item.change24h ?? 0) < 0).length;
    const average = values.length ? values.reduce((sum, item) => sum + (item.change24h ?? 0), 0) / values.length : null;
    return { observed: values.length, total: Object.keys(markets).length, rising, falling, average };
  }, [markets]);

  const stamp = (value?: number | null) => value ? `${new Date(value).toLocaleDateString("pt-BR", { timeZone: "UTC" })} · ${fmtTime(value)}` : "Horário indisponível";
  const change = (value: number | null) => value == null ? "Variação indisponível" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}% vs. registro anterior de ~24h`;

  return <section className="panel context-panel" id="context">
    <div className="panelhead"><span className="paneltitle">Contexto de mercado</span><span className="badge">Spot · sem alavancagem</span></div>
    <div className="context-grid">
      <article className="context-card">
        <div className="context-label"><Activity size={13}/> Amplitude · watchlist</div>
        <strong className="context-main">{breadth.observed ? `${breadth.rising} subindo · ${breadth.falling} caindo` : "Aguardando cotações"}</strong>
        <span className={`context-change ${breadth.average == null ? "" : breadth.average >= 0 ? "pos" : "neg"}`}>{breadth.average == null ? "—" : `Média 24h ${breadth.average >= 0 ? "+" : ""}${breadth.average.toFixed(2)}%`}</span>
        <small>{breadth.observed}/{breadth.total} ativos da watchlist com variação disponível</small>
      </article>
      <article className="context-card">
        <div className="context-label"><Activity size={13}/> Fear &amp; Greed</div>
        <strong className="context-main">{context?.fearAndGreed ? `${context.fearAndGreed.value} · ${context.fearAndGreed.label}` : unavailable ? "Indisponível" : "Carregando…"}</strong>
        <a className="context-source" href="https://alternative.me/crypto/fear-and-greed-index/" target="_blank" rel="noreferrer">Fonte: Alternative.me · {stamp(context?.fearAndGreed?.timestamp)}</a>
        <small>Indicador diário de sentimento; não é sinal de compra ou venda.</small>
      </article>
      <ContextMetricCard title="TVL DeFi · agregado" icon={<Globe2 size={13}/>} metric={context?.tvl ?? null} unavailable={unavailable} stamp={stamp} change={change}/>
      <ContextMetricCard title="Stablecoins · oferta USD" icon={<Landmark size={13}/>} metric={context?.stablecoinSupply ?? null} unavailable={unavailable} stamp={stamp} change={change}/>
    </div>
    <div className="context-foot">TVL e oferta de stablecoins são contexto on-chain agregado, com atualização tipicamente horária; não representam fluxo de ordens spot. Fontes: <a href="https://defillama.com/" target="_blank" rel="noreferrer">DefiLlama</a> e <a href="https://alternative.me/crypto/fear-and-greed-index/" target="_blank" rel="noreferrer">Alternative.me</a>. Última consulta {context ? stamp(context.receivedAt) : "—"}.</div>
  </section>;
}

function ContextMetricCard({ title, icon, metric, unavailable, stamp, change }: {
  title: string; icon: ReactNode; metric: MarketContextData["tvl"]; unavailable: boolean;
  stamp: (value?: number | null) => string; change: (value: number | null) => string;
}) {
  return <article className="context-card">
    <div className="context-label">{icon} {title}</div>
    <strong className="context-main">{metric ? `$${fmtCompact(metric.value)}` : unavailable ? "Indisponível" : "Carregando…"}</strong>
    <span className={`context-change ${metric?.change24hPct == null ? "" : metric.change24hPct >= 0 ? "pos" : "neg"}`}>{metric ? change(metric.change24hPct) : "—"}</span>
    <small className="context-source">Fonte: DefiLlama · {stamp(metric?.timestamp)}</small>
  </article>;
}
