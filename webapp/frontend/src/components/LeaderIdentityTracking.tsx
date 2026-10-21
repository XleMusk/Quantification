import { useEffect, useState } from 'react';
import { api } from '../api';
import { ErrorMessage } from './Common';

type Tracking = {
  registered_at: string; source: { day: string; created_at: string }; member_count: number;
  first_eligible_close_day: string; observed_close_days: string[]; next_required_close_day: string | null;
  episode: null | { from_phase: string; to_phase: string; confirmation_close_day: string;
    fixed_window_close_days: string[]; complete_close_count: number };
  status_counts: Record<string, number>;
};
type View = { status: string; source_file: string; fixed_cohort_id: string; read_at: string; tracking: Tracking | null };

const phases: Record<string, string> = { broad_strength_snapshot: '普涨快照', broad_weakness_snapshot: '普跌快照', mixed_breadth_snapshot: '涨跌分化' };
const statuses: Record<string, string> = { waiting_environment_change: '等待首次环境变化', waiting_three_closes: '等待三个收盘',
  unknown_missing_or_unqualified_or_reference_incompatible: '缺失、资格或价格参考链未知',
  price_proxy_conditions_matched: '价格代理条件匹配', price_proxy_conditions_not_matched: '价格代理条件未匹配' };

export function LeaderIdentityTracking({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError('');
    api<View>('/trading/research/leader-identity', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(value => { if (!controller.signal.aborted) { setView(null); setError(value.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [refreshVersion]);
  const tracking = view?.tracking;
  const episode = tracking?.episode;
  return <details className="offline-details" open aria-label="事前固定龙股身份观察">
    <summary>事前身份观察 · 固定完整集合{busy ? ' · 读取中' : ''}</summary>
    {error && <ErrorMessage message={error} />}
    {!tracking && !error && <p role="status">{busy ? '读取已保存的固定注册记录。' : '固定集合尚未注册，暂不可观察。页面读取不会创建注册或补记历史。'}</p>}
    {tracking && <>
      <div className="leader-clock"><strong>实际注册 {tracking.registered_at}</strong><span>身份来源收盘 {tracking.source.day}</span><span>全体 {tracking.member_count} 只</span></div>
      <p className="subtle">来源分析记录 {tracking.source.created_at}；首个可观察收盘 {tracking.first_eligible_close_day}。注册前收盘仅作背景，不是入场价。</p>
      <p>已观察收盘：{tracking.observed_close_days.join('、') || '尚无'}；下一必需收盘：{tracking.next_required_close_day || '固定日历范围内暂无后继日'}。</p>
      {episode ? <p>固定首次变化：{phases[episode.from_phase] || episode.from_phase} → {phases[episode.to_phase] || episode.to_phase}，确认收盘 {episode.confirmation_close_day}。窗口 {episode.fixed_window_close_days.join('、')}；已记录 {episode.complete_close_count} 个收盘，形成 {Math.max(0, episode.complete_close_count - 1)} 个完整区间。</p>
        : <p>尚无已记录的首次收盘环境变化，继续等待事前注册后的准确收盘证据。</p>}
      <div className="table-scroll"><table className="compact" aria-label="固定身份集合状态"><thead><tr><th>全体成员状态</th><th>人数</th></tr></thead><tbody>{Object.entries(tracking.status_counts).map(([status, count]) => <tr key={status}><td>{statuses[status] || status}</td><td>{count}</td></tr>)}</tbody></table></div>
      <p className="subtle">三个收盘是两个完整区间：确认收盘 E → E1、E1 → E2。只跟踪固定的首次变化，不按后续结果另选窗口；这不是已验证穿越龙，也不是策略收益或交易盈亏。</p>
      <p className="subtle">接口保留全部 {tracking.member_count} 名成员，本卡仅展示集合与状态汇总，未展开逐股明细。缺失成员保留为未知，不能当作零收益或移出分母。</p>
    </>}
    {view && <p className="mono subtle">固定集合 {view.fixed_cohort_id} · 读取 {view.read_at}<br />{view.source_file}</p>}
  </details>;
}
