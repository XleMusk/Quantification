import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const transpile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
const dataModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const mathSource = transpile(await readFile(new URL('../src/charting/indicatorMath.ts', import.meta.url), 'utf8'));
const mathUrl = dataModule(mathSource);
const technicalSource = transpile(await readFile(new URL('../src/charting/customTechnical.ts', import.meta.url), 'utf8')).replace(/from ['"]\.\/indicatorMath['"]/g, `from '${mathUrl}'`);
const math = await import(mathUrl);
const { computeTechnical, TECHNICAL_IDS } = await import(dataModule(technicalSource));
const near = (actual, expected, tolerance = 1e-9) => assert.ok(typeof actual === 'number' && Math.abs(actual - expected) < tolerance, `${actual} != ${expected}`);
const bars = [
  { open: 10, high: 11, low: 9, close: 10, volume: 100 },
  { open: 12, high: 14, low: 11, close: 13, volume: 200 },
  { open: 13, high: 16, low: 12, close: 15, volume: 300 },
  { open: 14, high: 15, low: 12, close: 13, volume: 400 },
  { open: 14, high: 17, low: 13, close: 16, volume: 500 },
].map((bar, i) => ({ timestamp: i * 86400000, ...bar }));

test('SMA, EMA and Wilder have full-window seeds and reset on missing observations', () => {
  assert.deepEqual(math.sma([1, 2, 3, 4, 5], 3), [undefined, undefined, 2, 3, 4]);
  assert.deepEqual(math.ema([1, 2, 3, 4, 5], 3), [undefined, undefined, 2, 3, 4]);
  const wilder = math.wilder([1, 2, 3, 4, 5], 3);
  near(wilder[3], 8 / 3); near(wilder[4], 31 / 9);
  assert.deepEqual(math.ema([1, 2, 3, undefined, 4, 5, 6], 3), [undefined, undefined, 2, undefined, undefined, undefined, 5]);
  assert.deepEqual(math.sma([1, undefined, 3, 4], 2), [undefined, undefined, undefined, 3.5]);
});

test('rolling moments use population deviation and extrema require complete windows', () => {
  for (const value of math.rollingStd([1, 2, 3, 4], 3).slice(2)) near(value, Math.sqrt(2 / 3));
  near(math.rollingStd([1e9 - 1, 1e9, 1e9 + 1], 3)[2], Math.sqrt(2 / 3));
  assert.deepEqual(math.rollingMin([3, 1, undefined, 4, 2], 2), [undefined, 1, undefined, undefined, 2]);
  assert.deepEqual(math.rollingMax([3, 1, undefined, 4, 2], 2), [undefined, 3, undefined, undefined, 4]);
});

test('ATR includes gap true range and applies Wilder recurrence; NATR is percent', () => {
  assert.deepEqual(math.trueRange(bars), [2, 4, 4, 3, 4]);
  const result = computeTechnical('Q_ATR', bars, [3]);
  assert.equal(result[1].value, undefined);
  near(result[2].value, 10 / 3); near(result[3].value, 29 / 9); near(result[4].value, 94 / 27);
  near(computeTechnical('Q_NATR', bars, [3])[4].value, 100 * (94 / 27) / 16);
});

test('Supertrend trails bands and changes direction only after a close crosses its band', () => {
  const result = computeTechnical('Q_SUPERTREND', bars, [2, 1]);
  assert.deepEqual(result[0], {});
  near(result[1].trend, 15.5); assert.equal(result[1].direction, -1);
  near(result[2].trend, 15.5); near(result[3].trend, 15.5);
  assert.equal(result[4].direction, 1); near(result[4].trend, 11.375);
});

test('Donchian and Keltner honor inclusive windows and independently seeded ATR/EMA', () => {
  const channel = computeTechnical('Q_DONCHIAN', bars, [3]);
  assert.deepEqual(channel[2], { upper: 16, lower: 9, middle: 12.5 });
  assert.deepEqual(channel[4], { upper: 17, lower: 12, middle: 14.5 });
  const keltner = computeTechnical('Q_KELTNER', bars, [3, 3, 2]);
  near(keltner[2].middle, 38 / 3); near(keltner[2].upper, 58 / 3); near(keltner[2].lower, 6);
});

test('StochRSI uses Wilder RSI, then stochastic range and K/D smoothing', () => {
  const relative = math.rsi(bars.map(bar => bar.close), 2);
  near(relative[2], 100); near(relative[3], 500 / 9); near(relative[4], 1700 / 21);
  const result = computeTechnical('Q_STOCHRSI', bars, [2, 2, 1, 1]);
  assert.equal(result[2].k, undefined); near(result[3].k, 0); near(result[4].k, 100); near(result[4].d, 100);
});

test('CMF and MFI match hand-calculated signed money-flow windows', () => {
  near(computeTechnical('Q_CMF', bars, [3])[2].value, 13 / 36);
  near(computeTechnical('Q_CMF', bars, [3])[4].value, 2 / 9);
  const mfi = computeTechnical('Q_MFI', bars, [2]);
  near(mfi[2].value, 100); near(mfi[3].value, 100 * 129 / 289); near(mfi[4].value, 100 * 23 / 39);
});

test('Chaikin oscillator subtracts SMA-seeded EMAs of cumulative ADL', () => {
  const result = computeTechnical('Q_CHAIKIN', bars, [1, 2]);
  assert.equal(result[0].value, undefined);
  near(result[1].value, 100 / 3);
  near(result[2].value, 550 / 9);
  near(result[3].value, -650 / 27);
});

test('Force Index uses price change times volume, RVOL baseline excludes current volume', () => {
  assert.deepEqual(computeTechnical('Q_FORCE', bars, [1]).map(row => row.value), [undefined, 600, 600, -800, 1500]);
  const relative = computeTechnical('Q_RVOL', bars, [2]);
  assert.equal(relative[1].value, undefined); near(relative[2].value, 2); near(relative[3].value, 1.6); near(relative[4].value, 10 / 7);
});

test('Bollinger position, bandwidth and Kaufman efficiency match explicit formulas', () => {
  near(computeTechnical('Q_BBP', bars, [3, 2])[4].value, .5 + 1 / Math.sqrt(14));
  near(computeTechnical('Q_BBW', bars, [3, 2])[4].value, 100 * Math.sqrt(14) / 11);
  near(computeTechnical('Q_ER', bars, [3])[4].value, 3 / 7);
});

const parameterSets = {
  Q_ATR: [3], Q_NATR: [3], Q_SUPERTREND: [3, 2], Q_DONCHIAN: [3], Q_KELTNER: [3, 3, 2],
  Q_STOCHRSI: [3, 3, 2, 2], Q_CMF: [3], Q_MFI: [3], Q_CHAIKIN: [2, 3], Q_FORCE: [3],
  Q_RVOL: [3], Q_BBP: [3, 2], Q_BBW: [3, 2], Q_ER: [3],
};
const longBars = Array.from({ length: 95 }, (_, i) => {
  const close = 40 + Math.sin(i * .37) * 3 + i * .04;
  return { timestamp: i * 86400000, open: close - .2, high: close + 1 + i % 3, low: close - 1, close, volume: 100 + (i * 37) % 300 };
});

test('all 14 calculations are prefix-invariant and do not mutate source bars', () => {
  const before = JSON.stringify(longBars);
  for (const id of TECHNICAL_IDS) {
    const full = computeTechnical(id, longBars, parameterSets[id]);
    for (const count of [1, 2, 8, 31, 73]) assert.deepEqual(computeTechnical(id, longBars.slice(0, count), parameterSets[id]), full.slice(0, count), id);
    assert.ok(full.some(row => Object.values(row).some(Number.isFinite)), `${id} never warms up`);
  }
  assert.equal(JSON.stringify(longBars), before);
});

test('flat and zero-volume bars follow explicit conventions without NaN or Infinity', () => {
  const flat = Array.from({ length: 50 }, (_, i) => ({ timestamp: i, open: 10, high: 10, low: 10, close: 10, volume: 0 }));
  for (const id of TECHNICAL_IDS) for (const row of computeTechnical(id, flat, parameterSets[id])) for (const value of Object.values(row)) assert.ok(value === undefined || Number.isFinite(value), `${id}: ${value}`);
  assert.equal(computeTechnical('Q_ATR', flat, [3]).at(-1).value, 0);
  assert.equal(computeTechnical('Q_ER', flat, [3]).at(-1).value, 0);
  assert.equal(computeTechnical('Q_BBW', flat, [3, 2]).at(-1).value, 0);
  for (const id of ['Q_CMF', 'Q_MFI', 'Q_RVOL', 'Q_BBP']) assert.equal(computeTechnical(id, flat, [3, 2]).at(-1).value, undefined, id);
  assert.equal(computeTechnical('Q_STOCHRSI', flat, [3, 3, 1, 1]).at(-1).k, undefined);
});

test('invalid input and oversized windows never return non-finite chart values', () => {
  const broken = [...longBars.slice(0, 25), { ...longBars[25], close: NaN, volume: -1, high: Infinity }, ...longBars.slice(26)];
  for (const id of TECHNICAL_IDS) {
    for (const row of computeTechnical(id, broken, parameterSets[id])) for (const value of Object.values(row)) assert.ok(value === undefined || Number.isFinite(value), `${id}: ${value}`);
    assert.ok(computeTechnical(id, bars, [5000, 5000, 5000, 5000]).every(row => Object.values(row).every(value => value === undefined)));
    assert.deepEqual(computeTechnical(id, [], parameterSets[id]), []);
  }
});
