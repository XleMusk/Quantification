import { memo, useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart, BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, MarkLineComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

function ChartView({ x, values, label, kind = 'line', percentage = false, color = '#3779da', compact = false }: { x: string[]; values: (number | null)[]; label: string; kind?: 'line' | 'bar'; percentage?: boolean; color?: string; compact?: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current);
    chart.setOption({ animation: false, color: [color], textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif', fontSize: 11 },
      grid: { top: compact ? 6 : 16, bottom: compact ? 4 : 28, left: 45, right: 14, containLabel: true },
      tooltip: { trigger: 'axis', confine: true, backgroundColor: '#202930', borderColor: '#3b4853', textStyle: { color: '#dbe3e9' }, valueFormatter: (v: unknown) => v == null ? '—' : percentage ? `${(Number(v) * 100).toFixed(3)}%` : Number(v).toFixed(4) },
      xAxis: { type: 'category', data: x, boundaryGap: kind === 'bar', axisTick: { show: false }, axisLine: { lineStyle: { color: '#34414b' } }, axisLabel: { color: '#91a1ae', hideOverlap: true } },
      yAxis: { type: 'value', scale: kind === 'line', splitNumber: compact ? 2 : 4, axisLabel: { color: '#91a1ae', hideOverlap: true, formatter: percentage ? (v: number) => `${(v * 100).toFixed(1)}%` : undefined }, splitLine: { lineStyle: { color: '#29343d' } } },
      series: [{ type: kind, data: values, showSymbol: false, connectNulls: false, barMaxWidth: 38, lineStyle: { width: 2 }, name: label }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [x, values, label, kind, percentage, color, compact]);
  return <div className="chart" ref={container} role="img" aria-label={label} />;
}
export const Chart = memo(ChartView, (a, b) => a.label === b.label && a.kind === b.kind && a.percentage === b.percentage && a.color === b.color && a.compact === b.compact && JSON.stringify(a.x) === JSON.stringify(b.x) && JSON.stringify(a.values) === JSON.stringify(b.values));
