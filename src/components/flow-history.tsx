"use client";

import { useEffect, useMemo, useState } from "react";
import { fmtCompact, getJson } from "@/lib/market";

type FlowBar = {
  time: number; tradeCount: number; buyQuantity: number; sellQuantity: number;
  buyNotional: number; sellNotional: number; open: number; high: number; low: number; close: number;
};
type FlowHistoryData = {
  bars: FlowBar[];
  coverage: { estimatedPercent: number | null; observedBuckets: number; observedStart: number | null };
};

export default function FlowHistory({ symbol }: { symbol: string }) {
  const [hours, setHours] = useState(6);
  const [data, setData] = useState<FlowHistoryData | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const interval = hours <= 6 ? 1 : hours <= 24 ? 5 : 60;

  useEffect(() => {
    let active = true;
    const load = async () => {
      const end = Date.now();
      const start = end - hours * 60 * 60_000;
      try {
        const response = await getJson<FlowHistoryData>(`/api/flow-history?symbol=${symbol}&interval=${interval}&start_ms=${start}&end_ms=${end}`);
        if (active) { setData(response); setUnavailable(false); }
      } catch {
        if (active) { setData(null); setUnavailable(true); }
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [symbol, hours, interval]);

  const chart = useMemo(() => {
    const bars = data?.bars ?? [];
    let total = 0;
    const values = bars.map(bar => { total += bar.buyQuantity - bar.sellQuantity; return total; });
    if (values.length < 2) return { points: values.length ? `8,56` : "", net: total, zeroY: 56 };
    const min = Math.min(0, ...values), max = Math.max(0, ...values);
    const span = max - min || 1;
    const y = (value: number) => 100 - (value - min) * 88 / span;
    const points = values.map((value, index) => `${8 + index * 344 / (values.length - 1)},${y(value)}`).join(" ");
    return { points, net: total, zeroY: y(0) };
  }, [data]);

  const coverage = data?.coverage.estimatedPercent;
  return <div className="flowhistory">
    <div className="flowhistory-head"><span>CVD HISTÓRICO · {symbol.replace("USDT", "")}</span><div className="flowhistory-ranges">
      {[6, 24, 168].map(value => <button key={value} className={hours === value ? "active" : ""} onClick={() => setHours(value)}>{value === 168 ? "7D" : `${value}H`}</button>)}
    </div></div>
    {data && data.bars.length > 0 ? <>
      <svg className="flowhistory-chart" viewBox="0 0 360 112" preserveAspectRatio="none" role="img" aria-label={`CVD observado no período, líquido ${chart.net.toFixed(4)} ${symbol.replace("USDT", "")}`}>
        <line x1="0" x2="360" y1={chart.zeroY} y2={chart.zeroY} stroke="#283440" strokeWidth="1"/>
        <line x1="0" x2="360" y1="56" y2="56" stroke="#202a35" strokeWidth="1" strokeDasharray="3 4"/>
        <polyline points={chart.points} fill="none" stroke={chart.net >= 0 ? "#44d49a" : "#ff667c"} strokeWidth="2" vectorEffect="non-scaling-stroke"/>
        {data.bars.length === 1 && <circle cx="8" cy="56" r="3" fill={chart.net >= 0 ? "#44d49a" : "#ff667c"}/>}
      </svg>
      <div className="flowhistory-meta"><span>Saldo observado</span><strong className={`mono ${chart.net >= 0 ? "pos" : "neg"}`}>{chart.net >= 0 ? "+" : ""}{chart.net.toFixed(4)} {symbol.replace("USDT", "")}</strong></div>
      <div className="flowhistory-meta"><span>Trades · barras · cobertura estimada</span><span className="mono">{fmtCompact(data.bars.reduce((sum, bar) => sum + bar.tradeCount, 0))} · {data.bars.length} · {coverage == null ? "—" : `${coverage.toFixed(0)}%`}</span></div>
    </> : <div className="flowhistory-empty">{unavailable ? "Histórico agregado indisponível." : "Coletando fluxo para formar o histórico…"}</div>}
    <div className="flowhistory-note">CVD calculado de aggTrades persistidos desde o início da coleta agregada; não é histórico da exchange.</div>
  </div>;
}
