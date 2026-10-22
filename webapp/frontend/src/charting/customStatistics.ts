import type { IndicatorBar, IndicatorResult } from './indicatorMath';

/** Public descriptive estimators, evaluated on trailing observations only.
 *
 * These are price-series diagnostics, not performance of an executed strategy.
 * Annualization is an explicit caller parameter; it is not inferred from dates.
 * Historical volatility uses sample log-return standard deviation. Parkinson
 * (1980) and the usual OHLC Garman–Klass (1980) estimator omit overnight jumps.
 * Sharpe follows mean differential return / sample standard deviation, with the
 * reference return fixed to zero here (Sharpe, 1994); Sortino uses target zero
 * and all observations in the lower-partial-moment denominator.
 *
 * References: https://web.stanford.edu/~wfsharpe/art/sr/sr.htm
 * https://www.cmegroup.com/trading/fx/files/michael_parkinson.pdf
 * https://doi.org/10.1086/296072
 */
export const STATISTICAL_IDS = [
  'Q_HVOL', 'Q_PARKINSON', 'Q_GK', 'Q_SKEW', 'Q_ZSCORE', 'Q_RETURNZ',
  'Q_SLOPE', 'Q_R2', 'Q_DRAWDOWN', 'Q_SHARPE', 'Q_SORTINO',
  'Q_BETA', 'Q_CORR', 'Q_RESIDUALZ', 'Q_RS',
] as const;

type Value = number | undefined;
type Moments = { mean: number; ss: number; scale: number; normalized: number[] };
const defaults: Record<string, number[]> = {
  Q_HVOL: [20, 252], Q_PARKINSON: [20, 252], Q_GK: [20, 252],
  Q_SKEW: [60], Q_ZSCORE: [20], Q_RETURNZ: [20], Q_SLOPE: [20],
  Q_R2: [20], Q_DRAWDOWN: [60], Q_SHARPE: [60, 252],
  Q_SORTINO: [60, 252], Q_BETA: [60], Q_CORR: [60], Q_RESIDUALZ: [60], Q_RS: [20],
};
const finite = (value: number): Value => Number.isFinite(value) ? value : undefined;
const positive = (value: number): boolean => Number.isFinite(value) && value > 0;

function trailing(values: Value[], end: number, period: number): number[] | undefined {
  if (end < period - 1) return undefined;
  const window: number[] = [];
  for (let i = end - period + 1; i <= end; i++) {
    const value = values[i];
    if (value === undefined || !Number.isFinite(value)) return undefined;
    window.push(value);
  }
  return window;
}

// Work in scaled coordinates to avoid overflowing squared prices or returns.
function moments(values: number[]): Moments {
  let scale = 0;
  for (const value of values) scale = Math.max(scale, Math.abs(value));
  if (scale === 0) scale = 1;
  const normalized = values.map(value => value / scale);
  const mean = normalized.reduce((sum, value) => sum + value, 0) / values.length;
  const ss = normalized.reduce((sum, value) => sum + (value - mean) ** 2, 0);
  return { mean, ss, scale, normalized };
}

function zScore(values: number[]): Value {
  const state = moments(values);
  if (state.ss <= 0) return undefined;
  return finite((state.normalized.at(-1)! - state.mean) / Math.sqrt(state.ss / values.length));
}

function timeRegression(values: number[]): { slope: number; r2: Value } {
  const { normalized, mean, ss } = moments(values);
  const count = values.length;
  const center = (count - 1) / 2;
  const xss = count * (count ** 2 - 1) / 12;
  const cross = normalized.reduce((sum, value, index) => sum + (index - center) * (value - mean), 0);
  return {
    slope: mean > 0 ? cross / xss / mean * 100 : 0,
    r2: ss > 0 ? finite(Math.min(1, Math.max(0, cross ** 2 / (xss * ss)))) : undefined,
  };
}

function validOrder(bars: IndicatorBar[]): boolean {
  return bars.every((bar, i) => Number.isFinite(bar.timestamp)
    && (i === 0 || bar.timestamp > bars[i - 1].timestamp));
}

function logReturns(bars: IndicatorBar[]): Value[] {
  return bars.map((bar, index) => index > 0 && positive(bar.close) && positive(bars[index - 1].close)
    ? finite(Math.log(bar.close) - Math.log(bars[index - 1].close)) : undefined);
}

function simpleReturns(bars: IndicatorBar[]): Value[] {
  return bars.map((bar, index) => index > 0 && positive(bar.close) && positive(bars[index - 1].close)
    ? finite(bar.close / bars[index - 1].close - 1) : undefined);
}

function alignedBenchmark(bars: IndicatorBar[], benchmark: IndicatorBar[]): {
  prices: Value[]; returns: Value[];
} {
  const prices: Value[] = bars.map(() => undefined);
  const returns: Value[] = bars.map(() => undefined);
  if (!validOrder(benchmark)) return { prices, returns };
  const positions = new Map(benchmark.map((bar, index) => [bar.timestamp, index]));
  bars.forEach((bar, index) => {
    const position = positions.get(bar.timestamp);
    if (position === undefined || !positive(benchmark[position].close)) return;
    prices[index] = benchmark[position].close;
    // Both return intervals must start AND end at the exact same observations.
    // A benchmark-only intermediate session or a missing benchmark observation
    // breaks the contiguous aligned window instead of compressing the gap.
    if (index > 0 && position > 0
      && benchmark[position - 1].timestamp === bars[index - 1].timestamp
      && positive(benchmark[position - 1].close)) {
      returns[index] = finite(benchmark[position].close / benchmark[position - 1].close - 1);
    }
  });
  return { prices, returns };
}

function pairedStatistic(id: string, asset: number[], benchmark: number[]): Value {
  const a = moments(asset);
  const b = moments(benchmark);
  if (b.ss <= 0) return undefined;
  const cross = a.normalized.reduce((sum, value, index) => sum + (value - a.mean) * (b.normalized[index] - b.mean), 0);
  if (id === 'Q_BETA') return finite((cross / b.ss) * (a.scale / b.scale));
  if (id === 'Q_CORR') return a.ss > 0
    ? finite(Math.max(-1, Math.min(1, cross / Math.sqrt(a.ss * b.ss)))) : undefined;
  const beta = cross / b.ss;
  const residuals = a.normalized.map((value, index) => value - a.mean - beta * (b.normalized[index] - b.mean));
  // Exact and numerically indistinguishable perfect fits have no residual scale.
  const residualSS = residuals.reduce((sum, value) => sum + value ** 2, 0);
  if (residualSS <= (64 * Number.EPSILON) ** 2 * Math.max(1, a.ss) * asset.length) return undefined;
  return finite(residuals.at(-1)! / Math.sqrt(residualSS / asset.length));
}

export function computeStatistics(
  id: string, bars: IndicatorBar[], params: number[], context?: { benchmark?: IndicatorBar[] },
): IndicatorResult[] {
  const output: IndicatorResult[] = bars.map(() => ({}));
  const preset = defaults[id];
  if (!preset || !bars.length || !validOrder(bars)) return output;
  const period = params[0] ?? preset[0];
  const annual = params[1] ?? preset[1] ?? 252;
  const minimum = id === 'Q_DRAWDOWN' || id === 'Q_RS' ? 1 : id === 'Q_SKEW' ? 3 : 2;
  if (!Number.isInteger(period) || period < minimum || period > 10000
    || !Number.isFinite(annual) || annual <= 0) return output;
  const closes: Value[] = bars.map(bar => positive(bar.close) ? bar.close : undefined);
  const logs = logReturns(bars);
  const returns = simpleReturns(bars);
  const paired = id === 'Q_BETA' || id === 'Q_CORR' || id === 'Q_RESIDUALZ' || id === 'Q_RS';
  const benchmark = paired ? alignedBenchmark(bars, context?.benchmark ?? []) : undefined;

  const ranges: Value[] = id === 'Q_PARKINSON' || id === 'Q_GK' ? bars.map(bar => {
    if (![bar.open, bar.high, bar.low, bar.close].every(positive)
      || bar.high < Math.max(bar.open, bar.close, bar.low)
      || bar.low > Math.min(bar.open, bar.close, bar.high)) return undefined;
    const hl = Math.log(bar.high) - Math.log(bar.low);
    if (id === 'Q_PARKINSON') return finite(hl ** 2 / (4 * Math.LN2));
    const co = Math.log(bar.close) - Math.log(bar.open);
    return finite(Math.max(0, 0.5 * hl ** 2 - (2 * Math.LN2 - 1) * co ** 2));
  }) : [];

  for (let index = 0; index < bars.length; index++) {
    let value: Value;
    if (id === 'Q_RS') {
      if (index >= period && benchmark && positive(bars[index - period].close) && positive(bars[index].close)
        && benchmark.prices[index] !== undefined && benchmark.prices[index - period] !== undefined
        && trailing(benchmark.returns, index, period) && trailing(returns, index, period)) {
        value = finite(Math.expm1(Math.log(bars[index].close) - Math.log(bars[index - period].close)
          - Math.log(benchmark.prices[index]!) + Math.log(benchmark.prices[index - period]!)) * 100);
      }
    } else if (paired) {
      const asset = trailing(returns, index, period);
      const reference = benchmark && trailing(benchmark.returns, index, period);
      if (asset && reference) value = pairedStatistic(id, asset, reference);
    } else if (id === 'Q_ZSCORE' || id === 'Q_SLOPE' || id === 'Q_R2' || id === 'Q_DRAWDOWN') {
      const window = trailing(closes, index, period);
      if (window) {
        if (id === 'Q_ZSCORE') value = zScore(window);
        else if (id === 'Q_SLOPE') value = finite(timeRegression(window).slope);
        else if (id === 'Q_R2') value = timeRegression(window).r2;
        else value = finite((window.at(-1)! / window.reduce((peak, price) => Math.max(peak, price), 0) - 1) * 100);
      }
    } else if (id === 'Q_PARKINSON' || id === 'Q_GK') {
      const window = trailing(ranges, index, period);
      if (window) value = finite(Math.sqrt(window.reduce((sum, estimate) => sum + estimate, 0) / period * annual) * 100);
    } else if (id === 'Q_HVOL' || id === 'Q_SKEW' || id === 'Q_RETURNZ') {
      const window = trailing(logs, index, period);
      if (window) {
        if (id === 'Q_RETURNZ') value = zScore(window);
        else {
          const state = moments(window);
          if (id === 'Q_HVOL') value = finite(Math.sqrt(state.ss / (period - 1)) * state.scale * Math.sqrt(annual) * 100);
          else if (state.ss > 0) {
            const third = state.normalized.reduce((sum, observation) => sum + (observation - state.mean) ** 3, 0) / period;
            value = finite(Math.sqrt(period * (period - 1)) / (period - 2) * third / (state.ss / period) ** 1.5);
          }
        }
      }
    } else if (id === 'Q_SHARPE' || id === 'Q_SORTINO') {
      const window = trailing(returns, index, period);
      if (window) {
        const state = moments(window);
        const denominator = id === 'Q_SHARPE' ? Math.sqrt(state.ss / (period - 1))
          : Math.sqrt(state.normalized.reduce((sum, observation) => sum + Math.min(observation, 0) ** 2, 0) / period);
        if (denominator > 0) value = finite(state.mean / denominator * Math.sqrt(annual));
      }
    }
    if (value !== undefined && Number.isFinite(value)) output[index] = { value };
  }
  return output;
}
