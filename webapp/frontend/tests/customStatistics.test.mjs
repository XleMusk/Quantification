import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const source = await readFile(new URL('../src/charting/customStatistics.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { computeStatistics, STATISTICAL_IDS } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const bars = closes => closes.map((close, index) => ({ timestamp: Date.UTC(2025, 0, index + 1), open: close, high: close, low: close, close, volume: 1000 }));
const prices = returns => returns.reduce((values, value) => [...values, values.at(-1) * (1 + value)], [100]);
const near = (actual, expected, tolerance = 1e-10) => {
  assert.equal(typeof actual, 'number');
  assert.ok(Math.abs(actual - expected) <= tolerance * Math.max(1, Math.abs(expected)), `${actual} != ${expected}`);
};
const last = (id, input, params, context) => computeStatistics(id, input, params, context).at(-1).value;

test('sample log-return volatility has n-return warmup and explicit annualization', () => {
  const input = bars([100, 100 * Math.exp(.1), 100 * Math.exp(-.1), 100 * Math.exp(.2)]);
  const result = computeStatistics('Q_HVOL', input, [3, 1]);
  assert.deepEqual(result.slice(0, 3), [{}, {}, {}]);
  const mean = (.1 - .2 + .3) / 3;
  const variance = ((.1 - mean) ** 2 + (-.2 - mean) ** 2 + (.3 - mean) ** 2) / 2;
  near(result[3].value, Math.sqrt(variance) * 100);
  near(last('Q_HVOL', input, [3, 252]), Math.sqrt(variance * 252) * 100);
});

test('Parkinson and Garman–Klass equal hand-calculated range estimators', () => {
  const input = bars([11, 11]).map(bar => ({ ...bar, open: 10, high: 12, low: 8 }));
  const hl = Math.log(12 / 8);
  near(last('Q_PARKINSON', input, [2, 252]), Math.sqrt(hl ** 2 / (4 * Math.LN2) * 252) * 100);
  const estimate = .5 * hl ** 2 - (2 * Math.LN2 - 1) * Math.log(11 / 10) ** 2;
  near(last('Q_GK', input, [2, 252]), Math.sqrt(estimate * 252) * 100);
  const invalid = input.map((bar, index) => index ? { ...bar, high: 9 } : bar);
  assert.equal(last('Q_GK', invalid, [2, 252]), undefined);
});

test('price z-score and normalized OLS slope/R² use trailing data', () => {
  const input = bars([1, 2, 3, 4]);
  near(last('Q_ZSCORE', input, [4]), 1.5 / Math.sqrt(1.25));
  near(last('Q_SLOPE', input, [4]), 40);
  near(last('Q_R2', input, [4]), 1);
  near(last('Q_SLOPE', bars([4, 3, 2, 1]), [4]), -40);
});

test('drawdown measures current close against trailing peak, not maximum drawdown', () => {
  const result = computeStatistics('Q_DRAWDOWN', bars([10, 12, 9, 11, 13]), [3]);
  assert.deepEqual(result.slice(0, 2), [{}, {}]);
  near(result[2].value, -25);
  near(result[3].value, (11 / 12 - 1) * 100);
  near(result[4].value, 0);
});

test('sample skewness and log-return z-score have explicit conventions', () => {
  const input = bars([100, 100, 100 * Math.exp(.1), 100]);
  near(last('Q_SKEW', input, [3]), 0);
  const asymmetric = bars([100, 100 * Math.exp(.1), 100 * Math.exp(.3), 100 * Math.exp(.7)]);
  near(last('Q_SKEW', asymmetric, [3]), .9352195295828245);
  near(last('Q_RETURNZ', asymmetric, [3]), (.4 - 7 / 30) / Math.sqrt(((.1 - 7 / 30) ** 2 + (.2 - 7 / 30) ** 2 + (.4 - 7 / 30) ** 2) / 3));
});

test('rolling Sharpe and zero-target Sortino equal simple return calculations', () => {
  const r = [.1, -.05, .2];
  const input = bars(prices(r));
  const mean = 1 / 12;
  const sd = Math.sqrt(r.reduce((sum, value) => sum + (value - mean) ** 2, 0) / 2);
  near(last('Q_SHARPE', input, [3, 252]), mean / sd * Math.sqrt(252));
  near(last('Q_SORTINO', input, [3, 252]), mean / Math.sqrt(.05 ** 2 / 3) * Math.sqrt(252));
  assert.equal(last('Q_SORTINO', bars(prices([.01, .02, .03])), [3, 252]), undefined);
});

test('flat and invalid observations never produce false zero ratios or nonfinite output', () => {
  const input = bars(Array(80).fill(100));
  for (const id of STATISTICAL_IDS) {
    const output = computeStatistics(id, input, [8, 252], { benchmark: input });
    assert.equal(output.length, input.length);
    for (const row of output) for (const value of Object.values(row)) assert.ok(Number.isFinite(value));
  }
  for (const id of ['Q_ZSCORE', 'Q_RETURNZ', 'Q_SKEW', 'Q_R2', 'Q_SHARPE', 'Q_SORTINO', 'Q_BETA', 'Q_CORR', 'Q_RESIDUALZ']) {
    assert.equal(last(id, input, [8, 252], { benchmark: input }), undefined, id);
  }
  near(last('Q_HVOL', input, [8, 252]), 0);
  near(last('Q_SLOPE', input, [8]), 0);
  near(last('Q_RS', input, [8], { benchmark: input }), 0);
  const broken = input.map((bar, index) => index === 79 ? { ...bar, close: 0 } : bar);
  assert.equal(last('Q_HVOL', broken, [8, 252]), undefined);
  for (const params of [[0], [-2], [2.5], [NaN], [3, 0]]) assert.equal(last('Q_HVOL', input, params), undefined);
});

test('benchmark regression resolves beta/correlation and rejects perfect-fit residual z', () => {
  const referenceReturns = [-.02, -.01, .01, .02, -.015, .005];
  const reference = bars(prices(referenceReturns));
  const asset = bars(prices(referenceReturns.map(value => 2 * value + .003)));
  near(last('Q_BETA', asset, [6], { benchmark: reference }), 2);
  near(last('Q_CORR', asset, [6], { benchmark: reference }), 1);
  assert.equal(last('Q_RESIDUALZ', asset, [6], { benchmark: reference }), undefined);
});

test('residual z-score and relative performance match independent constructions', () => {
  const referenceReturns = [-.02, -.01, 0, .01, .02];
  const errors = [.01, -.02, 0, .02, -.01]; // Sum and covariance with benchmark are zero.
  const reference = bars(prices(referenceReturns));
  const asset = bars(prices(referenceReturns.map((value, index) => 1.5 * value + .002 + errors[index])));
  near(last('Q_BETA', asset, [5], { benchmark: reference }), 1.5);
  near(last('Q_RESIDUALZ', asset, [5], { benchmark: reference }), -1 / Math.sqrt(2));
  near(last('Q_RS', asset, [5], { benchmark: reference }), ((asset.at(-1).close / asset[0].close) / (reference.at(-1).close / reference[0].close) - 1) * 100);
});

test('benchmark gaps reset aligned return windows instead of compressing observations', () => {
  const reference = bars(prices([.01, -.02, .03, -.01, .02, -.015, .025]));
  const asset = bars(prices([.015, -.03, .045, -.015, .03, -.0225, .0375]));
  const missing = reference.filter((_, index) => index !== 3);
  for (const id of ['Q_BETA', 'Q_CORR', 'Q_RS']) {
    const output = computeStatistics(id, asset, [3], { benchmark: missing });
    assert.equal(output[3].value, undefined);
    assert.equal(output[4].value, undefined);
    assert.equal(output[6].value, undefined);
    assert.ok(Number.isFinite(output[7].value), id);
    assert.ok(computeStatistics(id, asset, [3]).every(row => row.value === undefined));
  }
  // An extra benchmark session inside an asset return interval also breaks alignment.
  const assetGap = asset.filter((_, index) => index !== 3);
  assert.equal(computeStatistics('Q_BETA', assetGap, [2], { benchmark: reference })[3].value, undefined);
  const duplicated = [...reference.slice(0, 3), reference[2], ...reference.slice(3)];
  assert.ok(computeStatistics('Q_CORR', asset, [2], { benchmark: duplicated }).every(row => row.value === undefined));
});

test('every estimator is prefix invariant even if the supplied benchmark includes future bars', () => {
  const asset = bars(Array.from({ length: 75 }, (_, i) => 100 * Math.exp(.003 * i + .025 * Math.sin(i * .7))));
  const reference = bars(Array.from({ length: 75 }, (_, i) => 80 * Math.exp(.002 * i + .018 * Math.cos(i * .6))));
  const rangedAsset = asset.map(bar => ({ ...bar, high: bar.close * 1.02, low: bar.close * .98 }));
  for (const id of STATISTICAL_IDS) {
    const expected = computeStatistics(id, rangedAsset, [8, 252], { benchmark: reference });
    for (let length = 12; length <= rangedAsset.length; length += 7) {
      const actual = computeStatistics(id, rangedAsset.slice(0, length), [8, 252], { benchmark: reference });
      assert.deepEqual(actual, expected.slice(0, length), `${id} prefix ${length}`);
    }
  }
});

test('extreme finite prices and malformed ordering cannot leak NaN or Infinity', () => {
  const input = bars([1e-300, 1e300, 1e-200, 1e200, 1e-100, 1e100]);
  for (const id of STATISTICAL_IDS) {
    for (const row of computeStatistics(id, input, [3, 252], { benchmark: input })) {
      for (const value of Object.values(row)) assert.ok(Number.isFinite(value), id);
    }
  }
  const outOfOrder = bars([1, 2, 3]).reverse();
  assert.deepEqual(computeStatistics('Q_ZSCORE', outOfOrder, [2]), [{}, {}, {}]);
  assert.deepEqual(computeStatistics('missing', bars([1, 2, 3]), [2]), [{}, {}, {}]);
});
