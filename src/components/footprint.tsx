"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { fmtCompact, fmtPrice, fmtTime, getJson, type FootprintData } from "@/lib/market";

export default function Footprint({ symbol }: { symbol: string }) {
  const root = useRef<HTMLElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [interval, setInterval] = useState(5);
  const [limit, setLimit] = useState(24);
  const [tickCount, setTickCount] = useState(5);
  const [data, setData] = useState<FootprintData | null>(null);
  const [selectedTime, setSelectedTime] = useState<number | null>(null);
  const [error, setError] = useState("");
  const requestId = useRef(0);
  useEffect(() => {
    const node = root.current;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: "250px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    let active = true;
    const load = async () => {
      const id = ++requestId.current;
      try {
        const next = await getJson<FootprintData>(`/api/footprint?symbol=${symbol}&interval=${interval}&limit=${limit}&tick_count=${tickCount}`);
        if (active && requestId.current === id) { setData(next); setError(""); setSelectedTime(previous => next.candles.some(c => c.time === previous) ? previous : next.candles.at(-1)?.time ?? null); }
      } catch { if (active) setError("Footprint indisponível; tentando reconectar."); }
    };
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 8000);
    return () => { active = false; window.clearInterval(timer); };
  }, [visible, symbol, interval, limit, tickCount]);
  const candle = useMemo(() => data?.candles.find(item => item.time === selectedTime) ?? data?.candles.at(-1) ?? null, [data, selectedTime]);
  const maxVolume = Math.max(1, ...(candle?.levels.map(level => level.totalNotional) ?? []));
  const money = (value: number | undefined) => value == null ? "—" : `$${fmtCompact(value)}`;
  return <section className="panel footprint" id="footprint" ref={root as React.RefObject<HTMLElement>}>
    <div className="panelhead"><span className="paneltitle">Footprint · {symbol.replace("USDT", "")} / USDT · Binance Spot</span><span className="badge good">{data?.coverage.observedCandles ?? 0}/{data?.coverage.expectedCandles ?? limit} CANDLES</span></div>
    <div className="footprint-controls"><label>Período<select value={interval} onChange={event => setInterval(Number(event.target.value))}><option value={1}>1 minuto</option><option value={5}>5 minutos</option><option value={15}>15 minutos</option></select></label><label>Candles<select value={limit} onChange={event => setLimit(Number(event.target.value))}><option value={12}>12</option><option value={24}>24</option><option value={48}>48</option><option value={60}>60</option></select></label><label>Agrupamento<select value={tickCount} onChange={event => setTickCount(Number(event.target.value))}><option value={1}>1 tick</option><option value={2}>2 ticks</option><option value={5}>5 ticks</option><option value={10}>10 ticks</option></select></label><span className="subtle">Faixa de preço: {data ? fmtPrice(data.priceStep, data.priceStep < 0.01 ? 6 : 4) : "—"}</span></div>
    <div className="footprint-candles">{data?.candles.map(item => <button key={item.time} className={item.time === candle?.time ? "active" : ""} onClick={() => setSelectedTime(item.time)} title={`Delta ${money(item.deltaNotional)}`}><span>{fmtTime(item.time * 1000)}</span><b className={item.deltaNotional >= 0 ? "pos" : "neg"}>{item.deltaNotional >= 0 ? "+" : ""}{fmtCompact(item.deltaNotional)}</b><small>Vol {fmtCompact(item.totalNotional)}</small></button>)}</div>
    {error && <div className="dataerror">{error}</div>}
    {!candle ? <div className="footprint-empty">{visible ? "Coletando aggTrades para montar os níveis por preço…" : "Abra esta seção para iniciar a coleta detalhada."}</div> : <>
      <div className="footprint-summary"><div><small>DELTA</small><strong className={candle.deltaNotional >= 0 ? "pos" : "neg"}>{candle.deltaNotional >= 0 ? "+" : ""}{money(candle.deltaNotional)}</strong></div><div><small>VOLUME</small><strong>{money(candle.totalNotional)}</strong></div><div><small>COMPRA / VENDA</small><strong><i className="pos">{money(candle.buyNotional)}</i> / <i className="neg">{money(candle.sellNotional)}</i></strong></div><div><small>NEGÓCIOS</small><strong>{fmtCompact(candle.tradeCount)}</strong></div><div><small>POC</small><strong>{fmtPrice(candle.poc)}</strong></div><div><small>VAH / VAL · 70%</small><strong>{fmtPrice(candle.vah)} / {fmtPrice(candle.val)}</strong></div></div>
      <div className="footprint-meta">{fmtTime(candle.time * 1000)} · O {fmtPrice(candle.open)} · H {fmtPrice(candle.high)} · L {fmtPrice(candle.low)} · C {fmtPrice(candle.close)}</div>
      <div className="footprint-ladder"><div className="footprint-row head"><span>VENDA · USDT</span><span>PREÇO</span><span>COMPRA · USDT</span><span>DELTA</span></div>{[...candle.levels].reverse().map(level => <div className={`footprint-row ${level.price === candle.poc ? "poc" : ""}`} key={level.price}><span className="footprint-cell sell" style={{ "--fp-width": `${Math.max(2, level.sellNotional / maxVolume * 100)}%` } as React.CSSProperties}>{money(level.sellNotional)}</span><span className="mono">{fmtPrice(level.price, data?.priceStep && data.priceStep < 0.01 ? 5 : 2)}</span><span className="footprint-cell buy" style={{ "--fp-width": `${Math.max(2, level.buyNotional / maxVolume * 100)}%` } as React.CSSProperties}>{money(level.buyNotional)}</span><span className={level.delta >= 0 ? "pos" : "neg"}>{level.delta >= 0 ? "+" : ""}{fmtCompact(level.delta)}</span></div>)}</div>
    </>}
    <div className="footprint-note">{data?.source ?? "Dados detalhados passam a ser registrados enquanto esta seção está ativa."} · Delta pelo lado agressor inferido na Binance; não representa volume de todas as corretoras. Retenção: {data?.coverage.retentionDays ?? 3} dias. {data?.coverage.interpretation ?? "Histórico começa quando a coleta desta seção inicia."}</div>
  </section>;
}
