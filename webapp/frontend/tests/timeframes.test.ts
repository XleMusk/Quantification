import test from 'node:test';
import assert from 'node:assert/strict';
import type { Bar } from '../src/types';
import { aggregateBars, alignBenchmark, findLinkedBarIndex, observationDate, observationTimestamp, periodState } from '../src/charting/timeframes';

const bar = (date: string, close: number, volume = 10, extra: Record<string, unknown> = {}): Bar => ({
  date, symbol: 'TEST.SSE', open: close - 1, high: close + 2, low: close - 2,
  close, volume, ...extra,
});

test('ISO Monday weeks aggregate across year boundaries and never fabricate sessions', () => {
  const rows = [bar('2020-12-31', 10, 10), bar('2021-01-01', 12, 20),
    bar('2021-01-03', 11, 0), bar('2021-01-04', 20, 30), bar('2021-01-18', 30, 40)];
  assert.deepEqual(aggregateBars(rows, 'W'), [
    { date: '2021-01-03', symbol: 'TEST.SSE', open: 9, high: 14, low: 8, close: 11, volume: 30 },
    { ...rows[3] }, { ...rows[4] },
  ]);
  assert.equal(aggregateBars(rows, 'W').length, 3, 'the missing week is not filled');
});

test('month candles use observed month end, including leap year and holiday gaps', () => {
  const rows = [bar('2024-02-01', 10), bar('2024-02-08', 12), bar('2024-02-29', 15),
    bar('2024-03-04', 20)];
  const result = aggregateBars(rows, 'M');
  assert.deepEqual(result.map(row => row.date), ['2024-02-29', '2024-03-04']);
  assert.deepEqual(result[0], { date: '2024-02-29', symbol: 'TEST.SSE', open: 9,
    high: 17, low: 8, close: 15, volume: 30 });
});

test('asOf is applied before OHLC, volume and date aggregation', () => {
  const rows = [bar('2025-01-06', 10, 100), bar('2025-01-07', 11, 200),
    bar('2025-01-08', 1000, 1_000_000), bar('2025-01-09', 2000, 2_000_000)];
  const expected = [{ date: '2025-01-07', symbol: 'TEST.SSE', open: 9,
    high: 13, low: 8, close: 11, volume: 300 }];
  for (const period of ['W', 'M'] as const) {
    assert.deepEqual(aggregateBars(rows, period, '2025-01-07'), expected);
    assert.deepEqual(aggregateBars(rows, period, '2025-01-07'), aggregateBars(rows.slice(0, 2), period));
  }
});

test('sorting never mutates source bars and midnight serialization is retained', () => {
  const rows = [bar('2025-01-07T00:00:00.000', 12), bar('2025-01-06T00:00:00.000', 10)];
  const copy = structuredClone(rows);
  const result = aggregateBars(rows, 'D');
  assert.deepEqual(rows, copy);
  assert.equal(result[0].date, '2025-01-06T00:00:00.000');
  result[0].close = 99;
  assert.equal(rows[1].close, 10);
  assert.equal(observationTimestamp('2025-01-06T00:00:00.000'), Date.parse('2025-01-06T00:00:00+08:00'));
});

test('turnover is summed only from complete explicit finite observations', () => {
  const complete = [bar('2025-01-06', 10, 10, { turnover: 100 }), bar('2025-01-07', 12, 20, { turnover: 240 })];
  assert.equal((aggregateBars(complete, 'W')[0] as Bar & { turnover?: number }).turnover, 340);
  const absent = [complete[0], bar('2025-01-07', 12, 20)];
  assert.equal('turnover' in aggregateBars(absent, 'W')[0], false);
  for (const invalid of [NaN, Infinity, -1, undefined]) {
    const mixed = [complete[0], bar('2025-01-07', 12, 20, { turnover: invalid })];
    assert.equal('turnover' in aggregateBars(mixed, 'W')[0], false);
  }
  assert.equal((aggregateBars([bar('2025-01-06', 10, 0, { turnover: 0 })], 'W')[0] as Bar & { turnover?: number }).turnover, 0);
});

test('invalid and duplicate dates, mixed symbols and malformed bars are rejected', () => {
  assert.throws(() => observationDate('2025-02-29'));
  assert.throws(() => observationDate('2025-01-06T12:00:00'));
  assert.throws(() => aggregateBars([bar('2025-01-06', 10), bar('2025-01-06T00:00:00.000', 11)], 'W'));
  assert.throws(() => aggregateBars([bar('2025-01-06', 10), bar('2025-01-07', 11, 10, { symbol: 'OTHER.SSE' })], 'M'));
  assert.throws(() => aggregateBars([bar('2025-01-06', 10, -1)], 'D'));
  assert.throws(() => aggregateBars([bar('2025-01-06', 10, 10, { high: 1 })], 'D'));
});

test('benchmark alignment preserves missing dates and excludes future benchmark data', () => {
  const asset = [bar('2025-01-06', 10), bar('2025-01-07', 11), bar('2025-01-08', 12)];
  const benchmark = [bar('2025-01-06', 20), bar('2025-01-08', 22), bar('2025-01-09', 900)];
  const result = alignBenchmark(asset, benchmark, 'D');
  assert.equal(result.asset.length, 3);
  assert.deepEqual(result.benchmark.map(row => row.date), ['2025-01-06', '2025-01-08']);
  assert.deepEqual(result.missingDates, ['2025-01-07']);
  assert.equal(result.benchmark.some(row => row.close === 900), false);
  assert.equal(alignBenchmark([], benchmark, 'D').benchmark.length, 0);
});

test('a partial final week/month is disclosed rather than declared complete from weekdays', () => {
  for (const period of ['W', 'M'] as const) {
    const state = periodState([bar('2025-01-08', 10)], period);
    assert.equal(state.isPartial, true);
    assert.match(state.lastLabel, /末根可能未完成/);
  }
  assert.equal(periodState([bar('2025-01-08', 10)], 'D').isPartial, false);
  assert.deepEqual(periodState([], 'W'), { isPartial: false, lastLabel: '暂无可聚合行情' });
});

test('linked daily crosshair maps Wednesday to its visible current week, including ISO year boundary', () => {
  const weekly = [bar('2025-01-03', 10), bar('2025-01-10', 11), bar('2025-01-17', 12)];
  assert.equal(findLinkedBarIndex('2025-01-08', weekly, 'W'), 1);
  assert.equal(findLinkedBarIndex('2024-12-31', weekly, 'W'), 0);
  const partial = aggregateBars([bar('2025-01-06', 10), bar('2025-01-08', 11)], 'W');
  assert.equal(findLinkedBarIndex('2025-01-07', partial, 'W'), 0);
  assert.equal(partial[0].date, '2025-01-08', 'linking does not invent the coming Friday');
});

test('linked daily crosshair maps within the same visible month rather than the previous month end', () => {
  const monthly = [bar('2024-12-31', 10), bar('2025-01-31', 11), bar('2025-02-28', 12)];
  const original = structuredClone(monthly);
  assert.equal(findLinkedBarIndex('2025-01-15T00:00:00.000', monthly, 'M'), 1);
  assert.equal(findLinkedBarIndex('2025-02-03', monthly, 'M'), 2);
  assert.deepEqual(monthly, original);
});

test('linked crosshair preserves gaps and never selects an unavailable or later bucket', () => {
  const weekly = [bar('2025-01-03', 10), bar('2025-01-17', 12)];
  assert.equal(findLinkedBarIndex('2025-01-08', weekly, 'W'), 0);
  assert.equal(findLinkedBarIndex('2024-12-20', weekly, 'W'), -1);
  assert.equal(findLinkedBarIndex('2025-01-08', weekly.slice(1), 'W'), -1);
  assert.equal(findLinkedBarIndex('2025-01-08', [], 'M'), -1);
  const daily = [bar('2025-01-07', 10), bar('2025-01-09', 11)];
  assert.equal(findLinkedBarIndex('2025-01-08', daily, 'D'), 0);
});
