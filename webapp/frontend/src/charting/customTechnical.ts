import { atr, ema, finite, period, rollingMax, rollingMin, rollingStd, rsi, safe, sma, sum, type IndicatorBar, type IndicatorResult } from './indicatorMath';

/**
 * Public formula implementations, independent of proprietary scripts.
 * ATR uses Wilder smoothing; EMA uses an initial full-window SMA.
 * References: TradingView support articles for ATR (43000501823), Supertrend
 * (43000634738), Stoch RSI (43000502333), MFI (43000502348), Chaikin oscillator
 * (43000501979); Marc Chaikin's CMF description at help.chaikinanalytics.com.
 * All windows include only the current and previous observed bars. A Donchian
 * band is descriptive of this bar's window, not a prior-bar breakout signal.
 */
export const TECHNICAL_IDS = [
  'Q_ATR', 'Q_NATR', 'Q_SUPERTREND', 'Q_DONCHIAN', 'Q_KELTNER', 'Q_STOCHRSI',
  'Q_CMF', 'Q_MFI', 'Q_CHAIKIN', 'Q_FORCE', 'Q_RVOL', 'Q_BBP', 'Q_BBW', 'Q_ER',
];
const output = (values: (number | undefined)[]): IndicatorResult[] => values.map(value => ({ value }));
const ratio = (numerator: number | undefined, denominator: number | undefined, scale = 1) => numerator == null || denominator == null || denominator === 0 ? undefined : safe(scale * numerator / denominator);

export function computeTechnical(id: string, bars: IndicatorBar[], params: number[] = []): IndicatorResult[] {
  const closes = bars.map(bar => finite(bar.close) && bar.close > 0 ? bar.close : undefined);
  const volumes = bars.map(bar => finite(bar.volume) && bar.volume >= 0 ? bar.volume : undefined);
  switch (id) {
    case 'Q_ATR': return output(atr(bars, period(params[0], 14)));
    case 'Q_NATR': return output(atr(bars, period(params[0], 14)).map((value, i) => ratio(value, closes[i], 100)));
    case 'Q_SUPERTREND': {
      const ranges = atr(bars, period(params[0], 10)); const multiplier = finite(params[1]) && params[1] > 0 ? params[1] : 3;
      let upper: number | undefined; let lower: number | undefined; let direction = -1;
      return bars.map((bar, i) => {
        const range = ranges[i];
        if (range == null || closes[i] == null || !finite(bar.high) || !finite(bar.low)) { upper = undefined; lower = undefined; direction = -1; return {}; }
        const basicUpper = (bar.high + bar.low) / 2 + multiplier * range;
        const basicLower = (bar.high + bar.low) / 2 - multiplier * range;
        const previousUpper = upper; const previousLower = lower;
        upper = previousUpper == null || basicUpper < previousUpper || bars[i - 1].close > previousUpper ? basicUpper : previousUpper;
        lower = previousLower == null || basicLower > previousLower || bars[i - 1].close < previousLower ? basicLower : previousLower;
        if (previousUpper == null || previousLower == null) direction = -1;
        else direction = direction === -1 ? bar.close > upper ? 1 : -1 : bar.close < lower ? -1 : 1;
        return { trend: safe(direction === 1 ? lower : upper), direction };
      });
    }
    case 'Q_DONCHIAN': {
      const n = period(params[0], 20); const upper = rollingMax(bars.map(bar => finite(bar.high) ? bar.high : undefined), n); const lower = rollingMin(bars.map(bar => finite(bar.low) ? bar.low : undefined), n);
      return bars.map((_, i) => ({ upper: upper[i], lower: lower[i], middle: upper[i] == null || lower[i] == null ? undefined : safe((upper[i]! + lower[i]!) / 2) }));
    }
    case 'Q_KELTNER': {
      const center = ema(closes, period(params[0], 20)); const ranges = atr(bars, period(params[1], 10)); const multiplier = finite(params[2]) && params[2] > 0 ? params[2] : 2;
      return bars.map((_, i) => ({ middle: center[i], upper: center[i] == null || ranges[i] == null ? undefined : safe(center[i]! + multiplier * ranges[i]!), lower: center[i] == null || ranges[i] == null ? undefined : safe(center[i]! - multiplier * ranges[i]!) }));
    }
    case 'Q_STOCHRSI': {
      const relative = rsi(closes, period(params[0], 14)); const n = period(params[1], 14); const low = rollingMin(relative, n); const high = rollingMax(relative, n);
      // A flat RSI window has no stochastic range: leave it unavailable.
      const raw = relative.map((value, i) => value == null || low[i] == null || high[i] == null ? undefined : ratio(value - low[i]!, high[i]! - low[i]!, 100));
      const k = sma(raw, period(params[2], 3)); const d = sma(k, period(params[3], 3));
      return bars.map((_, i) => ({ k: k[i], d: d[i] }));
    }
    case 'Q_CMF': {
      const n = period(params[0], 20); const weighted = moneyFlowVolume(bars); const numerator = sum(weighted, n); const denominator = sum(volumes, n);
      return output(numerator.map((value, i) => ratio(value, denominator[i])));
    }
    case 'Q_MFI': {
      const typical = bars.map(bar => finite(bar.high) && finite(bar.low) && finite(bar.close) ? safe((bar.high + bar.low + bar.close) / 3) : undefined);
      const positive = typical.map((value, i) => !i || value == null || typical[i - 1] == null || volumes[i] == null ? undefined : value > typical[i - 1]! ? safe(value * volumes[i]!) : 0);
      const negative = typical.map((value, i) => !i || value == null || typical[i - 1] == null || volumes[i] == null ? undefined : value < typical[i - 1]! ? safe(value * volumes[i]!) : 0);
      const pos = sum(positive, period(params[0], 14)); const neg = sum(negative, period(params[0], 14));
      // No positive or negative flow is unavailable, not a fabricated neutral value.
      return output(pos.map((value, i) => value == null || neg[i] == null || value + neg[i]! === 0 ? undefined : neg[i] === 0 ? 100 : safe(100 - 100 / (1 + value / neg[i]!))));
    }
    case 'Q_CHAIKIN': {
      let accumulation = 0; const adl = moneyFlowVolume(bars).map(value => {
        if (value == null) { accumulation = 0; return undefined; }
        return safe(accumulation += value);
      });
      const short = ema(adl, period(params[0], 3)); const long = ema(adl, period(params[1], 10));
      return output(short.map((value, i) => value == null || long[i] == null ? undefined : safe(value - long[i]!)));
    }
    case 'Q_FORCE': {
      const raw = closes.map((close, i) => !i || close == null || closes[i - 1] == null || volumes[i] == null ? undefined : safe((close - closes[i - 1]!) * volumes[i]!));
      return output(ema(raw, period(params[0], 13)));
    }
    case 'Q_RVOL': {
      // Current volume / mean of the PREVIOUS n bars, not a self-inclusive baseline.
      const average = sma(volumes.map((_, i) => i ? volumes[i - 1] : undefined), period(params[0], 20));
      return output(volumes.map((volume, i) => ratio(volume, average[i])));
    }
    case 'Q_BBP':
    case 'Q_BBW': {
      const n = period(params[0], 20); const multiplier = finite(params[1]) && params[1] > 0 ? params[1] : 2; const center = sma(closes, n); const deviation = rollingStd(closes, n);
      return output(closes.map((close, i) => {
        if (close == null || center[i] == null || deviation[i] == null) return undefined;
        const spread = multiplier * deviation[i]!;
        return id === 'Q_BBP' ? ratio(close - (center[i]! - spread), 2 * spread) : ratio(2 * spread, center[i], 100);
      }));
    }
    case 'Q_ER': {
      const n = period(params[0], 10); const path = sum(closes.map((value, i) => !i || value == null || closes[i - 1] == null ? undefined : safe(Math.abs(value - closes[i - 1]!))), n);
      // A completely flat valid path has efficiency 0.
      return output(closes.map((value, i) => i < n || value == null || closes[i - n] == null || path[i] == null ? undefined : path[i] === 0 ? 0 : ratio(Math.abs(value - closes[i - n]!), path[i])));
    }
    default: throw new Error(`Unknown technical indicator: ${id}`);
  }
}

function moneyFlowVolume(bars: IndicatorBar[]): (number | undefined)[] {
  return bars.map(bar => {
    if (![bar.high, bar.low, bar.close, bar.volume].every(finite) || bar.high < bar.low || bar.volume < 0) return undefined;
    return bar.high === bar.low ? 0 : safe(((2 * bar.close - bar.high - bar.low) / (bar.high - bar.low)) * bar.volume);
  });
}
