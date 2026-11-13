"use client";
import { useEffect, useRef } from "react";
import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle, TickMarkType, type CreatePriceLineOptions, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type Time, type UTCTimestamp } from "lightweight-charts";
import type { BreakoutSignal, Candle, ChartOverlays, ReversalSignal, VolatilityZones } from "@/lib/market";

const CHART_TIME_ZONE = "America/Sao_Paulo";
const chartDate = (time: Time): Date => typeof time === "number"
  ? new Date(time * 1000)
  : typeof time === "string"
    ? new Date(`${time}T00:00:00Z`)
    : new Date(Date.UTC(time.year, time.month - 1, time.day));
const formatChartTick = (time: Time, tickType: TickMarkType): string => {
  const date = chartDate(time);
  if (tickType === TickMarkType.Year) return new Intl.DateTimeFormat("pt-BR", { year: "numeric", timeZone: CHART_TIME_ZONE }).format(date);
  if (tickType === TickMarkType.Month) return new Intl.DateTimeFormat("pt-BR", { month: "short", timeZone: CHART_TIME_ZONE }).format(date).replace(".", "");
  if (tickType === TickMarkType.DayOfMonth) return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", timeZone: CHART_TIME_ZONE }).format(date);
  const options: Intl.DateTimeFormatOptions = tickType === TickMarkType.TimeWithSeconds
    ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZone: CHART_TIME_ZONE }
    : { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: CHART_TIME_ZONE };
  return new Intl.DateTimeFormat("pt-BR", options).format(date);
};
const formatChartCrosshairTime = (time: Time) => new Intl.DateTimeFormat("pt-BR", {
  timeZone: CHART_TIME_ZONE, day: "2-digit", month: "2-digit", year: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
}).format(chartDate(time));

export type RiskRewardLevels = { entry: number | null; stop: number | null };
type RiskRewardLevel = keyof RiskRewardLevels;
type RiskRewardLine = RiskRewardLevel | "target1" | "target2";

export default function MarketChart({ candles, symbol, interval, zones, showGarch, showHv, showVolumeAnomalies, overlays, showVwaps, showPreviousLevels, signals, breakoutSignals, riskRewardMode = false, riskRewardLevels, onRiskRewardPoint, onRiskRewardLevelChange }: { candles: Candle[]; symbol: string; interval: string; zones: VolatilityZones | null; showGarch: boolean; showHv: boolean; showVolumeAnomalies: boolean; overlays: ChartOverlays | null; showVwaps: boolean; showPreviousLevels: boolean; signals: ReversalSignal[]; breakoutSignals: BreakoutSignal[]; riskRewardMode?: boolean; riskRewardLevels?: RiskRewardLevels; onRiskRewardPoint?: (price: number) => void; onRiskRewardLevelChange?: (level: RiskRewardLevel, price: number) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const hasMultipleCandles = candles.length >= 2;
  const firstCandleTime = candles[0]?.time;
  const lastCandleTime = candles[candles.length - 1]?.time;
  const chartRef = useRef<ReturnType<typeof createChart> | null>(null);
  const candleRef = useRef<ReturnType<ReturnType<typeof createChart>["addSeries"]> | null>(null);
  const volumeRef = useRef<ReturnType<ReturnType<typeof createChart>["addSeries"]> | null>(null);
  const renderedDataRef = useRef<{ key: string; first: number; last: number; length: number; anomalies: boolean } | null>(null);
  const zoneSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const referenceSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const riskRewardLinesRef = useRef<Partial<Record<RiskRewardLine, IPriceLine>>>({});
  const riskRewardLevelsRef = useRef<RiskRewardLevels | undefined>(riskRewardLevels);
  const onRiskRewardLevelChangeRef = useRef(onRiskRewardLevelChange);
  riskRewardLevelsRef.current = riskRewardLevels;
  onRiskRewardLevelChangeRef.current = onRiskRewardLevelChange;

  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, {
      width: host.current.clientWidth, height: host.current.clientHeight,
      layout: { background: { type: ColorType.Solid, color: "#0d1219" }, textColor: "#778596", fontFamily: "DM Mono, monospace", fontSize: 10 },
      localization: { locale: "pt-BR", timeFormatter: formatChartCrosshairTime },
      grid: { vertLines: { color: "#17202a" }, horzLines: { color: "#17202a" } },
      crosshair: { mode: CrosshairMode.Normal }, rightPriceScale: { borderColor: "#27313c", scaleMargins: { top: 0.08, bottom: 0.2 } }, timeScale: { borderColor: "#27313c", timeVisible: true, secondsVisible: false, rightOffset: 16, tickMarkFormatter: formatChartTick },
    });
    const candle = chart.addSeries(CandlestickSeries, { upColor: "#44d49a", downColor: "#ff667c", borderUpColor: "#44d49a", borderDownColor: "#ff667c", wickUpColor: "#44d49a", wickDownColor: "#ff667c", priceLineColor: "#28d6c5" });
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "volume" });
    markersRef.current = createSeriesMarkers(candle, []);
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    chartRef.current = chart; candleRef.current = candle; volumeRef.current = volume;
    const observer = new ResizeObserver(entries => { const rect = entries[0]?.contentRect; if (rect) chart.applyOptions({ width: rect.width, height: rect.height }); });
    observer.observe(host.current);
    return () => { observer.disconnect(); markersRef.current?.detach(); markersRef.current = null; chart.remove(); chartRef.current = null; };
  }, []);

  useEffect(() => {
    const reversalMarkers = signals.map(signal => ({
      id: `${signal.symbol}-${signal.interval}-${signal.time}-${signal.direction}`,
      time: signal.time as UTCTimestamp,
      position: signal.direction === "BUY" ? "belowBar" as const : "aboveBar" as const,
      shape: signal.direction === "BUY" ? "arrowUp" as const : "arrowDown" as const,
      color: signal.direction === "BUY" ? "#44d49a" : "#ff667c",
      text: `${signal.direction} ${signal.score}`,
      size: 1,
    }));
    const breakoutMarkers = breakoutSignals.map(signal => ({
      id: `breakout-${signal.symbol}-${signal.interval}-${signal.time}-${signal.direction}`,
      time: signal.time as UTCTimestamp,
      position: signal.direction === "BUY" ? "belowBar" as const : "aboveBar" as const,
      shape: signal.direction === "BUY" ? "arrowUp" as const : "arrowDown" as const,
      color: signal.direction === "BUY" ? "#32c9df" : "#ffae55",
      text: signal.direction === "BUY" ? "ROMP. ↑" : "ROMP. ↓",
      size: 1,
    }));
    markersRef.current?.setMarkers([...reversalMarkers, ...breakoutMarkers].sort((a, b) => Number(a.time) - Number(b.time)));
  }, [signals, breakoutSignals]);

  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    if (!chart || !candle || !riskRewardMode || !onRiskRewardPoint) return;
    const handler = (param: { point?: { x: number; y: number } | null }) => {
      const point = param.point;
      const hostHeight = host.current?.clientHeight ?? 0;
      if (!point || point.y < 0 || point.y > hostHeight * 0.78) return;
      const price = (candle as { coordinateToPrice: (coordinate: number) => number | null }).coordinateToPrice(point.y);
      if (price != null && Number.isFinite(price) && price > 0) onRiskRewardPoint(price);
    };
    chart.subscribeClick(handler);
    return () => chart.unsubscribeClick(handler);
  }, [riskRewardMode, onRiskRewardPoint]);

  useEffect(() => {
    const hostElement = host.current;
    const candle = candleRef.current as { coordinateToPrice: (coordinate: number) => number | null; priceToCoordinate: (price: number) => number | null } | null;
    if (!hostElement || !candle) return;
    let dragging: RiskRewardLevel | null = null;
    const onPointerDown = (event: PointerEvent) => {
      if (riskRewardMode) return;
      const levels = riskRewardLevelsRef.current;
      if (!levels) return;
      const rect = hostElement.getBoundingClientRect();
      const y = event.clientY - rect.top;
      if (y < 0 || y > hostElement.clientHeight * 0.78) return;
      const candidates = (Object.entries(levels) as [RiskRewardLevel, number | null][])
        .filter((entry): entry is [RiskRewardLevel, number] => entry[1] != null)
        .map(([level, price]) => ({ level, distance: Math.abs((candle.priceToCoordinate(price) ?? Number.POSITIVE_INFINITY) - y) }))
        .sort((a, b) => a.distance - b.distance);
      const nearest = candidates[0];
      if (!nearest || nearest.distance > 9) return;
      dragging = nearest.level;
      event.preventDefault();
      event.stopPropagation();
      hostElement.setPointerCapture(event.pointerId);
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!dragging) return;
      const rect = hostElement.getBoundingClientRect();
      const y = Math.max(0, Math.min(hostElement.clientHeight * 0.78, event.clientY - rect.top));
      const price = candle.coordinateToPrice(y);
      if (price != null && Number.isFinite(price) && price > 0) onRiskRewardLevelChangeRef.current?.(dragging, price);
    };
    const onPointerUp = () => { dragging = null; };
    hostElement.addEventListener("pointerdown", onPointerDown, true);
    hostElement.addEventListener("pointermove", onPointerMove);
    hostElement.addEventListener("pointerup", onPointerUp);
    hostElement.addEventListener("pointercancel", onPointerUp);
    return () => {
      hostElement.removeEventListener("pointerdown", onPointerDown, true);
      hostElement.removeEventListener("pointermove", onPointerMove);
      hostElement.removeEventListener("pointerup", onPointerUp);
      hostElement.removeEventListener("pointercancel", onPointerUp);
    };
  }, [riskRewardMode]);

  useEffect(() => {
    const candle = candleRef.current as { createPriceLine: (options: CreatePriceLineOptions) => IPriceLine; removePriceLine: (line: IPriceLine) => void } | null;
    if (!candle) return;
    const levels = riskRewardLevels ?? { entry: null, stop: null };
    const risk = levels.entry != null && levels.stop != null ? Math.abs(levels.entry - levels.stop) : null;
    const direction = levels.entry != null && levels.stop != null && levels.stop < levels.entry ? 1 : -1;
    const target1 = risk != null && risk > 0 && levels.entry != null ? levels.entry + direction * risk : null;
    const target2 = risk != null && risk > 0 && levels.entry != null ? levels.entry + direction * risk * 2 : null;
    const options: Record<RiskRewardLine, { price: number | null; color: string; title: string; lineStyle: LineStyle }> = {
      entry: { price: levels.entry, color: "#f0bd61", title: "ENTRADA", lineStyle: LineStyle.Solid },
      stop: { price: levels.stop, color: "#ff667c", title: "STOP", lineStyle: LineStyle.Dashed },
      target1: { price: target1, color: "#44d49a", title: "ALVO · 1R", lineStyle: LineStyle.Dashed },
      target2: { price: target2, color: "#28a879", title: "ALVO · 2R", lineStyle: LineStyle.Dashed },
    };
    for (const level of Object.keys(options) as RiskRewardLine[]) {
      const option = options[level];
      const current = riskRewardLinesRef.current[level];
      if (option.price == null) {
        if (current) candle.removePriceLine(current);
        delete riskRewardLinesRef.current[level];
      } else if (current) {
        current.applyOptions({ price: option.price, title: option.title });
      } else {
        riskRewardLinesRef.current[level] = candle.createPriceLine({ price: option.price, color: option.color, lineWidth: 2, lineStyle: option.lineStyle, axisLabelVisible: true, title: option.title });
      }
    }
  }, [riskRewardLevels]);

  useEffect(() => {
    const candleSeries = candleRef.current as { setData: (data: unknown[]) => void; update: (data: unknown) => void } | null;
    const volumeSeries = volumeRef.current as { setData: (data: unknown[]) => void; update: (data: unknown) => void } | null;
    if (!candleSeries || !volumeSeries || candles.length === 0) return;
    const mapCandle = (c: Candle, index: number) => {
      const previous = candles.slice(Math.max(0, index - 20), index);
      const averageVolume = previous.length === 20 ? previous.reduce((sum, item) => sum + item.volume, 0) / 20 : null;
      const ratio = averageVolume && averageVolume > 0 ? c.volume / averageVolume : null;
      const anomaly = showVolumeAnomalies && ratio != null && ratio >= 1.5;
      const bullish = c.close >= c.open;
      const color = bullish ? (anomaly ? "#7affc4" : "#44d49a") : (anomaly ? "#ff9a58" : "#ff667c");
      return {
        candle: { time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close, color, borderColor: color, wickColor: color },
        volume: { time: c.time as UTCTimestamp, value: c.volume, color: bullish ? (anomaly ? "#44d49a" : "#44d49a35") : (anomaly ? "#ff667c" : "#ff667c35") },
      };
    };
    const key = `${symbol}:${interval}`;
    const first = candles[0].time;
    const last = candles[candles.length - 1].time;
    const previous = renderedDataRef.current;
    const reset = !previous || previous.key !== key || previous.first !== first || previous.anomalies !== showVolumeAnomalies || candles.length < previous.length || candles.length > previous.length + 1;
    if (reset) {
      const mapped = candles.map(mapCandle);
      candleSeries.setData(mapped.map(item => item.candle));
      volumeSeries.setData(mapped.map(item => item.volume));
      chartRef.current?.timeScale().fitContent();
    } else if (last === previous.last || candles.length === previous.length + 1) {
      const mapped = mapCandle(candles[candles.length - 1], candles.length - 1);
      candleSeries.update(mapped.candle);
      volumeSeries.update(mapped.volume);
    } else {
      const mapped = candles.map(mapCandle);
      candleSeries.setData(mapped.map(item => item.candle));
      volumeSeries.setData(mapped.map(item => item.volume));
    }
    renderedDataRef.current = { key, first, last, length: candles.length, anomalies: showVolumeAnomalies };
  }, [candles, symbol, interval, showVolumeAnomalies]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || firstCandleTime == null || lastCandleTime == null || !hasMultipleCandles) return;
    for (const series of zoneSeriesRef.current) chart.removeSeries(series);
    zoneSeriesRef.current = [];
    if (!zones || zones.status !== "ready" || zones.reference == null) return;
    const first = firstCandleTime as UTCTimestamp;
    const last = lastCandleTime as UTCTimestamp;
    const levels: { title: string; value: number | null | undefined; color: string; width?: 1 | 2 | 3 | 4; style?: LineStyle }[] = [];
    levels.push({ title: "0σ", value: zones.reference, color: "#f0bd61", width: 2 });
    if (showGarch && zones.garch) {
      const palette: Record<string, string> = { "0.5": "#f47a8c", "1": "#ff667c", "1.5": "#fa8494", "2": "#ff415e", "2.5": "#e53955", "3": "#bd334a" };
      for (const [multiple, band] of Object.entries(zones.garch.bands)) {
        if (multiple === "2.5") continue;
        const color = palette[multiple] ?? "#ff667c";
        levels.push({ title: `+${multiple}σ`, value: band.upper, color, width: multiple === "2" || multiple === "3" ? 2 : 1, style: multiple === "0.5" || multiple === "1.5" ? LineStyle.Dashed : LineStyle.Solid });
        levels.push({ title: `−${multiple}σ`, value: band.lower, color: multiple === "3" ? "#43d49a" : "#49bd91", width: multiple === "2" || multiple === "3" ? 2 : 1, style: multiple === "0.5" || multiple === "1.5" ? LineStyle.Dashed : LineStyle.Solid });
      }
    }
    if (showHv && zones.hv252?.bands["1"]?.upper != null) {
      levels.push({ title: "+1σ", value: zones.hv252.bands["1"].upper, color: "#4d9bff", width: 2, style: LineStyle.Dashed });
      levels.push({ title: "−1σ", value: zones.hv252.bands["1"].lower, color: "#4d9bff", width: 2, style: LineStyle.Dashed });
    }
    for (const level of levels) {
      if (level.value == null || !Number.isFinite(level.value)) continue;
      const series = chart.addSeries(LineSeries, { color: level.color, lineWidth: level.width ?? 1, lineStyle: level.style ?? LineStyle.Solid, title: level.title, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, pointMarkersVisible: false, crosshairMarkerRadius: 0, autoscaleInfoProvider: () => null });
      series.setData([{ time: first, value: level.value }, { time: last, value: level.value }]);
      zoneSeriesRef.current.push(series);
    }
    return () => {
      const currentChart = chartRef.current;
      if (currentChart) for (const series of zoneSeriesRef.current) currentChart.removeSeries(series);
      zoneSeriesRef.current = [];
    };
  }, [firstCandleTime, lastCandleTime, hasMultipleCandles, zones, showGarch, showHv]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || firstCandleTime == null || lastCandleTime == null || !hasMultipleCandles) return;
    for (const series of referenceSeriesRef.current) chart.removeSeries(series);
    referenceSeriesRef.current = [];
    const addLine = (title: string, color: string, points: { time: number; value: number }[], dashed = false) => {
      if (points.length < 2) return;
      const series = chart.addSeries(LineSeries, {
        color, lineWidth: 2, lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
        title, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, pointMarkersVisible: false, crosshairMarkerRadius: 0,
        autoscaleInfoProvider: () => null,
      });
      series.setData(points.map(point => ({ time: point.time as UTCTimestamp, value: point.value })));
      referenceSeriesRef.current.push(series);
    };
    const first = firstCandleTime as UTCTimestamp;
    const last = lastCandleTime as UTCTimestamp;
    const matchingOverlays = overlays?.symbol === symbol && overlays.interval === interval ? overlays : null;
    if (matchingOverlays && showVwaps) {
      addLine("VWAP D", "#f0bd61", matchingOverlays.vwap.daily);
      addLine("VWAP S", "#4d9bff", matchingOverlays.vwap.weekly);
      addLine("VWAP M", "#bd82ff", matchingOverlays.vwap.monthly);
    }
    if (matchingOverlays && showPreviousLevels) {
      const levels = [
        { title: "D−1 Máx", value: matchingOverlays.previous.day.high, color: "#ff667c" },
        { title: "D−1 Mín", value: matchingOverlays.previous.day.low, color: "#44d49a" },
        { title: "S−1 Máx", value: matchingOverlays.previous.week.high, color: "#ff9a58" },
        { title: "S−1 Mín", value: matchingOverlays.previous.week.low, color: "#28d6c5" },
        { title: "M−1 Máx", value: matchingOverlays.previous.month.high, color: "#e86cff" },
        { title: "M−1 Mín", value: matchingOverlays.previous.month.low, color: "#7aa2ff" },
      ];
      for (const level of levels) {
        if (level.value == null || !Number.isFinite(level.value)) continue;
        addLine(level.title, level.color, [{ time: first, value: level.value }, { time: last, value: level.value }], true);
      }
    }
    return () => {
      const currentChart = chartRef.current;
      if (currentChart) for (const series of referenceSeriesRef.current) currentChart.removeSeries(series);
      referenceSeriesRef.current = [];
    };
  }, [firstCandleTime, lastCandleTime, hasMultipleCandles, overlays, showVwaps, showPreviousLevels, symbol, interval]);

  return <div ref={host} aria-label={`Gráfico de candles de ${symbol}`} />;
}
