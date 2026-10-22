import { registerOverlay, type Coordinate, type OverlayCreateFiguresCallbackParams, type OverlayFigure, type OverlayTemplate } from 'klinecharts';

/** Geometric study tools only: these neither submit orders nor alter account state. */
export const REVIEW_EXTRA_TOOLS: readonly (readonly [string, string])[] = [
  ['Q_priceRange', '价格区间测量'], ['Q_dateRange', '时间区间测量'], ['Q_range', '价格与时间测量'],
  ['Q_longPosition', '多头盈亏比'], ['Q_shortPosition', '空头盈亏比'],
  ['Q_fibExtension', '斐波那契扩展'], ['Q_pitchfork', '安德鲁分叉线'], ['Q_ellipse', '椭圆区域'],
];

export type TradePlanInput = {
  side: 'long' | 'short'; entry: number; stop: number; target: number;
  equity?: number; riskPct?: number; lotSize?: number;
};
export type TradePlan = {
  rr: number; riskRewardRatio: number; unitRisk: number; unitReward: number;
  riskPercent: number; rewardPercent: number;
  shares: number | null; lossBudget: number | null; estimatedLoss: number | null; notional: number | null;
};

/** Pre-fee, pre-slippage risk-budget arithmetic, independent from executable orders. */
export function computeTradePlan(input: TradePlanInput): TradePlan {
  const { side, entry, stop, target, equity, riskPct, lotSize = 100 } = input;
  if (!['long', 'short'].includes(side)) throw new Error('方向须为 long 或 short');
  if (![entry, stop, target].every(value => Number.isFinite(value) && value > 0)) throw new Error('入场、止损和目标价须为正数');
  const oriented = side === 'long' ? stop < entry && entry < target : target < entry && entry < stop;
  if (!oriented) throw new Error(side === 'long' ? '多头须满足：止损 < 入场 < 目标' : '空头须满足：目标 < 入场 < 止损');
  if (!Number.isSafeInteger(lotSize) || lotSize < 1) throw new Error('每手股数须为正整数');
  if ((equity == null) !== (riskPct == null)) throw new Error('风险预算需同时填写权益和风险比例');
  if (equity != null && (!Number.isFinite(equity) || equity <= 0 || !Number.isFinite(riskPct) || riskPct! <= 0 || riskPct! > 100)) throw new Error('权益须为正数，风险比例须在 0 至 100% 之间');
  const unitRisk = Math.abs(entry - stop);
  const unitReward = Math.abs(target - entry);
  const lossBudget = equity == null ? null : equity * riskPct! / 100;
  const shares = lossBudget == null ? null : Math.floor(lossBudget / unitRisk / lotSize + 1e-12) * lotSize;
  const result: TradePlan = {
    rr: unitReward / unitRisk, riskRewardRatio: unitReward / unitRisk, unitRisk, unitReward,
    riskPercent: unitRisk / entry * 100, rewardPercent: unitReward / entry * 100,
    shares, lossBudget, estimatedLoss: shares == null ? null : shares * unitRisk,
    notional: shares == null ? null : shares * entry,
  };
  if (Object.values(result).some(value => value != null && !Number.isFinite(value)) || (shares != null && !Number.isSafeInteger(shares))) throw new Error('数值超出可计算范围');
  return result;
}

const label = (x: number, y: number, text: string, color = '#d9b65a'): OverlayFigure => ({
  type: 'text', ignoreEvent: true, attrs: { x, y, text, baseline: 'bottom', align: 'left' },
  styles: { color, size: 11, backgroundColor: '#182127dd', paddingLeft: 4, paddingRight: 4, paddingTop: 3, paddingBottom: 3 },
});
const line = (coordinates: Coordinate[], color?: string): OverlayFigure => ({
  type: 'line', attrs: { coordinates }, ...(color ? { styles: { color, size: 1 } } : {}),
});
function boundedTextPoint(p: Coordinate, bounds: { width: number; height: number }, textWidth = 180): Coordinate {
  return { x: Math.max(2, Math.min(p.x, Math.max(2, bounds.width - textWidth))), y: Math.max(18, Math.min(p.y - 7, bounds.height - 4)) };
}
function price(value: number | undefined, digits: number): string { return value != null && Number.isFinite(value) ? value.toFixed(Math.min(8, Math.max(0, digits))) : '—'; }
function signed(value: number, digits = 2): string { return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`; }
function coordinatesReady(params: OverlayCreateFiguresCallbackParams, count: number): boolean {
  return params.coordinates.length >= count && params.coordinates.slice(0, count).every(p => Number.isFinite(p.x) && Number.isFinite(p.y));
}
function measurement(params: OverlayCreateFiguresCallbackParams, mode: 'price' | 'date' | 'both'): OverlayFigure[] {
  if (!coordinatesReady(params, 2)) return [];
  const { coordinates: [a, b], overlay, bounding, precision, xAxis } = params;
  const first = overlay.points[0]; const second = overlay.points[1];
  const pieces: string[] = [];
  if (mode !== 'date' && first.value != null && second.value != null) {
    const difference = second.value - first.value;
    pieces.push(`Δ ${signed(difference, precision.price)}`);
    if (first.value !== 0) pieces.push(`${signed(difference / first.value * 100)}%`);
  }
  if (mode !== 'price') {
    const firstIndex = first.dataIndex ?? xAxis?.convertFromPixel(a.x);
    const secondIndex = second.dataIndex ?? xAxis?.convertFromPixel(b.x);
    const observedAnchors = Number.isFinite(first.timestamp) && Number.isFinite(second.timestamp);
    if (firstIndex != null && secondIndex != null && Number.isFinite(firstIndex) && Number.isFinite(secondIndex)) {
      const bars = Math.abs(Math.round(secondIndex) - Math.round(firstIndex)) + 1;
      pieces.push(observedAnchors ? `观测K线 ${bars} 根` : `横向间距 ${bars} 根（含空白区）`);
    }
    if (observedAnchors) pieces.push(`相隔 ${Math.round(Math.abs(second.timestamp! - first.timestamp!) / 86400000)} 日历日`);
  }
  const anchor = boundedTextPoint({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y) }, bounding, 260);
  return [
    { type: 'rect', attrs: { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.max(1, Math.abs(b.x - a.x)), height: Math.max(1, Math.abs(b.y - a.y)) }, styles: { style: 'stroke_fill', color: '#d8a64616', borderColor: '#d8a646', borderSize: 1 } },
    line([a, b]), label(anchor.x, anchor.y, pieces.join(' · ')),
  ];
}
function position(params: OverlayCreateFiguresCallbackParams, side: 'long' | 'short'): OverlayFigure[] {
  if (!coordinatesReady(params, 2)) return [];
  const { coordinates, overlay, bounding, precision } = params;
  const [entryPoint, stopPoint, targetPoint] = coordinates;
  const [entry, stop, target] = overlay.points.map(p => p.value);
  const anchor = boundedTextPoint(entryPoint, bounding, 290);
  if (coordinates.length < 3 || entry == null || stop == null || target == null) {
    return [line([entryPoint, stopPoint]), label(anchor.x, anchor.y, '依次选择：入场价 → 止损价 → 目标价')];
  }
  let plan: TradePlan;
  try { plan = computeTradePlan({ side, entry, stop, target, lotSize: 1 }); }
  catch (error) { return [line([entryPoint, stopPoint, targetPoint], '#e57679'), label(anchor.x, anchor.y, (error as Error).message, '#e57679')]; }
  const left = Math.min(entryPoint.x, stopPoint.x, targetPoint.x);
  const right = Math.max(entryPoint.x + 90, stopPoint.x, targetPoint.x);
  const figures: OverlayFigure[] = [];
  for (const [point, color] of [[stopPoint, '#d96c73'], [targetPoint, '#4bba91']] as const) {
    figures.push({ type: 'rect', attrs: { x: left, y: Math.min(entryPoint.y, point.y), width: right - left, height: Math.abs(entryPoint.y - point.y) }, styles: { style: 'stroke_fill', color: `${color}30`, borderColor: color, borderSize: 1 } });
    figures.push(line([{ x: left, y: point.y }, { x: right, y: point.y }], color));
  }
  figures.push(line([{ x: left, y: entryPoint.y }, { x: right, y: entryPoint.y }], '#d6dfe7'));
  figures.push(label(Math.max(2, left), Math.max(18, Math.min(entryPoint.y - 7, bounding.height - 4)), `${side === 'long' ? '多头' : '空头'} RR ${plan.rr.toFixed(2)} · 入场 ${price(entry, precision.price)} · 风险 ${plan.riskPercent.toFixed(2)}% · 收益 ${plan.rewardPercent.toFixed(2)}%`));
  figures.push(label(Math.max(2, left), Math.max(18, Math.min(stopPoint.y - 6, bounding.height - 4)), `止损 ${price(stop, precision.price)}（未计费用/滑点）`, '#e79095'));
  figures.push(label(Math.max(2, left), Math.max(18, Math.min(targetPoint.y - 6, bounding.height - 4)), `目标 ${price(target, precision.price)}`, '#71cda8'));
  return figures;
}
function fibonacciExtension(params: OverlayCreateFiguresCallbackParams): OverlayFigure[] {
  if (!coordinatesReady(params, 2)) return [];
  const [a, b, c] = params.coordinates;
  const figures = [line(c ? [a, b, c] : [a, b])];
  if (!c) return figures;
  const [first, second, third] = params.overlay.points.map(point => point.value);
  if (first == null || second == null || third == null) return figures;
  const left = Math.min(a.x, b.x, c.x); const right = Math.max(c.x + 70, params.bounding.width);
  for (const ratio of [0, .618, 1, 1.272, 1.618, 2, 2.618]) {
    const value = third + (second - first) * ratio;
    const y = params.yAxis?.convertToPixel(value) ?? c.y + (b.y - a.y) * ratio;
    if (!Number.isFinite(y) || !Number.isFinite(value)) continue;
    figures.push(line([{ x: left, y }, { x: right, y }]));
    figures.push(label(Math.max(2, c.x), y - 3, `${(ratio * 100).toFixed(1)}%  ${price(value, params.precision.price)}`));
  }
  return figures;
}
function pitchfork(params: OverlayCreateFiguresCallbackParams): OverlayFigure[] {
  if (!coordinatesReady(params, 2)) return [];
  const [a, b, c] = params.coordinates;
  if (!c) return [line([a, b])];
  const midpoint = { x: (b.x + c.x) / 2, y: (b.y + c.y) / 2 };
  const dx = midpoint.x - a.x; const dy = midpoint.y - a.y;
  const norm = Math.hypot(dx, dy);
  if (norm < 1e-8) return [line([b, c]), label(Math.max(2, a.x), Math.max(18, a.y), '起点与中点重合，请移动锚点', '#e57679')];
  const rayLength = Math.hypot(params.bounding.width, params.bounding.height) * 2;
  const vector = { x: dx / norm * rayLength, y: dy / norm * rayLength };
  return [line([b, c]), ...[a, b, c].map(start => line([start, { x: start.x + vector.x, y: start.y + vector.y }]))];
}
function ellipse(params: OverlayCreateFiguresCallbackParams): OverlayFigure[] {
  if (!coordinatesReady(params, 2)) return [];
  const [a, b] = params.coordinates;
  const cx = (a.x + b.x) / 2; const cy = (a.y + b.y) / 2;
  const rx = Math.abs(b.x - a.x) / 2; const ry = Math.abs(b.y - a.y) / 2;
  const coordinates = Array.from({ length: 64 }, (_, i) => ({ x: cx + rx * Math.cos(i * Math.PI / 32), y: cy + ry * Math.sin(i * Math.PI / 32) }));
  return [{ type: 'polygon', attrs: { coordinates }, styles: { style: 'stroke_fill', color: '#d8a64616', borderColor: '#d8a646', borderSize: 1 } }];
}

export const REVIEW_OVERLAY_TEMPLATES: OverlayTemplate[] = [
  { name: 'Q_priceRange', totalStep: 3, createPointFigures: (params: OverlayCreateFiguresCallbackParams) => measurement(params, 'price') },
  { name: 'Q_dateRange', totalStep: 3, createPointFigures: (params: OverlayCreateFiguresCallbackParams) => measurement(params, 'date') },
  { name: 'Q_range', totalStep: 3, createPointFigures: (params: OverlayCreateFiguresCallbackParams) => measurement(params, 'both') },
  { name: 'Q_longPosition', totalStep: 4, createPointFigures: (params: OverlayCreateFiguresCallbackParams) => position(params, 'long') },
  { name: 'Q_shortPosition', totalStep: 4, createPointFigures: (params: OverlayCreateFiguresCallbackParams) => position(params, 'short') },
  { name: 'Q_fibExtension', totalStep: 4, createPointFigures: fibonacciExtension },
  { name: 'Q_pitchfork', totalStep: 4, createPointFigures: pitchfork },
  { name: 'Q_ellipse', totalStep: 3, createPointFigures: ellipse },
].map(template => ({ needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true, ...template }));
let registered = false;
export function registerReviewOverlays(): void {
  if (registered) return;
  REVIEW_OVERLAY_TEMPLATES.forEach(template => registerOverlay(template));
  registered = true;
}
