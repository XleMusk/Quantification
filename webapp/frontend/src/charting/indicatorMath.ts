/** Causal indicator helpers. Missing observations remain missing; no forward fill. */
export type IndicatorBar = { timestamp: number; open: number; high: number; low: number; close: number; volume: number; turnover?: number };
export type IndicatorResult = Record<string, number | undefined>;
export type NumericSeries = readonly (number | undefined)[];

export const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
export const safe = (value: number): number | undefined => finite(value) ? value : undefined;
export function period(value: number | undefined, fallback = 14): number {
  return finite(value) && value >= 1 ? Math.min(5000, Math.floor(value)) : fallback;
}

/** Complete-window sum: an unavailable value invalidates only windows containing it. */
export function sum(values: NumericSeries, length: number): (number | undefined)[] {
  const n = period(length); let total = 0; let valid = 0;
  return values.map((value, i) => {
    if (i >= n && finite(values[i - n])) { total -= values[i - n]!; valid--; }
    if (finite(value)) { total += value; valid++; }
    return i >= n - 1 && valid === n ? safe(total) : undefined;
  });
}
export function sma(values: NumericSeries, length: number): (number | undefined)[] {
  const n = period(length);
  return sum(values, n).map(value => value == null ? undefined : safe(value / n));
}

/** SMA-seeded exponential smoothing. Missing input resets the seed, not the price. */
function smooth(values: NumericSeries, length: number, alpha: number): (number | undefined)[] {
  const n = period(length); let seed: number[] = []; let previous: number | undefined;
  return values.map(value => {
    if (!finite(value)) { previous = undefined; seed = []; return undefined; }
    if (previous == null) {
      seed.push(value);
      if (seed.length < n) return undefined;
      previous = seed.reduce((a, b) => a + b, 0) / n; seed = [];
    } else previous += alpha * (value - previous);
    if (!finite(previous)) { previous = undefined; seed = []; }
    return previous;
  });
}
export function ema(values: NumericSeries, length: number): (number | undefined)[] {
  const n = period(length); return smooth(values, n, 2 / (n + 1));
}
export function wilder(values: NumericSeries, length: number): (number | undefined)[] {
  const n = period(length); return smooth(values, n, 1 / n);
}

/** Population standard deviation (ddof=0), using removable Welford moments. */
export function rollingStd(values: NumericSeries, length: number): (number | undefined)[] {
  const n = period(length); let count = 0; let mean = 0; let m2 = 0;
  return values.map((value, i) => {
    const old = i >= n ? values[i - n] : undefined;
    if (finite(old)) {
      if (count === 1) { count = 0; mean = 0; m2 = 0; }
      else { const nextMean = mean + (mean - old) / (count - 1); m2 -= (old - mean) * (old - nextMean); mean = nextMean; count--; }
    }
    if (finite(value)) { count++; const delta = value - mean; mean += delta / count; m2 += delta * (value - mean); }
    return i >= n - 1 && count === n ? safe(Math.sqrt(Math.max(0, m2 / n))) : undefined;
  });
}

function rollingExtreme(values: NumericSeries, length: number, maximum: boolean): (number | undefined)[] {
  const n = period(length); const queue: number[] = []; let head = 0; let invalid = 0;
  return values.map((value, i) => {
    if (i >= n && !finite(values[i - n])) invalid--;
    while (head < queue.length && queue[head] <= i - n) head++;
    if (finite(value)) {
      while (queue.length > head && (maximum ? values[queue[queue.length - 1]]! <= value : values[queue[queue.length - 1]]! >= value)) queue.pop();
      queue.push(i);
    } else invalid++;
    if (head > 1024 && head > queue.length / 2) { queue.splice(0, head); head = 0; }
    return i >= n - 1 && invalid === 0 ? values[queue[head]] : undefined;
  });
}
export const rollingMin = (values: NumericSeries, length: number) => rollingExtreme(values, length, false);
export const rollingMax = (values: NumericSeries, length: number) => rollingExtreme(values, length, true);

export function trueRange(bars: readonly IndicatorBar[]): (number | undefined)[] {
  return bars.map((bar, i) => {
    if (!finite(bar.high) || !finite(bar.low) || bar.high < bar.low) return undefined;
    const previous = bars[i - 1]?.close;
    return i === 0 ? safe(bar.high - bar.low) : finite(previous) ? safe(Math.max(bar.high - bar.low, Math.abs(bar.high - previous), Math.abs(bar.low - previous))) : undefined;
  });
}
export const atr = (bars: readonly IndicatorBar[], length = 14) => wilder(trueRange(bars), length);
export function rsi(values: NumericSeries, length = 14): (number | undefined)[] {
  const changes = values.map((value, i) => i && finite(value) && finite(values[i - 1]) ? safe(value - values[i - 1]!) : undefined);
  const gains = wilder(changes.map(value => value == null ? undefined : Math.max(value, 0)), length);
  const losses = wilder(changes.map(value => value == null ? undefined : Math.max(-value, 0)), length);
  return gains.map((gain, i) => gain == null || losses[i] == null ? undefined : losses[i] === 0 ? gain === 0 ? 50 : 100 : safe(100 - 100 / (1 + gain / losses[i]!)));
}
