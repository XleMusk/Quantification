import type { Bar } from '../types';

export type ReviewPeriod = 'D' | 'W' | 'M';
type AmountBar = Bar & { turnover?: number };
const DAY_MS = 86_400_000;

/** Read the date of a daily bar without locale or machine-timezone conversion. */
export function observationDate(value: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})(?:T00:00:00(?:\.\d{1,3})?Z?)?$/.exec(value);
  if (!match) throw new Error('复盘聚合仅接受日频日期或午夜日期时间');
  const date = match[1];
  const time = Date.parse(`${date}T00:00:00Z`);
  if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== date) {
    throw new Error('复盘聚合日期无效');
  }
  return date;
}

/** KLineCharts timestamps use the observed session date at Shanghai midnight. */
export const observationTimestamp = (date: string): number =>
  Date.parse(`${observationDate(date)}T00:00:00+08:00`);

function bucket(date: string, period: ReviewPeriod): string {
  if (period === 'D') return date;
  if (period === 'M') return date.slice(0, 7);
  const value = new Date(`${date}T00:00:00Z`);
  const mondayOffset = (value.getUTCDay() + 6) % 7;
  return new Date(value.getTime() - mondayOffset * DAY_MS).toISOString().slice(0, 10);
}

/**
 * Map a crosshair date to an existing candle in the target's own period.
 * A Wednesday belongs to this week's candle even if that visible candle is
 * stamped Friday. The caller must pass only its already-revealed target bars;
 * this helper never manufactures a target candle beyond that supplied series.
 * If the matching bucket is absent, use the closest earlier observed date.
 */
export function findLinkedBarIndex(sourceDate: string, targetBars: readonly Bar[], targetPeriod: ReviewPeriod): number {
  if (!['D', 'W', 'M'].includes(targetPeriod)) throw new Error('不支持的复盘周期');
  const date = observationDate(sourceDate);
  const sourceBucket = bucket(date, targetPeriod);
  let match = -1;
  let matchDate = '';
  let previous = -1;
  let previousDate = '';
  for (let index = 0; index < targetBars.length; index++) {
    const targetDate = observationDate(targetBars[index].date);
    if (bucket(targetDate, targetPeriod) === sourceBucket && targetDate >= matchDate) {
      match = index; matchDate = targetDate;
    }
    if (targetDate <= date && targetDate >= previousDate) {
      previous = index; previousDate = targetDate;
    }
  }
  return match >= 0 ? match : previous;
}

function usableTurnover(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * Aggregate only observed daily rows. There is no price/calendar forward fill.
 *
 * The last row in each bucket supplies its date and close. A replay's unfinished
 * week/month is therefore stamped with its last revealed session, never an
 * invented future Friday or month end. `asOf` filters before aggregation.
 * Turnover is propagated only when every contributing row explicitly has it.
 */
export function aggregateBars(bars: Bar[], period: ReviewPeriod, asOf?: string): Bar[] {
  if (!['D', 'W', 'M'].includes(period)) throw new Error('不支持的复盘周期');
  const cutoff = asOf === undefined ? undefined : observationDate(asOf);
  const rows = bars.map(bar => ({ bar, date: observationDate(bar.date) }))
    .filter(row => cutoff === undefined || row.date <= cutoff)
    .sort((a, b) => a.date.localeCompare(b.date));
  const symbol = rows[0]?.bar.symbol;
  let previousDate = '';
  for (const { bar, date } of rows) {
    if (bar.symbol !== symbol) throw new Error('复盘聚合须使用单个证券的行情');
    if (date === previousDate) throw new Error('复盘聚合不能包含重复观测日期');
    if (![bar.open, bar.high, bar.low, bar.close, bar.volume].every(Number.isFinite)
      || Math.min(bar.open, bar.high, bar.low, bar.close) <= 0 || bar.volume < 0
      || bar.high < Math.max(bar.open, bar.close, bar.low)
      || bar.low > Math.min(bar.open, bar.close, bar.high)) {
      throw new Error('复盘聚合行情包含无效 OHLC 或成交量');
    }
    previousDate = date;
  }
  const result: AmountBar[] = [];
  let currentBucket = '';
  let allHaveTurnover = false;
  for (const { bar, date } of rows) {
    const key = bucket(date, period);
    const amount = (bar as AmountBar).turnover;
    if (key !== currentBucket) {
      const next: AmountBar = { date: bar.date, symbol: bar.symbol, open: bar.open,
        high: bar.high, low: bar.low, close: bar.close, volume: bar.volume };
      allHaveTurnover = usableTurnover(amount);
      if (allHaveTurnover) next.turnover = amount;
      result.push(next); currentBucket = key;
    } else {
      const current = result[result.length - 1];
      current.date = bar.date; current.high = Math.max(current.high, bar.high);
      current.low = Math.min(current.low, bar.low); current.close = bar.close;
      current.volume += bar.volume;
      if (!Number.isFinite(current.volume)) throw new Error('聚合成交量超出有限数值范围');
      allHaveTurnover = allHaveTurnover && usableTurnover(amount);
      if (allHaveTurnover) {
        const sum = current.turnover! + amount!;
        if (Number.isFinite(sum)) current.turnover = sum;
        else { delete current.turnover; allHaveTurnover = false; }
      } else delete current.turnover;
    }
  }
  return result;
}

/** `isPartial` is conservative: the final aggregate MAY be incomplete. */
export function periodState(bars: Bar[], period: ReviewPeriod): { isPartial: boolean; lastLabel: string } {
  if (!bars.length) return { isPartial: false, lastLabel: '暂无可聚合行情' };
  const date = observationDate(bars[bars.length - 1].date);
  const name = period === 'D' ? '日线' : period === 'W' ? '周线' : '月线';
  return { isPartial: period !== 'D', lastLabel: period === 'D'
    ? `${name}截至 ${date} · 仅含已观测记录`
    : `${name}截至 ${date} · 按已揭示数据聚合，末根可能未完成` };
}

/**
 * Keep the asset calendar intact. A missing benchmark date remains missing so a
 * rolling calculation cannot silently bridge it by compacting both series.
 */
export function alignBenchmark(assetBars: Bar[], benchmarkBars: Bar[], period: ReviewPeriod, asOf?: string): {
  asset: Bar[]; benchmark: Bar[]; missingDates: string[];
} {
  const asset = aggregateBars(assetBars, period, asOf);
  if (!asset.length) return { asset, benchmark: [], missingDates: [] };
  const assetDates = new Set(asset.map(bar => observationDate(bar.date)));
  const lastAssetDate = observationDate(asset[asset.length - 1].date);
  const benchmark = aggregateBars(benchmarkBars, period, lastAssetDate)
    .filter(bar => assetDates.has(observationDate(bar.date)));
  const benchmarkDates = new Set(benchmark.map(bar => observationDate(bar.date)));
  return { asset, benchmark, missingDates: [...assetDates].filter(date => !benchmarkDates.has(date)) };
}
