import test from 'node:test';
import assert from 'node:assert/strict';
import { aggregateMinutes, linkedMinuteIndex, minuteSlot, minuteVwap, parseShanghaiMinute, type MinuteBar } from '../src/charting/intraday';
import { getIndicatorDefinition, getUnavailableReason } from '../src/charting/indicatorCatalog';
const day = '2026-10-19';
function bar(time: string, price = 10, volume = 100, extra: Partial<MinuteBar> = {}): MinuteBar {
  const date = `${day}T${time}:00+08:00`;
  const timestamp = Date.parse(date);
  const minute = Number(time.slice(0, 2)) * 60 + Number(time.slice(3));
  return { date, timestamp, symbol: '600000.SSE', open: price, high: price + 1, low: price - 1, close: price, volume, amount: price * volume, amount_usable: true, session_date: day, session: minute === 565 ? 'opening_auction' : minute < 720 ? 'morning' : 'afternoon', auction: minute === 565 || minute >= 898, gap_before: 0, expected_minutes: 1, observed_minutes: 1, is_partial: false, ...extra };
}
function time(value: number): string { return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`; }
const session = () => [...Array.from({ length: 120 }, (_, i) => bar(time(571 + i))), ...Array.from({ length: 120 }, (_, i) => bar(time(781 + i)))];

test('minute aggregation uses right-end session buckets and leaves lunch and auction separate', () => {
  const rows = [bar('09:25'), ...session()];
  const five = aggregateMinutes(rows, '5m');
  assert.equal(five.length, 49);
  assert.equal(five[0].session, 'opening_auction');
  assert.equal(five[1].date.slice(11, 16), '09:35');
  assert.equal(five[24].date.slice(11, 16), '11:30');
  assert.equal(five[25].date.slice(11, 16), '13:05');
  assert.equal(five[1].observed_minutes, 5); assert.equal(five[1].is_partial, false);
  assert.equal(five[1].amount, 5000); assert.equal(five[1].turnover, 5000);
  assert.equal(aggregateMinutes(rows, '60m').length, 5);
  assert.equal(minuteSlot(bar('13:01').timestamp), 120);
});

test('cutoff precedes aggregation and unfinished bars retain actual last observed timestamp', () => {
  const rows = [bar('09:31', 10), bar('09:32', 11), bar('09:33', 1000)];
  const prefix = aggregateMinutes(rows, '5m', rows[1].timestamp);
  assert.deepEqual(prefix, aggregateMinutes(rows.slice(0, 2), '5m'));
  assert.equal(prefix[0].timestamp, rows[1].timestamp);
  assert.equal(prefix[0].high, 12); assert.equal(prefix[0].volume, 200);
  assert.equal(prefix[0].observed_minutes, 2); assert.equal(prefix[0].expected_minutes, 5); assert.equal(prefix[0].is_partial, true);
  assert.equal(prefix[0].bucket_end, bar('09:35').timestamp);
});

test('session VWAP is actual cumulative amount/volume and not an OHLC price proxy', () => {
  const rows = [bar('09:31', 10, 100), bar('09:32', 12, 200)];
  const values = minuteVwap(rows, { mode: 'session' });
  assert.equal(values[0].value, 10);
  assert.equal(values[1].value, 3400 / 300);
  assert.equal(values[1].complete, true);
});

test('session gaps invalidate remaining day, later complete day resets, and anchor after a gap can start fresh', () => {
  const rows = [bar('09:31'), bar('09:33', 12, 100, { gap_before: 1 }), bar('09:34', 12)];
  const values = minuteVwap(rows, { mode: 'session' });
  assert.equal(values[0].value, 10); assert.equal(values[1].value, undefined); assert.equal(values[2].value, undefined);
  const anchored = minuteVwap(rows, { mode: 'anchored', anchor: rows[1].timestamp });
  assert.equal(anchored[0].value, undefined); assert.equal(anchored[1].value, 12); assert.equal(anchored[2].value, 12);
  const nextDay = bar('09:31', 11, 100, { date: '2026-10-20T09:31:00+08:00', timestamp: Date.parse('2026-10-20T09:31:00+08:00'), session_date: '2026-10-20' });
  assert.equal(minuteVwap([...rows, nextDay], { mode: 'session' }).at(-1)?.value, 11);
  assert.equal(minuteVwap([bar('10:00')], { mode: 'session' })[0].value, undefined);
});

test('lunch and overnight preserve complete anchored VWAP, fully missing requested date stays unknown', () => {
  const rows = session();
  const nextDay = bar('09:31', 12, 100, { date: '2026-10-21T09:31:00+08:00', timestamp: Date.parse('2026-10-21T09:31:00+08:00'), session_date: '2026-10-21' });
  const full = minuteVwap(rows, { mode: 'session' });
  assert.equal(full[120].complete, true); assert.equal(full.at(-1)?.value, 10);
  const observed = minuteVwap([...rows, nextDay], { mode: 'anchored', anchor: rows[0].timestamp, requestedDays: [day, '2026-10-21'] });
  assert.equal(observed.at(-1)?.value, (240000 + 1200) / 24100);
  const unknown = minuteVwap([...rows, nextDay], { mode: 'anchored', anchor: rows[0].timestamp, requestedDays: [day, '2026-10-20', '2026-10-21'] });
  assert.equal(unknown.at(-1)?.value, undefined); assert.match(unknown.at(-1)?.reason ?? '', /交易日性质未核实/);
});

test('zero-volume and cash-unit problems never fabricate a VWAP', () => {
  const rows = [bar('09:31', 10, 0), bar('09:32', 12, 100)];
  const values = minuteVwap(rows, { mode: 'session' });
  assert.equal(values[0].value, undefined); assert.equal(values[0].complete, true); assert.equal(values[1].value, 12);
  for (const patch of [{ amount: null }, { amount: .16 }, { amount: Infinity }, { amount_usable: false }]) {
    const input = [bar('09:31'), bar('09:32', 12, 100, patch), bar('09:33', 12)];
    assert.equal(minuteVwap(input, { mode: 'session' }).at(-1)?.value, undefined);
    assert.equal(aggregateMinutes(input, '5m')[0].amount, null);
  }
});

test('VWAP values are prefix causal and future anchors do not appear before their source candle', () => {
  const rows = session(); rows[40].amount = null;
  const full = minuteVwap(rows, { mode: 'session' });
  assert.deepEqual(minuteVwap(rows, { mode: 'session', asOf: rows[30].timestamp }), full.slice(0, 31));
  assert.ok(minuteVwap(rows, { mode: 'anchored', anchor: rows[50].timestamp, asOf: rows[30].timestamp }).every(row => row.value === undefined));
  const original = structuredClone(rows); aggregateMinutes(rows, '15m'); minuteVwap(rows, { mode: 'session' }); assert.deepEqual(rows, original);
});

test('linked crosshair maps to the visible same minute bucket without inventing later bars', () => {
  const rows = session();
  const five = aggregateMinutes(rows, '5m', rows[7].timestamp);
  assert.equal(linkedMinuteIndex(rows[6].timestamp, five, '5m'), 1);
  assert.equal(five[1].date.slice(11, 16), '09:38');
  assert.equal(linkedMinuteIndex(bar('09:25').timestamp, five, '5m'), -1);
  assert.throws(() => aggregateMinutes([rows[0], rows[0]], '5m'));
  assert.throws(() => aggregateMinutes([bar('12:00')], '1m'));
});


test('intraday toolbox does not inherit daily annualization assumptions', () => {
  for (const period of ['1m', '5m', '15m', '30m', '60m']) for (const id of ['Q_HVOL', 'Q_PARKINSON', 'Q_GK', 'Q_SHARPE', 'Q_SORTINO']) {
    assert.match(getUnavailableReason(getIndicatorDefinition(id)!, { period, hasTurnover: true, hasBenchmark: false, barCount: 240 }) ?? '', /分钟线年化/);
  }
  assert.equal(getUnavailableReason(getIndicatorDefinition('Q_HVOL')!, { period: 'D', hasTurnover: true, hasBenchmark: false, barCount: 240 }), null);
});


test('Shanghai minute input accepts native datetime-local precision without browser timezone or date rollover', () => {
  const expected = Date.parse('2025-09-15T09:43:00+08:00');
  for (const value of ['2025-09-15T09:43', '2025-09-15T09:43:00', '2025-09-15T09:43:00.000']) assert.equal(parseShanghaiMinute(value), expected);
  assert.equal(parseShanghaiMinute('2024-02-29T15:00'), Date.parse('2024-02-29T15:00:00+08:00'));
  for (const value of ['', '2025-09-15T09:43:01', '2025-09-15T09:43:00.001', '2025-02-29T09:43', '2025-09-15T24:00', '2025-09-15T09:60', '2025-09-15T09:43Z', '2025-09-15T09:43+08:00']) assert.equal(parseShanghaiMinute(value), undefined, value);
});
