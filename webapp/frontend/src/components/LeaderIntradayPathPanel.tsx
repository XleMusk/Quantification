import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';
import { ErrorMessage } from './Common';

type FeatureSummary = { member_count: number; known_count: number; unknown_count: number; conditional_median_change_percent: number | null };
type OutcomeSummary = { member_count: number; paired_count: number; unknown_count: number; up_count: number; down_count: number; flat_count: number; conditional_median_window_return_percent: number | null };
type FeatureGroup = { id: string; name: string; member_count: number; summary: FeatureSummary };
type OutcomeGroup = { id: string; name: string; member_count: number; summary: OutcomeSummary; conditional_median_difference_vs_rest_pct: number | null };
type ClockRange = { count: number; minimum: string | null; maximum: string | null; span_seconds: number | null };
type Anchor = { anchor_id: string; created_at: string; persisted_at: string; response_received_at: string; source_file: string;
  features: { summary: FeatureSummary; groups: FeatureGroup[]; archetype_groups: FeatureGroup[]; source_quote_clocks: { current_at_anchor: ClockRange } } };
type Outcome = { anchor_id: string; derived_at: string; status: string; anchor_created_at: string; anchor_persisted_at: string;
  anchor_response_received_at: string; selected_successor_receipt: string | null; successor_response_received_at: string | null;
  response_span_seconds: number | null; prospective_claim: boolean; summary: OutcomeSummary; groups: OutcomeGroup[]; archetype_groups: OutcomeGroup[] };
type View = { status: string; read_at: string; derived_at: string | null; source_file: string; anchor_count: number; outcome_count: number;
  pending_count: number; outcome_status_counts: Record<string, number>; latest_anchor: Anchor | null; latest_outcome: Outcome | null;
  protocol: null | { registered_at: string; source_day: string; source_available_at: string; target_count: number; job_id: string } };
const names: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙', trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙', cross_regime_candidate: '穿越龙候选', rebound_repair_candidate: '反包龙候选', sector_catchup_leader: '补涨龙', turnover_leader: '换手龙' };
const number = (value?: number | null, digits = 0) => value == null || !Number.isFinite(value) ? '待核验' : fmt(value, digits);
const percent = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 3)}%`;
const points = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 3)} 个百分点`;
const outcomeNames: Record<string, string> = { observed: '已有有效后继配对', observed_unknown: '后继已取得，有效配对未知', terminal_unknown_no_future_counterpart: '固定时限内无完整后继，保留未知' };

export function LeaderIntradayPathPanel({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<View | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(true);
  useEffect(() => {
    const controller = new AbortController(); setBusy(true); setError('');
    api<View>('/trading/research/leader-intraday-panel', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [refreshVersion]);
  const protocol = view?.protocol, anchor = view?.latest_anchor, outcome = view?.latest_outcome;
  const rest = outcome?.groups.find(group => group.id === 'rest');
  const source = anchor?.features.source_quote_clocks.current_at_anchor;
  return <details className="offline-details" open aria-label="固定龙型事前后继观察">
    <summary>固定龙型事前后继观察 · {busy ? '读取中' : error ? '本次不可用' : protocol ? '已保存的研究面板' : '等待实际注册'}</summary>
    <p className="subtle">先保存当时环境，再观察首个300至420秒后的完整后继。只记录固定旧龙型及固定非590其余名册，不预测、不选择龙型或股票，不生成交易。</p>
    {error && <ErrorMessage message={error} />}
    {!protocol && !error && <p role="status">{busy ? '读取已保存的固定协议与汇总，不启动任务。' : '本次固定面板尚未注册；页面读取不会创建协议、锚点或后继。'}</p>}
    {protocol && <>
      <div className="leader-clock"><strong>实际注册 {protocol.registered_at}</strong><span>旧标签来源收盘 {protocol.source_day}</span><span>完整名册 {number(protocol.target_count)}</span></div>
      <div className="window-summary"><span>已保存锚点 <strong>{number(view?.anchor_count)}</strong></span><span>已记录后继终态 <strong>{number(view?.outcome_count)}</strong></span><span>待观察 <strong>{number(view?.pending_count)}</strong></span></div>
      <p className="subtle">来源实际可得 {protocol.source_available_at}；汇总实际形成 {view?.derived_at || '尚无'}。较早的原始报价不是锚点的事前写盘时刻。空组表示登记时没有已知标签匹配，不代表市场不存在该类龙股。</p>
      {anchor ? <details className="offline-details" aria-label="最近实际锚点环境">
        <summary>最近实际锚点 · {anchor.anchor_id}</summary>
        <p className="subtle">创建 {anchor.created_at}；实际写盘 {anchor.persisted_at}；原轮响应接收 {anchor.response_received_at}。</p>
        <p>完整分母 {number(anchor.features.summary.member_count)}，当时价格已知 / 未知 {number(anchor.features.summary.known_count)} / {number(anchor.features.summary.unknown_count)}；较昨收条件中位涨跌 {percent(anchor.features.summary.conditional_median_change_percent)}。</p>
        <p className="subtle">当时有效源报价 {source?.minimum || '待核验'} 至 {source?.maximum || '待核验'}，源跨度 {number(source?.span_seconds, 1)} 秒。分批报价不是共同瞬时，也未确认盘口或参与者意图。</p>
        <div className="table-scroll"><table className="compact" aria-label="最近锚点八类固定龙型"><thead><tr><th>固定旧龙型</th><th>完整人数</th><th>当时已知 / 未知</th><th>较昨收条件中位涨跌</th></tr></thead><tbody>{anchor.features.archetype_groups.map(group => <tr key={group.id}><td>{names[group.id] || group.name}</td><td>{number(group.member_count)}</td><td>{number(group.summary.known_count)} / {number(group.summary.unknown_count)}</td><td>{group.summary.known_count ? percent(group.summary.conditional_median_change_percent) : '待核验'}</td></tr>)}</tbody></table></div>
      </details> : <p role="status">协议已登记，尚无实际保存的锚点。未观察不计为失败。</p>}
      {outcome ? <details className="offline-details" open aria-label="最近实际后继结果">
        <summary>最近记录后继 · 对应锚点 {outcome.anchor_id}</summary>
        <p><strong>{outcomeNames[outcome.status] || '后继状态待核验'}</strong>。最近锚点与此后继{anchor?.anchor_id === outcome.anchor_id ? '属于同一记录' : '不是同一记录，不能直接拼作一组事前判断'}。</p>
        <p className="subtle">对应锚点创建 {outcome.anchor_created_at}、实际写盘 {outcome.anchor_persisted_at}；保存响应区间 {outcome.anchor_response_received_at} 至 {outcome.successor_response_received_at || '无完整后继'}，跨度 {number(outcome.response_span_seconds, 1)} 秒；后继记录 {outcome.derived_at}。</p>
        <p>完整分母 {number(outcome.summary.member_count)}，有效配对 / 未知 {number(outcome.summary.paired_count)} / {number(outcome.summary.unknown_count)}；区间上涨 / 下跌 / 平盘 {number(outcome.summary.up_count)} / {number(outcome.summary.down_count)} / {number(outcome.summary.flat_count)}。{outcome.prospective_claim ? '保存记录包含锚点写盘之后的有效源报价配对。' : '本条未形成可称为事前观察的有效配对。'}</p>
        <p className="subtle">固定非590其余名册条件中位变化 {percent(rest?.summary.conditional_median_window_return_percent)}，配对 / 未知 {number(rest?.summary.paired_count)} / {number(rest?.summary.unknown_count)}。下表之差只比较各自已知配对子集，不表示完整总体差异。</p>
        <div className="table-scroll"><table className="compact" aria-label="后继八类固定龙型"><thead><tr><th>固定旧龙型</th><th>完整人数</th><th>有效配对 / 未知</th><th>后继条件中位变化</th><th>相对固定其余名册条件中位之差</th></tr></thead><tbody>{outcome.archetype_groups.map(group => <tr key={group.id}><td>{names[group.id] || group.name}</td><td>{number(group.member_count)}</td><td>{number(group.summary.paired_count)} / {number(group.summary.unknown_count)}</td><td>{group.summary.paired_count ? percent(group.summary.conditional_median_window_return_percent) : '待核验'}</td><td>{group.summary.paired_count ? points(group.conditional_median_difference_vs_rest_pct) : '待核验'}</td></tr>)}</tbody></table></div>
      </details> : <p role="status">尚无已保存的后继终态，待观察与未知均不计为失败，也不当作零涨跌。</p>}
    </>}
    <p className="subtle">重叠窗口不是独立试验，旧龙型标签可重叠、人数不得相加。缺失和不兼容成员保留未知；这不是预测准确率、胜率、策略收益或当前信号，不确认穿越龙，不打开执行准入。</p>
    {view && <p className="mono subtle" style={{ overflowWrap: 'anywhere' }}>本次读取 {view.read_at}<br />{view.source_file}</p>}
  </details>;
}
