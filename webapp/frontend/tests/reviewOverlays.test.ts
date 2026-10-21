import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { OverlayCreateFiguresCallbackParams, OverlayFigure } from 'klinecharts';
import { computeTradePlan, REVIEW_EXTRA_TOOLS, REVIEW_OVERLAY_TEMPLATES } from '../src/charting/reviewOverlays.ts';

function figures(name: string, points: { value: number; dataIndex?: number; timestamp?: number }[], coordinates = [{ x: 50, y: 100 }, { x: 120, y: 130 }, { x: 180, y: 50 }]): OverlayFigure[] {
  const template = REVIEW_OVERLAY_TEMPLATES.find(item => item.name === name)!;
  const params = { overlay: { points }, coordinates: coordinates.slice(0, points.length), bounding: { width: 600, height: 400 }, precision: { price: 3 }, xAxis: { convertFromPixel: (px: number) => Math.round(px / 10) }, yAxis: { convertToPixel: (value: number) => 500 - value * 10 } } as unknown as OverlayCreateFiguresCallbackParams;
  const result = template.createPointFigures!(params);
  return Array.isArray(result) ? result : [result];
}
const labels = (items: OverlayFigure[]) => items.filter(item => item.type === 'text').map(item => item.attrs.text as string).join(' ');

test('long and short share the same risk/reward math, preserving price units', () => {
  const long = computeTradePlan({ side: 'long', entry: 10, stop: 9.5, target: 11.5, equity: 100000, riskPct: 1, lotSize: 100 });
  assert.equal(long.rr, 3); assert.equal(long.unitRisk, .5); assert.equal(long.unitReward, 1.5);
  assert.equal(long.riskPercent, 5); assert.equal(long.rewardPercent, 15);
  assert.equal(long.lossBudget, 1000); assert.equal(long.shares, 2000); assert.equal(long.estimatedLoss, 1000); assert.equal(long.notional, 20000);
  const short = computeTradePlan({ side: 'short', entry: 10, stop: 10.5, target: 8.5, equity: 100000, riskPct: 1 });
  assert.equal(short.rr, 3); assert.equal(short.shares, 2000);
  const geometryOnly = computeTradePlan({ side: 'long', entry: 1.001, stop: 1, target: 1.004 });
  assert.ok(Math.abs(geometryOnly.rr - 3) < 1e-9); assert.equal(geometryOnly.shares, null);
});

test('sizing rounds down to full lots and can correctly yield zero shares', () => {
  const input = { side: 'long' as const, entry: 20, stop: 17, target: 26, equity: 100000, riskPct: 1 };
  const plan = computeTradePlan(input);
  assert.equal(plan.shares, 300); assert.equal(plan.estimatedLoss, 900);
  assert.equal(computeTradePlan({ ...input, equity: 1000, riskPct: .1 }).shares, 0);
  assert.equal(computeTradePlan({ ...input, lotSize: 1 }).shares, 333);
  assert.ok(plan.estimatedLoss! <= plan.lossBudget!);
});

test('invalid sides, orientations, budgets and lot sizes are rejected', () => {
  const input = { side: 'long' as const, entry: 10, stop: 9, target: 12 };
  for (const patch of [{ entry: 0 }, { stop: NaN }, { target: Infinity }, { stop: 10 }, { stop: 11 }, { target: 9 }, { equity: 10000 }, { riskPct: 1 }, { equity: 10000, riskPct: 0 }, { equity: 10000, riskPct: 101 }, { equity: -1, riskPct: 1 }, { lotSize: 0 }, { lotSize: 1.5 }, { lotSize: Number.MAX_SAFE_INTEGER + 1 }]) assert.throws(() => computeTradePlan({ ...input, ...patch }));
  assert.throws(() => computeTradePlan({ ...input, side: 'short' }));
  assert.throws(() => computeTradePlan({ ...input, side: 'invalid' as 'long' }));
});

test('measures observed bars and calendar days separately, never labels blank space as observations', () => {
  const first = Date.UTC(2025, 0, 3); const second = Date.UTC(2025, 0, 6);
  const observed = labels(figures('Q_range', [{ value: 10, dataIndex: 2, timestamp: first }, { value: 11, dataIndex: 3, timestamp: second }]));
  assert.match(observed, /\+1\.000/); assert.match(observed, /\+10\.00%/);
  assert.match(observed, /观测K线 2 根/); assert.match(observed, /相隔 3 日历日/);
  const blank = labels(figures('Q_dateRange', [{ value: 10, dataIndex: 2 }, { value: 11, dataIndex: 5 }]));
  assert.match(blank, /横向间距 4 根（含空白区）/); assert.doesNotMatch(blank, /观测K线|日历日/);
});

test('risk drawings show valid plans or explicit orientation errors, with mill precision', () => {
  const good = figures('Q_longPosition', [{ value: 10.001 }, { value: 9.501 }, { value: 11.501 }]);
  assert.equal(good.filter(item => item.type === 'rect').length, 2);
  assert.match(labels(good), /RR 3\.00/); assert.match(labels(good), /入场 10\.001/); assert.match(labels(good), /未计费用\/滑点/);
  const invalid = figures('Q_longPosition', [{ value: 10 }, { value: 11 }, { value: 12 }]);
  assert.equal(invalid.filter(item => item.type === 'rect').length, 0); assert.match(labels(invalid), /止损 < 入场 < 目标/);
  assert.match(labels(figures('Q_shortPosition', [{ value: 10 }, { value: 11 }, { value: 8 }])), /RR 2\.00/);
});

test('fib extension projects numeric price levels through the current axis', () => {
  const output = figures('Q_fibExtension', [{ value: 10 }, { value: 12 }, { value: 11 }]);
  assert.match(labels(output), /161\.8%  14\.236/);
  const lineLevels = output.filter(item => item.type === 'line').slice(1).map(item => item.attrs.coordinates[0].y);
  assert.ok(lineLevels.some(y => Math.abs(y - (500 - 14.236 * 10)) < 1e-8));
});

test('all drawings handle incomplete anchors and degenerate pitchfork geometry', () => {
  for (const [name] of REVIEW_EXTRA_TOOLS) assert.deepEqual(figures(name, []), []);
  const ellipse = figures('Q_ellipse', [{ value: 10 }, { value: 12 }]);
  assert.equal(ellipse[0].type, 'polygon'); assert.equal(ellipse[0].attrs.coordinates.length, 64);
  const fork = figures('Q_pitchfork', [{ value: 10 }, { value: 11 }, { value: 9 }], [{ x: 10, y: 10 }, { x: 0, y: 0 }, { x: 20, y: 20 }]);
  assert.match(labels(fork), /起点与中点重合/);
});
