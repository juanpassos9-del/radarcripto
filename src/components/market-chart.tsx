"use client";
import { useEffect, useRef } from "react";
import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle, type ISeriesApi, type ISeriesMarkersPluginApi, type Time, type UTCTimestamp } from "lightweight-charts";
import type { Candle, ChartOverlays, ReversalSignal, VolatilityZones } from "@/lib/market";

export default function MarketChart({ candles, symbol, interval, zones, showGarch, showHv, showVolumeAnomalies, overlays, showVwaps, showPreviousLevels, signals }: { candles: Candle[]; symbol: string; interval: string; zones: VolatilityZones | null; showGarch: boolean; showHv: boolean; showVolumeAnomalies: boolean; overlays: ChartOverlays | null; showVwaps: boolean; showPreviousLevels: boolean; signals: ReversalSignal[] }) {
  const host = useRef<HTMLDivElement>(null);
  const hasMultipleCandles = candles.length >= 2;
  const firstCandleTime = candles[0]?.time;
  const lastCandleTime = candles[candles.length - 1]?.time;
  const chartRef = useRef<ReturnType<typeof createChart> | null>(null);
  const candleRef = useRef<ReturnType<ReturnType<typeof createChart>["addSeries"]> | null>(null);
  const volumeRef = useRef<ReturnType<ReturnType<typeof createChart>["addSeries"]> | null>(null);
  const zoneSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const referenceSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, {
      width: host.current.clientWidth, height: host.current.clientHeight,
      layout: { background: { type: ColorType.Solid, color: "#0d1219" }, textColor: "#778596", fontFamily: "DM Mono, monospace", fontSize: 10 },
      grid: { vertLines: { color: "#17202a" }, horzLines: { color: "#17202a" } },
      crosshair: { mode: CrosshairMode.Normal }, rightPriceScale: { borderColor: "#27313c" }, timeScale: { borderColor: "#27313c", timeVisible: true, secondsVisible: false },
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
    markersRef.current?.setMarkers(signals.map(signal => ({
      id: `${signal.symbol}-${signal.interval}-${signal.time}-${signal.direction}`,
      time: signal.time as UTCTimestamp,
      position: signal.direction === "BUY" ? "belowBar" : "aboveBar",
      shape: signal.direction === "BUY" ? "arrowUp" : "arrowDown",
      color: signal.direction === "BUY" ? "#44d49a" : "#ff667c",
      text: `${signal.direction} ${signal.score}`,
      size: 1,
    })));
  }, [signals]);

  useEffect(() => {
    const candleSeries = candleRef.current as { setData: (data: unknown[]) => void } | null;
    const volumeSeries = volumeRef.current as { setData: (data: unknown[]) => void } | null;
    if (!candleSeries || !volumeSeries || candles.length === 0) return;
    const mapped = candles.map((c, index) => {
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
    });
    candleSeries.setData(mapped.map(item => item.candle));
    volumeSeries.setData(mapped.map(item => item.volume));
    chartRef.current?.timeScale().fitContent();
  }, [candles, symbol, showVolumeAnomalies]);

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
      const series = chart.addSeries(LineSeries, { color: level.color, lineWidth: level.width ?? 1, lineStyle: level.style ?? LineStyle.Solid, title: level.title, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, pointMarkersVisible: false, crosshairMarkerRadius: 0 });
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
