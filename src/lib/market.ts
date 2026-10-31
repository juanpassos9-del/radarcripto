export type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number };
export type VwapPoint = { time: number; value: number };
export type ChartOverlays = {
  symbol: string; interval: string; timezone: "UTC";
  vwap: { daily: VwapPoint[]; weekly: VwapPoint[]; monthly: VwapPoint[] };
  previous: { day: { high: number | null; low: number | null }; week: { high: number | null; low: number | null }; month: { high: number | null; low: number | null } };
  source: string; receivedAt: number;
};
export type Trade = { id: number | string; time: number; price: number; quantity: number; side: "buy" | "sell"; source?: string };
export type Level = { price: number; quantity: number; total: number; sources?: string[] };
export type MarketSnapshot = {
  symbol: string; price: number | null; change24h: number | null; volume24h: number | null;
  quoteVolume24h?: number | null; high24h: number | null; low24h: number | null; source: string; eventTime: number | null;
  receivedAt: number; latencyMs: number | null; status: "live" | "stale" | "unavailable";
};
export type ScannerFlowAsset = { symbol: string; notional15m: number | null; cvdNotional15m: number | null; relativeFlow15m: number | null; observedBuckets: number; lastObservedBucket: number | null };
export type ScannerFlowSnapshot = { assets: Record<string, ScannerFlowAsset>; bucketStartedAt: number; source: string; receivedAt: number; relativeFlowMethod: string };
export type ContextMetric = { value: number; change24hPct: number | null; timestamp: number; source: string; status: string } | null;
export type MarketContextData = { fearAndGreed: ({ value: number; label: string; timestamp: number; source: string; status: string } | null); tvl: ContextMetric; stablecoinSupply: ContextMetric; receivedAt: number };
export type BookData = { bids: Level[]; asks: Level[]; spread: number; imbalance: number; eventTime: number | null; receivedAt: number; status: string; source?: string; sources?: Record<string, string> };
export type ReversalSignal = {
  time: number; symbol: string; interval: string; direction: "BUY" | "SELL"; price: number; bandPrice: number;
  sigma: number; source: "GARCH" | "HV252"; sources: Array<"GARCH" | "HV252">; pattern: string;
  score: number; strength: "fraca" | "moderada" | "forte"; volumeRatio: number | null; qualified: boolean; reasons: string[]; suppressedByCooldown?: boolean;
};
export type ReversalSignalData = {
  symbol: string; interval: string; signals: ReversalSignal[]; latest: ReversalSignal | null;
  lastCandidate: ReversalSignal | null; suppressedByCooldown: number;
  settings: { minSigma: number; touchTolerance: number; volumeMultiple: number; volumeLookback: number; minScore: number; cooldown: number; buyEnabled: boolean; sellEnabled: boolean; garchEnabled: boolean; hvEnabled: boolean; volumeEnabled: boolean };
  scoreRules: Record<string, string | number>;
};
export type BreakoutSignal = {
  time: number; symbol: string; interval: string; direction: "BUY" | "SELL"; price: number;
  rangeHigh: number; rangeLow: number; volumeRatio: number; compressionPercentile: number;
  qualified: boolean; compressedAt: number; reasons: string[];
};
export type BreakoutSignalData = {
  symbol: string; interval: string; status: string; requiredCandles?: number; observedCandles?: number;
  signals: BreakoutSignal[]; latest: BreakoutSignal | null; lastCandidate?: BreakoutSignal | null;
  settings: { bbPeriod: number; compressionLookback: number; compressionPercentile: number; compressionRecentBars: number; rangeLookback: number; volumeLookback: number; volumeMultiple: number; cooldown: number; buyEnabled: boolean; sellEnabled: boolean };
};
export type VolatilityZones = {
  symbol: string; status: string; source?: string; reference?: number; referenceMode?: string; asOf?: number; receivedAt?: number; observations?: number; required?: number;
  garch?: { dailyVol: number; annualizedVol: number; bands: Record<string, { upper: number; lower: number }> };
  hv252?: { status: string; dailyVol: number | null; annualizedVol: number | null; observations: number; bands: Record<string, { upper: number | null; lower: number | null }> };
  method?: string; lookahead?: string;
};

export async function getJson<T>(path: string): Promise<T> {
  const apiOrigin = process.env.NEXT_PUBLIC_MARKET_API_URL?.replace(/\/+$/, "");
  const url = apiOrigin ? new URL(path, apiOrigin).toString() : path;
  const response = await authorizedFetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

const apiTokens = new Map<string, { token: string; expiresAt: number }>();

async function getMarketToken(use: "api" | "ws", forceRefresh = false): Promise<string> {
  const cached = apiTokens.get(use);
  if (!forceRefresh && cached && cached.expiresAt > Date.now() + 5000) return cached.token;
  const response = await fetch(`/api/auth/market-token?use=${use}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Não foi possível autenticar na API de mercado (${response.status}).`);
  const data = await response.json() as { token: string; expiresIn: number };
  apiTokens.set(use, { token: data.token, expiresAt: Date.now() + data.expiresIn * 1000 });
  return data.token;
}

async function authorizedFetch(url: string): Promise<Response> {
  let token = await getMarketToken("api");
  let response = await fetch(url, { cache: "no-store", headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 401) {
    token = await getMarketToken("api", true);
    response = await fetch(url, { cache: "no-store", headers: { Authorization: `Bearer ${token}` } });
  }
  return response;
}

export function getMarketWsToken() {
  return getMarketToken("ws");
}
export const marketDataLabel = "BINANCE SPOT · USDT";
export const fmtPrice = (value: number | null | undefined, digits = 2) => value == null ? "—" : new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
export const fmtCompact = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(value);
export const fmtTime = (timestamp: number | null | undefined) => timestamp == null ? "—" : new Date(timestamp).toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo", hour12: false });
