"use client";

import { useMemo, useState } from "react";
import { ArrowDownWideNarrow, ArrowUpWideNarrow, ScanSearch } from "lucide-react";
import { fmtCompact, fmtPrice, type MarketSnapshot, type ScannerFlowSnapshot } from "@/lib/market";

type Filter = "all" | "positive" | "negative" | "active";
type SortKey = "change24h" | "quoteVolume24h" | "relativeFlow15m" | "cvdNotional15m" | "cvdNotional1h" | "cvdNotional4h";

export default function MarketScanner({ symbols, markets, flow, selectedSymbol, onSelect }: {
  symbols: string[];
  markets: Record<string, MarketSnapshot>;
  flow: ScannerFlowSnapshot | null;
  selectedSymbol: string;
  onSelect: (symbol: string) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("relativeFlow15m");
  const [descending, setDescending] = useState(true);
  const [query, setQuery] = useState("");

  const rows = useMemo(() => {
    const normalized = query.trim().toUpperCase();
    return symbols.map(symbol => ({ symbol, market: markets[symbol], flow: flow?.assets[symbol] }))
      .filter(row => !normalized || row.symbol.includes(normalized) || row.symbol.replace("USDT", "").includes(normalized))
      .filter(row => filter === "all" || (filter === "positive" && (row.market?.change24h ?? 0) > 0) || (filter === "negative" && (row.market?.change24h ?? 0) < 0) || (filter === "active" && (row.flow?.relativeFlow15m ?? 0) >= 1.5))
      .sort((a, b) => {
        const aValue = sortKey === "change24h" || sortKey === "quoteVolume24h" ? a.market?.[sortKey] : a.flow?.[sortKey];
        const bValue = sortKey === "change24h" || sortKey === "quoteVolume24h" ? b.market?.[sortKey] : b.flow?.[sortKey];
        if (aValue == null) return bValue == null ? 0 : 1;
        if (bValue == null) return -1;
        return (Number(bValue) - Number(aValue)) * (descending ? 1 : -1);
      });
  }, [symbols, markets, flow, filter, query, sortKey, descending]);

  const breadth = useMemo(() => {
    const observed = symbols.map(symbol => markets[symbol]).filter(item => item?.change24h != null);
    const leaders = [...observed].sort((a, b) => (b.change24h ?? 0) - (a.change24h ?? 0));
    return {
      count: observed.length,
      rising: observed.filter(item => (item.change24h ?? 0) > 0).length,
      falling: observed.filter(item => (item.change24h ?? 0) < 0).length,
      leaders: leaders.slice(0, 2),
      laggards: leaders.slice(-2).reverse(),
    };
  }, [symbols, markets]);

  const selectSort = (key: SortKey) => {
    if (sortKey === key) setDescending(value => !value);
    else { setSortKey(key); setDescending(true); }
  };
  const sortButton = (key: SortKey, label: string) => <button className="scanner-sort" onClick={() => selectSort(key)}>{label}{sortKey === key ? descending ? <ArrowDownWideNarrow size={11}/> : <ArrowUpWideNarrow size={11}/> : null}</button>;

  return <section className="panel scanner-panel" id="scanner">
    <div className="panelhead"><span className="paneltitle">Scanner Spot · {symbols.length} ativos</span><span className="badge">{flow?.source ?? "Fluxo Binance aquecendo"}</span></div>
    <div className="scanner-breadth">
      <div><small>AMPLITUDE · 24H</small><strong>{breadth.count ? `${breadth.rising} alta · ${breadth.falling} baixa` : "Aguardando cotações"}</strong><span>{breadth.count}/{symbols.length} ativos observados</span></div>
      <div><small>LÍDERES</small><strong className="pos">{breadth.leaders.length ? breadth.leaders.map(item => `${item.symbol.replace("USDT", "")} ${(item.change24h ?? 0) >= 0 ? "+" : ""}${(item.change24h ?? 0).toFixed(1)}%`).join(" · ") : "—"}</strong><span>Variação spot 24h</span></div>
      <div><small>MAIORES QUEDAS</small><strong className="neg">{breadth.laggards.length ? breadth.laggards.map(item => `${item.symbol.replace("USDT", "")} ${(item.change24h ?? 0).toFixed(1)}%`).join(" · ") : "—"}</strong><span>Variação spot 24h</span></div>
    </div>
    <div className="scanner-controls">
      <label className="scanner-search"><ScanSearch size={13}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Filtrar ativo" aria-label="Filtrar ativos do scanner"/></label>
      <div className="scanner-filters" aria-label="Filtrar scanner">
        {([ ["all", "Todos"], ["positive", "Em alta"], ["negative", "Em baixa"], ["active", "Fluxo ≥1,5×"] ] as [Filter, string][]).map(([key, label]) => <button key={key} className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>{label}</button>)}
      </div>
      <span className="scanner-stamp">Fluxo consultado {flow ? new Date(flow.receivedAt).toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo", hour12: false }) : "—"}</span>
    </div>
    <div className="scanner-table-wrap">
      <table className="scanner-table">
        <thead><tr><th>Ativo</th><th>Preço</th><th>{sortButton("change24h", "Variação 24h")}</th><th>{sortButton("quoteVolume24h", "Volume 24h · USDT")}</th><th>{sortButton("relativeFlow15m", "Ritmo de fluxo · 15m")}</th><th>{sortButton("cvdNotional1h", "CVD · 1h")}</th><th>{sortButton("cvdNotional4h", "CVD · 4h")}</th></tr></thead>
        <tbody>{rows.map(({ symbol, market, flow: itemFlow }) => <tr key={symbol} className={selectedSymbol === symbol ? "selected" : ""} onClick={() => onSelect(symbol)} tabIndex={0} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(symbol); } }}>
          <td><strong>{symbol.replace("USDT", "")}</strong><small> / USDT</small><span className={`scanner-source ${market?.status === "live" ? "live" : ""}`}>{market?.status === "live" ? "LIVE" : market?.status?.toUpperCase() ?? "AGUARDANDO"}</span></td>
          <td className="mono">{fmtPrice(market?.price, market?.price && market.price < 10 ? 4 : 2)}</td>
          <td className={`mono ${(market?.change24h ?? 0) >= 0 ? "pos" : "neg"}`}>{market?.change24h == null ? "—" : `${market.change24h >= 0 ? "+" : ""}${market.change24h.toFixed(2)}%`}</td>
          <td className="mono">{market?.quoteVolume24h == null ? "—" : `$${fmtCompact(market.quoteVolume24h)}`}</td>
          <td className="mono">{itemFlow?.relativeFlow15m == null ? <span className="subtle">Aquecendo / cobertura insuficiente</span> : <strong className={itemFlow.relativeFlow15m >= 1.5 ? "amber" : ""}>{itemFlow.relativeFlow15m.toFixed(2)}×</strong>}</td>
          <td className={`mono ${(itemFlow?.cvdNotional1h ?? 0) >= 0 ? "pos" : "neg"}`} title={`${itemFlow?.observedBuckets1h ?? 0}/4 blocos de 15m com trades observados`}>{itemFlow?.cvdNotional1h == null ? "—" : `${itemFlow.cvdNotional1h >= 0 ? "+" : "−"}$${fmtCompact(Math.abs(itemFlow.cvdNotional1h))}`}</td>
          <td className={`mono ${(itemFlow?.cvdNotional4h ?? 0) >= 0 ? "pos" : "neg"}`} title={`${itemFlow?.observedBuckets4h ?? 0}/16 blocos de 15m com trades observados`}>{itemFlow?.cvdNotional4h == null ? "—" : `${itemFlow.cvdNotional4h >= 0 ? "+" : "−"}$${fmtCompact(Math.abs(itemFlow.cvdNotional4h))}`}</td>
        </tr>)}{rows.length === 0 && <tr><td colSpan={7} className="scanner-empty">Nenhum ativo corresponde ao filtro.</td></tr>}</tbody>
      </table>
    </div>
    <div className="disclaimer">Amplitude e líderes usam as variações 24h da Binance Spot nos ativos da lista. Ritmo de fluxo compara o bloco UTC de 15m em andamento com os três anteriores. CVD de 1h/4h soma agressão compradora menos vendedora em blocos com trades observados; dados de fluxo são da Binance, não do mercado inteiro. Passe o cursor para ver cobertura dos blocos.</div>
  </section>;
}
