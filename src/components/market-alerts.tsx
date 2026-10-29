"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Bell, BellRing, Plus, Trash2 } from "lucide-react";
import { fmtCompact, fmtPrice, type MarketSnapshot, type ScannerFlowSnapshot } from "@/lib/market";

type Metric = "price" | "change24h" | "quoteVolume24h" | "relativeFlow15m" | "cvdNotional15m";
type Operator = "above" | "below";
type Rule = { id: string; symbol: string; metric: Metric; operator: Operator; threshold: number; enabled: boolean; createdAt: number; lastTriggeredAt: number | null };
const storageKey = "nexus-market-alert-rules-v1";
const metricNames: Record<Metric, string> = { price: "Preço", change24h: "Variação 24h", quoteVolume24h: "Volume 24h", relativeFlow15m: "Ritmo de fluxo 15m", cvdNotional15m: "CVD nocional do bloco" };

export default function MarketAlerts({ symbols, markets, flow }: { symbols: string[]; markets: Record<string, MarketSnapshot>; flow: ScannerFlowSnapshot | null }) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [hydrated, setHydrated] = useState(false);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [metric, setMetric] = useState<Metric>("price");
  const [operator, setOperator] = useState<Operator>("above");
  const [threshold, setThreshold] = useState("" );
  const [notice, setNotice] = useState("");
  const [permission, setPermission] = useState("default");
  const previousValues = useRef(new Map<string, number>());
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(storageKey) ?? "[]") as Rule[];
      setRules(Array.isArray(saved) ? saved.filter(rule => rule && typeof rule.id === "string" && symbols.includes(rule.symbol) && Number.isFinite(rule.threshold)) : []);
    } catch { setRules([]); }
    setHydrated(true);
    setPermission("Notification" in window ? Notification.permission : "unsupported");
    return () => { if (noticeTimer.current) clearTimeout(noticeTimer.current); };
  }, [symbols]);

  useEffect(() => {
    if (hydrated) window.localStorage.setItem(storageKey, JSON.stringify(rules));
  }, [hydrated, rules]);

  useEffect(() => {
    const currentValues = new Map<string, number>();
    for (const asset of symbols) {
      const market = markets[asset];
      const flowAsset = flow?.assets[asset];
      const values: Partial<Record<Metric, number | null | undefined>> = {
        price: market?.price,
        change24h: market?.change24h,
        quoteVolume24h: market?.quoteVolume24h,
        relativeFlow15m: flowAsset?.relativeFlow15m,
        cvdNotional15m: flowAsset?.cvdNotional15m,
      };
      for (const [key, value] of Object.entries(values)) if (value != null && Number.isFinite(value)) currentValues.set(`${asset}:${key}`, value);
    }

    const now = Date.now();
    const fired = rules.filter(rule => {
      if (!rule.enabled || rule.lastTriggeredAt != null && now - rule.lastTriggeredAt < 15 * 60_000) return false;
      const key = `${rule.symbol}:${rule.metric}`;
      const current = currentValues.get(key);
      const previous = previousValues.current.get(key);
      if (current == null || previous == null) return false;
      return rule.operator === "above" ? previous < rule.threshold && current >= rule.threshold : previous > rule.threshold && current <= rule.threshold;
    });
    previousValues.current = currentValues;

    if (fired.length) {
      const message = fired.map(rule => `${rule.symbol.replace("USDT", "")} · ${metricNames[rule.metric]} ${rule.operator === "above" ? "cruzou acima de" : "cruzou abaixo de"} ${formatThreshold(rule.metric, rule.threshold)}`).join("; ");
      setNotice(message);
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
      noticeTimer.current = setTimeout(() => setNotice(""), 10_000);
      if ("Notification" in window && Notification.permission === "granted") {
        try { new Notification("NEXUS Spot · Alerta", { body: message }); } catch { /* in-app notice remains visible */ }
      }
      const firedAt = new Map(fired.map(rule => [rule.id, now]));
      setRules(previous => previous.map(rule => firedAt.has(rule.id) ? { ...rule, lastTriggeredAt: now } : rule));
    }
  }, [symbols, markets, flow, rules]);

  const addRule = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const parsed = Number(threshold);
    if (!Number.isFinite(parsed) || threshold.trim() === "") return;
    const rule: Rule = { id: crypto.randomUUID(), symbol, metric, operator, threshold: parsed, enabled: true, createdAt: Date.now(), lastTriggeredAt: null };
    setRules(current => [...current, rule]);
    setThreshold("");
  };
  return <section className="panel alerts-panel" id="alerts">
    <div className="panelhead"><span className="paneltitle">Alertas configuráveis</span><button className="tinybtn" onClick={async () => { if ("Notification" in window) setPermission(await Notification.requestPermission()); }}><BellRing size={11}/> {permission === "granted" ? "Notificações permitidas" : permission === "unsupported" ? "Notificação indisponível" : "Ativar notificações"}</button></div>
    <form className="alert-form" onSubmit={addRule}>
      <label>Ativo<select value={symbol} onChange={event => setSymbol(event.target.value)}>{symbols.map(asset => <option key={asset} value={asset}>{asset.replace("USDT", "")}/USDT</option>)}</select></label>
      <label>Métrica<select value={metric} onChange={event => setMetric(event.target.value as Metric)}>{Object.entries(metricNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>Condição<select value={operator} onChange={event => setOperator(event.target.value as Operator)}><option value="above">Cruzar acima</option><option value="below">Cruzar abaixo</option></select></label>
      <label>Limite <span className="subtle">{metric === "price" || metric === "quoteVolume24h" || metric === "cvdNotional15m" ? "USDT" : metric === "change24h" ? "%" : "×"}</span><input type="number" step="any" value={threshold} onChange={event => setThreshold(event.target.value)} placeholder={metric === "price" ? "Ex.: 65000" : metric === "change24h" ? "Ex.: 5" : metric === "relativeFlow15m" ? "Ex.: 1.5" : "Ex.: 1000000"} required/></label>
      <button className="alert-add" type="submit"><Plus size={13}/> Criar alerta</button>
    </form>
    <div className="alert-hint">O alerta dispara quando a condição cruza o limite. Cada regra tem cooldown de 15 min. Os dados de fluxo são observações da Binance e o alerta só funciona enquanto este terminal estiver aberto.</div>
    {notice && <div className="alert-notice" role="status"><Bell size={13}/>{notice}</div>}
    <div className="alert-rules">{rules.map(rule => <div className={`alert-rule ${rule.enabled ? "" : "disabled"}`} key={rule.id}>
      <button className={`signal-toggle ${rule.enabled ? "active" : ""}`} onClick={() => setRules(current => current.map(item => item.id === rule.id ? { ...item, enabled: !item.enabled } : item))} aria-label={`${rule.enabled ? "Desativar" : "Ativar"} alerta`}>{rule.enabled ? "ATIVO" : "PAUSADO"}</button>
      <span><strong>{rule.symbol.replace("USDT", "")}</strong> {metricNames[rule.metric]} {rule.operator === "above" ? "acima de" : "abaixo de"} <b className="mono">{formatThreshold(rule.metric, rule.threshold)}</b></span>
      <small>{rule.lastTriggeredAt ? `Disparou ${new Date(rule.lastTriggeredAt).toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo", hour12: false })}` : "Aguardando cruzamento"}</small>
      <button className="alert-delete" onClick={() => setRules(current => current.filter(item => item.id !== rule.id))} aria-label="Remover alerta"><Trash2 size={13}/></button>
    </div>)}{rules.length === 0 && <div className="scanner-empty">Nenhum alerta configurado.</div>}</div>
    {permission === "denied" && <div className="dataerror">Notificações bloqueadas pelo navegador; o aviso ainda aparece aqui enquanto a página estiver aberta.</div>}
  </section>;
}

function formatThreshold(metric: Metric, value: number) {
  if (metric === "price") return `${fmtPrice(value)} USDT`;
  if (metric === "change24h") return `${value}%`;
  if (metric === "relativeFlow15m") return `${value}×`;
  return `${fmtCompact(value)} USDT`;
}
