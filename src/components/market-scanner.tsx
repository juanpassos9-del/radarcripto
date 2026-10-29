"use client";

import { useMemo, useState } from "react";
import { ArrowDownWideNarrow, ArrowUpWideNarrow, ScanSearch } from "lucide-react";
import { fmtCompact, fmtPrice, type MarketSnapshot, type ScannerFlowSnapshot } from "@/lib/market";

type Filter = "all" | "positive" | "negative" | "active";
type SortKey = "change24h" | "quoteVolume24h" | "relativeFlow15m" | "cvdNotional15m";

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
        const aValue = a.flow?.[sortKey as "relativeFlow15m" | "cvdNotional15m"] ?? a.market?.[sortKey as "change24h" | "quoteVolume24h"] ?? null;
        const bValue = b.flow?.[sortKey as "relativeFlow15m" | "cvdNotional15m"] ?? b.market?.[sortKey as "change24h" | "quoteVolume24h"] ?? null;
        if (aValue == null) return bValue == null ? 0 : 1;
        if (bValue == null) return -1;
        return (Number(bValue) - Number(aValue)) * (descending ? 1 : -1);
      });
  }, [symbols, markets, flow, filter, query, sortKey, descending]);

  const selectSort = (key: SortKey) => {
    if (sortKey === key) setDescending(value => !value);
    else { setSortKey(key); setDescending(true); }
  };
  const sortButton = (key: SortKey, label: string) => <button className="scanner-sort" onClick={() => selectSort(key)}>{label}{sortKey === key ? descending ? <ArrowDownWideNarrow size={11}/> : <ArrowUpWideNarrow size={11}/> : null}</button>;

  return <section className="panel scanner-panel" id="scanner">
    <div className="panelhead"><span className="paneltitle">Scanner Spot · 25 ativos</span><span className="badge">{flow?.source ?? "Fluxo Binance aquecendo"}</span></div>
    <div className="scanner-controls">
      <label className="scanner-search"><ScanSearch size={13}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Filtrar ativo" aria-label="Filtrar ativos do scanner"/></label>
      <div className="scanner-filters" aria-label="Filtrar scanner">
        {([ ["all", "Todos"], ["positive", "Em alta"], ["negative", "Em baixa"], ["active", "Fluxo ≥1,5×"] ] as [Filter, string][]).map(([key, label]) => <button key={key} className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>{label}</button>)}
      </div>
      <span className="scanner-stamp">Fluxo consultado {flow ? new Date(flow.receivedAt).toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo", hour12: false }) : "—"}</span>
    </div>
    <div className="scanner-table-wrap">
      <table className="scanner-table">
        <thead><tr><th>Ativo</th><th>Preço</th><th>{sortButton("change24h", "Variação 24h")}</th><th>{sortButton("quoteVolume24h", "Volume 24h · USDT")}</th><th>{sortButton("relativeFlow15m", "Ritmo de fluxo · 15m")}</th><th>{sortButton("cvdNotional15m", "CVD nocional · bloco atual")}</th></tr></thead>
        <tbody>{rows.map(({ symbol, market, flow: itemFlow }) => <tr key={symbol} className={selectedSymbol === symbol ? "selected" : ""} onClick={() => onSelect(symbol)} tabIndex={0} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(symbol); } }}>
          <td><strong>{symbol.replace("USDT", "")}</strong><small> / USDT</small><span className={`scanner-source ${market?.status === "live" ? "live" : ""}`}>{market?.status === "live" ? "LIVE" : market?.status?.toUpperCase() ?? "AGUARDANDO"}</span></td>
          <td className="mono">{fmtPrice(market?.price, market?.price && market.price < 10 ? 4 : 2)}</td>
          <td className={`mono ${(market?.change24h ?? 0) >= 0 ? "pos" : "neg"}`}>{market?.change24h == null ? "—" : `${market.change24h >= 0 ? "+" : ""}${market.change24h.toFixed(2)}%`}</td>
          <td className="mono">{market?.quoteVolume24h == null ? "—" : `$${fmtCompact(market.quoteVolume24h)}`}</td>
          <td className="mono">{itemFlow?.relativeFlow15m == null ? <span className="subtle">Aquecendo / cobertura insuficiente</span> : <strong className={itemFlow.relativeFlow15m >= 1.5 ? "amber" : ""}>{itemFlow.relativeFlow15m.toFixed(2)}×</strong>}</td>
          <td className={`mono ${(itemFlow?.cvdNotional15m ?? 0) >= 0 ? "pos" : "neg"}`}>{itemFlow?.cvdNotional15m == null ? "—" : `${itemFlow.cvdNotional15m >= 0 ? "+" : "−"}$${fmtCompact(Math.abs(itemFlow.cvdNotional15m))}`}</td>
        </tr>)}{rows.length === 0 && <tr><td colSpan={6} className="scanner-empty">Nenhum ativo corresponde ao filtro.</td></tr>}</tbody>
      </table>
    </div>
    <div className="disclaimer">Ritmo de fluxo compara o notional observado no bloco UTC de 15m em andamento com os três blocos anteriores; só aparece após cobertura suficiente. CVD e fluxo histórico usam aggTrades persistidos da Binance, não todas as corretoras.</div>
  </section>;
}
