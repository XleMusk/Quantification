import type { Overlay, OverlayCreate } from 'klinecharts';
import type { Bar, Dataset } from '../types';

export const DRAWING_TOOLS = [
  ['segment', '趋势线段'], ['rayLine', '射线'], ['straightLine', '直线'],
  ['horizontalStraightLine', '水平线'], ['horizontalRayLine', '水平射线'], ['horizontalSegment', '水平线段'],
  ['verticalStraightLine', '垂直线'], ['verticalRayLine', '垂直射线'], ['verticalSegment', '垂直线段'],
  ['priceLine', '价格线'], ['priceChannelLine', '价格通道'], ['parallelStraightLine', '平行通道'],
  ['fibonacciLine', '斐波那契'], ['reviewRectangle', '矩形区域'], ['simpleAnnotation', '文字标注'], ['simpleTag', '价格标签'],
] as const;
export type SavedDrawing = Pick<OverlayCreate, 'name' | 'points' | 'styles' | 'lock' | 'visible' | 'extendData' | 'mode'> & { id: string; paneId: string };
export type ReviewState = { version: 1; drawings: SavedDrawing[]; notes: string };
export type ReplayCheckpoint = { version: 1; session: string; startDate: string; cursor: number };
const names = new Set<string>(DRAWING_TOOLS.map(([name]) => name));

export function createReplaySession(): string {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index++) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0'));
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`;
}

export function readLocal(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
export function readReview(key: string): ReviewState {
  const raw = readLocal(key);
  if (!raw) return { version: 1, drawings: [], notes: '' };
  const value = JSON.parse(raw) as ReviewState;
  if (value.version !== 1 || !Array.isArray(value.drawings) || typeof value.notes !== 'string') throw new Error('本地复盘记录格式无效，请先导出备份或清理该记录');
  if (value.drawings.some(d => !d || typeof d.id !== 'string' || !names.has(d.name) || !Array.isArray(d.points) || d.points.some(p => !(Number.isFinite(p.timestamp) || Number.isInteger(p.dataIndex)) || !Number.isFinite(p.value)))) throw new Error('本地画线数据无效，未载入');
  return value;
}
export function saveDrawing(overlay: Overlay, bars: Bar[]): SavedDrawing {
  return {
    id: overlay.id, name: overlay.name, paneId: overlay.paneId,
    points: overlay.points.map(point => {
      const day = point.dataIndex == null ? undefined : bars[point.dataIndex]?.date.slice(0, 10);
      const timestamp = point.timestamp ?? (day ? Date.parse(`${day}T00:00:00+08:00`) : undefined);
      // Blank-space anchors have an index but no observed timestamp; keep the
      // index within this immutable dataset instead of inventing a trading day.
      return Number.isFinite(timestamp) ? { timestamp, value: point.value } : { dataIndex: point.dataIndex, value: point.value };
    }),
    styles: overlay.styles ?? undefined, lock: overlay.lock, visible: overlay.visible, mode: overlay.mode,
    extendData: typeof overlay.extendData === 'string' ? overlay.extendData : undefined,
  };
}
export function reviewKey(dataset: Dataset, symbol: string, replaySession = ''): string {
  return `quant.review.v1:${dataset.id}:${encodeURIComponent(symbol)}:D:${dataset.adjustment}:${replaySession ? `replay:${replaySession}` : 'analysis'}`;
}
export const replayCheckpointKey = (dataset: Dataset, symbol: string) => `${reviewKey(dataset, symbol)}:latest-replay`;
export function readReplayCheckpoint(dataset: Dataset, symbol: string, bars: Bar[]): ReplayCheckpoint | null {
  try {
    const value = JSON.parse(readLocal(replayCheckpointKey(dataset, symbol)) ?? 'null') as ReplayCheckpoint | null;
    if (!value || value.version !== 1 || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.session)
      || !/^\d{4}-\d{2}-\d{2}$/.test(value.startDate) || !Number.isFinite(Date.parse(`${value.startDate}T00:00:00Z`))
      || new Date(`${value.startDate}T00:00:00Z`).toISOString().slice(0, 10) !== value.startDate
      || !Number.isInteger(value.cursor) || value.cursor > bars.length) return null;
    const first = bars.findIndex(bar => bar.date.slice(0, 10) >= value.startDate);
    return first >= 0 && value.startDate >= bars[0].date.slice(0, 10) && value.cursor >= first + 1 ? value : null;
  } catch { return null; }
}
export function tradingViewSymbol(symbol: string): string | null {
  const suffix = /^(\d{6})\.(SSE|SH|SZSE|SZ)$/i.exec(symbol);
  const prefix = /^(SH|SZ)(\d{6})$/i.exec(symbol);
  if (suffix) return `${/^(SSE|SH)$/i.test(suffix[2]) ? 'SSE' : 'SZSE'}:${suffix[1]}`;
  if (prefix) return `${prefix[1].toUpperCase() === 'SH' ? 'SSE' : 'SZSE'}:${prefix[2]}`;
  return null;
}
