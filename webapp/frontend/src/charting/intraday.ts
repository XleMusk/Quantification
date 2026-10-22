/** Minute bars preserve supplier end labels, Shanghai sessions and explicit gaps. */
export type MinutePeriod = '1m' | '5m' | '15m' | '30m' | '60m';
export const MINUTE_PERIODS: MinutePeriod[] = ['1m', '5m', '15m', '30m', '60m'];
export type MinuteBar = {
  date: string; timestamp: number; symbol: string; open: number; high: number; low: number; close: number;
  volume: number; amount?: number | null; turnover?: number | null; amount_usable?: boolean;
  session_date: string; session: 'morning' | 'afternoon' | 'opening_auction'; auction: boolean;
  gap_before: number; expected_minutes: number; observed_minutes: number; is_partial: boolean;
  bucket_end?: number;
};
export type MinuteDataset = {
  id: string; name: string; symbols: string[]; start: string; end: string; rows: number;
  coverage: Record<string, unknown>; warnings: string[]; kind?: string; source?: string;
};
export type MinuteSource = {
  dataset: MinuteDataset; symbol: string; prices: MinuteBar[]; calendar: string[];
  source_period: '1m'; source_kind: 'minute_snapshot'; price_basis: 'raw'; timezone: 'Asia/Shanghai';
  fields: string[]; turnover_available: boolean; coverage: Record<string, unknown>; warnings: string[];
  as_of?: string | null; realtime: false;
};
export type VwapPoint = { timestamp: number; value?: number; complete: boolean; reason?: string };
const MINUTE_MS = 60_000;
const SHANGHAI_MS = 8 * 60 * MINUTE_MS;

export function minuteTime(timestamp: number): { day: string; time: string; minute: number } {
  if (!Number.isSafeInteger(timestamp) || timestamp % MINUTE_MS !== 0) throw Error('分钟时间须精确到整分钟');
  const value = new Date(timestamp + SHANGHAI_MS);
  if (!Number.isFinite(value.getTime())) throw Error('分钟时间无效');
  return { day: value.toISOString().slice(0, 10), time: value.toISOString().slice(11, 16), minute: value.getUTCHours() * 60 + value.getUTCMinutes() };
}

/** A datetime-local input is a Shanghai wall clock, never the browser's zone. */
export function parseShanghaiMinute(value: string): number | undefined {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::00(?:\.0{1,3})?)?$/.exec(value.trim());
  if (!match) return undefined;
  const timestamp = Date.parse(`${match[1]}T${match[2]}:00+08:00`);
  if (!Number.isFinite(timestamp)) return undefined;
  const clock = minuteTime(timestamp);
  // Date.parse may normalize February 30 or 24:00; such input is not its label.
  return clock.day === match[1] && clock.time === match[2] ? timestamp : undefined;
}

/** Continuous-session ordinal 0..239. Lunch and overnight are not missing minutes. */
export function minuteSlot(timestamp: number): number {
  const { minute } = minuteTime(timestamp);
  if (minute >= 571 && minute <= 690) return minute - 571;
  if (minute >= 781 && minute <= 900) return minute - 781 + 120;
  if (minute === 565) return -1;
  throw Error('分钟标签不在已声明的沪深日间时段');
}

function sliceMinutes(bars: readonly MinuteBar[], asOf?: number): MinuteBar[] {
  if (asOf !== undefined && !Number.isFinite(asOf)) throw Error('回放截止时间无效');
  const rows = bars.filter(bar => asOf === undefined || bar.timestamp <= asOf).map(bar => ({ ...bar })).sort((a, b) => a.timestamp - b.timestamp);
  const symbol = rows[0]?.symbol;
  let last = -Infinity;
  for (const bar of rows) {
    const clock = minuteTime(bar.timestamp);
    const slot = minuteSlot(bar.timestamp);
    if (Date.parse(bar.date) !== bar.timestamp || bar.session_date !== clock.day || bar.symbol !== symbol || bar.timestamp <= last) throw Error('分钟日期、证券或唯一性不一致');
    if ((slot === -1) !== (bar.session === 'opening_auction') || (slot >= 0 && bar.session !== (slot < 120 ? 'morning' : 'afternoon'))) throw Error('分钟交易时段不一致');
    if (![bar.open, bar.high, bar.low, bar.close, bar.volume].every(Number.isFinite) || Math.min(bar.open, bar.high, bar.low, bar.close) <= 0 || bar.volume < 0 || bar.high < Math.max(bar.open, bar.close, bar.low) || bar.low > Math.min(bar.open, bar.close, bar.high)) throw Error('分钟 OHLC 或成交量无效');
    if (!Number.isInteger(bar.gap_before) || bar.gap_before < 0) throw Error('分钟缺口数量无效');
    last = bar.timestamp;
  }
  return rows;
}

function cashAmount(bar: MinuteBar): number | undefined {
  const amount = bar.amount;
  if (bar.amount_usable === false || typeof amount !== 'number' || !Number.isFinite(amount) || amount < 0 || (bar.volume === 0 ? amount !== 0 : amount <= 0)) return undefined;
  // Amount/volume is a monetary price, not the supplier's percentage turnover.
  const tolerance = .02 + Math.max(Math.abs(bar.high), Math.abs(bar.low)) * 2e-6;
  if (bar.volume > 0 && (amount / bar.volume < bar.low - tolerance || amount / bar.volume > bar.high + tolerance)) return undefined;
  return amount;
}

function minuteBucket(timestamp: number, period: MinutePeriod): { key: string; end: number; expected: number } {
  if (!MINUTE_PERIODS.includes(period)) throw Error('不支持的分钟周期');
  const slot = minuteSlot(timestamp);
  const { day } = minuteTime(timestamp);
  if (slot < 0) return { key: `${day}:auction`, end: timestamp, expected: 1 };
  const size = Number.parseInt(period, 10);
  const sessionStart = slot < 120 ? 0 : 120;
  const offset = slot - sessionStart;
  const group = Math.floor(offset / size);
  const endSlot = sessionStart + Math.min((group + 1) * size, 120) - 1;
  const endMinute = endSlot < 120 ? 571 + endSlot : 781 + endSlot - 120;
  return { key: `${day}:${sessionStart}:${group}`, end: Date.parse(`${day}T00:00:00+08:00`) + endMinute * MINUTE_MS, expected: Math.min(size, 120 - group * size) };
}

/** Aggregate after revealing the raw prefix. Never bridge a lunch break or auction. */
export function aggregateMinutes(bars: readonly MinuteBar[], period: MinutePeriod, asOf?: number): MinuteBar[] {
  const rows = sliceMinutes(bars, asOf);
  const result: MinuteBar[] = [];
  let previousKey = '';
  let allCash = false;
  for (const bar of rows) {
    const bucket = minuteBucket(bar.timestamp, period);
    const amount = cashAmount(bar);
    if (bucket.key !== previousKey) {
      const next: MinuteBar = { ...bar, expected_minutes: bucket.expected, observed_minutes: 1, bucket_end: bucket.end, is_partial: bar.timestamp < bucket.end || bucket.expected > 1, amount: amount ?? null, amount_usable: amount !== undefined };
      if (amount !== undefined) next.turnover = amount; else delete next.turnover;
      result.push(next); allCash = amount !== undefined; previousKey = bucket.key;
    } else {
      const current = result[result.length - 1];
      current.high = Math.max(current.high, bar.high); current.low = Math.min(current.low, bar.low);
      current.close = bar.close; current.date = bar.date; current.timestamp = bar.timestamp;
      current.volume += bar.volume; current.observed_minutes += 1;
      current.auction = current.auction || bar.auction;
      current.gap_before += bar.gap_before;
      allCash = allCash && amount !== undefined;
      if (allCash) {
        const total = current.amount! + amount!;
        if (Number.isFinite(total)) { current.amount = total; current.turnover = total; }
        else { allCash = false; current.amount = null; delete current.turnover; }
      } else { current.amount = null; delete current.turnover; }
      current.amount_usable = allCash;
      current.is_partial = current.observed_minutes < current.expected_minutes || current.timestamp < bucket.end;
      if (!Number.isFinite(current.volume)) throw Error('分钟聚合成交量超出数值范围');
    }
  }
  return result;
}

/** Strict cumulative VWAP: missing minutes or unusable amount invalidate the rest of that interval. */
export function minuteVwap(bars: readonly MinuteBar[], options: { mode: 'session' | 'anchored'; anchor?: number; asOf?: number; requestedDays?: readonly string[] }): VwapPoint[] {
  const rows = sliceMinutes(bars, options.asOf);
  let day = '', sumAmount = 0, sumVolume = 0, complete = true, priorSlot = -1, priorDay = '';
  let started = false;
  let reason: string | undefined;
  if (options.mode === 'anchored' && (options.anchor === undefined || !rows.some(bar => bar.timestamp === options.anchor))) return rows.map(bar => ({ timestamp: bar.timestamp, complete: false, reason: '锚点尚未揭示或不是实际分钟记录' }));
  return rows.map(bar => {
    const slot = minuteSlot(bar.timestamp);
    if (slot < 0) return { timestamp: bar.timestamp, complete: false, reason: '开盘集合竞价单独保留，不计入此连续时段均价' };
    if (options.mode === 'anchored' && bar.timestamp < options.anchor!) return { timestamp: bar.timestamp, complete: false, reason: '早于锚点' };
    const newDay = bar.session_date !== day;
    const beginning = options.mode === 'session' ? newDay : !started;
    if (beginning) {
      sumAmount = 0; sumVolume = 0; complete = true; reason = undefined; priorSlot = slot - 1;
      if (options.mode === 'session' && slot !== 0) { complete = false; reason = '缺少当日开头分钟'; }
      started = true;
    }
    const sameDay = priorDay === bar.session_date;
    // The anchor may start after a previous gap. Its own first bar is valid.
    if (!beginning && !sameDay && options.requestedDays?.some(requested => requested > priorDay && requested < bar.session_date)) { complete = false; reason = '请求日期间存在未返回的整日记录，交易日性质未核实'; }
    if (!beginning && ((sameDay && slot !== priorSlot + 1) || (!sameDay && (priorSlot !== 239 || slot !== 0)) || bar.gap_before > 0)) { complete = false; reason = '区间存在未返回分钟'; }
    if (options.mode === 'session' && bar.gap_before > 0) { complete = false; reason = '区间存在未返回分钟'; }
    const amount = cashAmount(bar);
    if (amount === undefined) { complete = false; reason = '成交额缺失、单位或价格范围不一致'; }
    if (amount !== undefined) { sumAmount += amount; sumVolume += bar.volume; }
    if (!Number.isFinite(sumAmount) || !Number.isFinite(sumVolume)) { complete = false; reason = '累计数值超出范围'; }
    const value = complete && sumVolume > 0 ? sumAmount / sumVolume : undefined;
    day = bar.session_date; priorDay = bar.session_date; priorSlot = slot;
    return { timestamp: bar.timestamp, ...(value === undefined ? {} : { value }), complete, ...(reason ? { reason } : sumVolume === 0 ? { reason: '尚无正成交量' } : {}) };
  });
}

/** Match the visible target bucket; a partial bucket retains its last observed time. */
export function linkedMinuteIndex(timestamp: number, targetBars: readonly MinuteBar[], period: MinutePeriod): number {
  const key = minuteBucket(timestamp, period).key;
  let earlier = -1;
  for (let index = 0; index < targetBars.length; index++) {
    if (minuteBucket(targetBars[index].timestamp, period).key === key) return index;
    if (targetBars[index].timestamp <= timestamp) earlier = index;
  }
  return earlier;
}

export function minuteIndicatorBars(bars: readonly MinuteBar[]) {
  return bars.map(bar => ({ timestamp: bar.timestamp, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume, ...(bar.turnover == null ? {} : { turnover: bar.turnover }) }));
}
