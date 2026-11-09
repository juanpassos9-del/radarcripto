"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { fmtCompact, fmtPrice, fmtTime, getJson, type FootprintData } from "@/lib/market";

const nicePriceDigits = (step: number) => step < 0.0001 ? 7 : step < 0.01 ? 5 : step < 1 ? 3 : 2;

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
        if (active && requestId.current === id) {
          setData(next); setError("");
          setSelectedTime(previous => next.candles.some(c => c.time === previous) ? previous : next.candles.at(-1)?.time ?? null);
        }
      } catch { if (active) setError("Footprint indisponível; tentando reconectar."); }
    };
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 8000);
    return () => { active = false; window.clearInterval(timer); };
  }, [visible, symbol, interval, limit, tickCount]);

  const candle = useMemo(() => data?.candles.find(item => item.time === selectedTime) ?? data?.candles.at(-1) ?? null, [data, selectedTime]);
  const chart = useMemo(() => {
    const candles = data?.candles ?? [];
    if (!candles.length || !data) return null;
    const min = Math.min(...candles.map(item => item.low));
    const max = Math.max(...candles.map(item => item.high));
    const baseStep = Math.max(data.priceStep, 1e-10);
    const multiplier = Math.max(1, Math.ceil((max - min) / baseStep / 36));
    const step = baseStep * multiplier;
    const first = Math.floor(min / step);
    const last = Math.ceil(max / step);
    const rows = Array.from({ length: Math.max(1, last - first + 1) }, (_, index) => (last - index) * step);
    const candleLevels = candles.map(item => {
      const levels = new Map<number, { buy: number; sell: number; total: number }>();
      for (const level of item.levels) {
        const row = Math.round(level.price / step);
        const old = levels.get(row) ?? { buy: 0, sell: 0, total: 0 };
        old.buy += level.buyNotional; old.sell += level.sellNotional; old.total += level.totalNotional;
        levels.set(row, old);
      }
      const maxLevel = Math.max(1, ...[...levels.values()].map(level => level.total));
      const bodyLow = Math.min(item.open, item.close);
      const bodyHigh = Math.max(item.open, item.close);
      return { item, levels, maxLevel, bodyLow, bodyHigh };
    });
    return { rows, step, candleLevels };
  }, [data]);
  const money = (value: number | undefined) => value == null ? "—" : `$${fmtCompact(value)}`;
  const colStyle = chart ? { gridTemplateColumns: `54px repeat(${chart.candleLevels.length}, minmax(68px, 1fr))`, minWidth: `${54 + chart.candleLevels.length * 68}px` } : undefined;
  const selectedIndex = chart?.candleLevels.findIndex(item => item.item.time === candle?.time) ?? -1;
  const selectedLevels = selectedIndex >= 0 && chart ? chart.candleLevels[selectedIndex] : null;

  return <section className="panel footprint" id="footprint" ref={root as React.RefObject<HTMLElement>}>
    <div className="panelhead"><span className="paneltitle">Footprint · {symbol.replace("USDT", "")} / USDT · Binance Spot</span><span className="badge good">{data?.coverage.observedCandles ?? 0}/{data?.coverage.expectedCandles ?? limit} CANDLES</span></div>
    <div className="footprint-controls"><label>Período<select value={interval} onChange={event => setInterval(Number(event.target.value))}><option value={1}>1 minuto</option><option value={5}>5 minutos</option><option value={15}>15 minutos</option></select></label><label>Candles<select value={limit} onChange={event => setLimit(Number(event.target.value))}><option value={12}>12</option><option value={24}>24</option><option value={48}>48</option><option value={60}>60</option></select></label><label>Agrupamento<select value={tickCount} onChange={event => setTickCount(Number(event.target.value))}><option value={1}>1 tick</option><option value={2}>2 ticks</option><option value={5}>5 ticks</option><option value={10}>10 ticks</option></select></label><span className="subtle">Passo visual: {chart ? fmtPrice(chart.step, nicePriceDigits(chart.step)) : "—"}</span></div>
    {error && <div className="dataerror">{error}</div>}
    {!candle || !chart || !selectedLevels ? <div className="footprint-empty">{visible ? "Coletando aggTrades para montar os candles de footprint…" : "Abra esta seção para iniciar a coleta detalhada."}</div> : <>
      <div className="footprint-summary"><div><small>DELTA</small><strong className={candle.deltaNotional >= 0 ? "pos" : "neg"}>{candle.deltaNotional >= 0 ? "+" : ""}{money(candle.deltaNotional)}</strong></div><div><small>VOLUME</small><strong>{money(candle.totalNotional)}</strong></div><div><small>COMPRA / VENDA</small><strong><i className="pos">{money(candle.buyNotional)}</i> / <i className="neg">{money(candle.sellNotional)}</i></strong></div><div><small>NEGÓCIOS</small><strong>{fmtCompact(candle.tradeCount)}</strong></div><div><small>POC</small><strong>{fmtPrice(candle.poc)}</strong></div><div><small>VAH / VAL · 70%</small><strong>{fmtPrice(candle.vah)} / {fmtPrice(candle.val)}</strong></div></div>
      <div className="footprint-meta">Candle selecionado: {fmtTime(candle.time * 1000)} · O {fmtPrice(candle.open)} · H {fmtPrice(candle.high)} · L {fmtPrice(candle.low)} · C {fmtPrice(candle.close)} · clique em outra coluna para selecionar</div>
      <div className="footprint-chart-scroll"><div className="footprint-chart" style={colStyle}>
        <div className="footprint-chart-head" style={colStyle}><span className="fp-axis-head">PREÇO</span>{chart.candleLevels.map(({ item }) => <button key={item.time} className={`fp-candle-head ${item.time === candle.time ? "selected" : ""}`} onClick={() => setSelectedTime(item.time)}><span>{fmtTime(item.time * 1000)}</span><small>VENDAS | COMPRAS</small><b className={item.deltaNotional >= 0 ? "pos" : "neg"}>{item.deltaNotional >= 0 ? "+" : ""}{fmtCompact(item.deltaNotional)}</b></button>)}</div>
        {chart.rows.map(price => {
          const priceRow = Math.round(price / chart.step);
          const isPoc = Math.round(candle.poc / chart.step) === priceRow;
          const isVah = Math.round(candle.vah / chart.step) === priceRow;
          const isVal = Math.round(candle.val / chart.step) === priceRow;
          return <div className={`footprint-chart-row ${isPoc ? "fp-poc" : ""} ${isVah ? "fp-vah" : ""} ${isVal ? "fp-val" : ""}`} style={colStyle} key={priceRow}>
            <span className="fp-price-axis">{fmtPrice(price, nicePriceDigits(chart.step))}</span>
            {chart.candleLevels.map(({ item, levels, maxLevel, bodyLow, bodyHigh }) => {
              const level = levels.get(priceRow);
              const insideWick = price <= item.high + chart.step / 2 && price >= item.low - chart.step / 2;
              const insideBody = price <= bodyHigh + chart.step / 2 && price >= bodyLow - chart.step / 2;
              const selected = item.time === candle.time;
              const sellRatio = level ? Math.min(100, level.sell / maxLevel * 100) : 0;
              const buyRatio = level ? Math.min(100, level.buy / maxLevel * 100) : 0;
              return <button key={item.time} className={`fp-cell ${selected ? "selected" : ""} ${insideWick ? "wick" : ""} ${insideBody ? `body ${item.close >= item.open ? "up" : "down"}` : ""}`} onClick={() => setSelectedTime(item.time)} title={`${fmtTime(item.time * 1000)} · ${fmtPrice(price)} · venda ${money(level?.sell)} · compra ${money(level?.buy)}`}>
                <span className="fp-sell" style={{ "--fp-fill": `${sellRatio}%` } as React.CSSProperties}>{level?.sell ? fmtCompact(level.sell) : ""}</span><i className="fp-candle-mark"/><span className="fp-buy" style={{ "--fp-fill": `${buyRatio}%` } as React.CSSProperties}>{level?.buy ? fmtCompact(level.buy) : ""}</span>
              </button>;
            })}
          </div>;
        })}
      </div></div>
      <div className="footprint-legend"><span><i className="fp-legend-sell"/> Agressão vendedora · USDT</span><span><i className="fp-legend-buy"/> Agressão compradora · USDT</span><span><i className="fp-legend-poc"/> POC do candle selecionado</span><span><i className="fp-legend-vah"/> Área de valor</span></div>
    </>}
    <div className="footprint-note">Binance Spot aggTrades observados enquanto esta seção está ativa. A escala visual agrupa níveis para manter o gráfico legível; selecione o agrupamento para controlar a granularidade. O lado agressor é inferido e a retenção é de {data?.coverage.retentionDays ?? 1} dia. A coleta começa quando esta seção é aberta.</div>
  </section>;
}
