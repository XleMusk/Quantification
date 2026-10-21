export type Dataset = {
  id: string; name: string; kind: 'historical' | 'synthetic'; source: string; adjustment: string;
  rows: number; symbols: string[]; start: string; end: string; created_at: string; warnings: string[];
  quality: { invalid_rows: number; zero_volume_rows: number; missing_grid_rows: number; sessions: number; calendar_kind: string };
};
export type Bar = { date: string; symbol: string; open: number; high: number; low: number; close: number; volume: number; turnover?: number };
export type Preview = { dataset: Dataset; symbol: string; prices: Bar[]; recent: Bar[] };
export type Config = { start: string; end: string; min_pairs: number; factors: string[]; holding_sessions: number };
export type Job = { id: string; dataset_id: string; status: string; progress: number; config: Config; error: string | null; cancel_requested: number; created_at: string; updated_at: string; logs: { time: string; message: string }[] };
export type Summary = { factor: string; valid_ic_days: number; mean_rank_ic: number | null; std_rank_ic: number | null; q1_mean_label: number | null; q2_mean_label: number | null; q3_mean_label: number | null; q4_mean_label: number | null; q5_mean_label: number | null };
export type Result = { job_id: string; dataset: Dataset; config: Config; completed_at: string; summary: Summary[]; daily_ic: { date: string; factor: string; rank_ic: number | null; n_pairs: number }[]; correlation: Record<string, string | number | null>[]; warnings: string[] };
export const factorNames: Record<string, string> = { reversal_5: '5 日反转', momentum_20: '20 日动量', relative_volume_20: '20 日相对成交量' };
export const statusNames: Record<string, string> = { queued: '排队中', running: '计算中', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消' };
export const adjustmentNames: Record<string, string> = { forward: '前复权', backward: '后复权', raw: '不复权', synthetic: '合成价格', unknown: '未声明' };
export const fmt = (value: number | null | undefined, digits = 3) => value == null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits });
export const dateText = (value: string) => value.slice(0, 10);
export const timeText = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
