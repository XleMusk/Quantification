import { fmt } from '../types';

type ChangeSummary = { member_count: number; paired_count: number; unknown_count: number;
  up_count: number; down_count: number; flat_count: number;
  conditional_median_window_return_percent: number | null; full_cohort_breadth: number | null;
  conditional_paired_breadth: number | null; breadth_bounds: [number, number] | null };
export type CohortRecentChange = { schema: string; status: string; member_count?: number;
  latest_receipt: string | null; baseline_receipt: string | null;
  latest_response_received_at: string | null; baseline_response_received_at: string | null;
  window_response_span_seconds: number | null; summary: ChangeSummary | null; current_hint: ChangeSummary | null;
  groups: { id: string; name: string; member_count: number; summary: ChangeSummary | null; current_hint: ChangeSummary | null }[] };
const names: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙',
  trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙', cross_regime_candidate: '穿越龙候选',
  rebound_repair_candidate: '反包龙候选', sector_catchup_leader: '补涨龙', turnover_leader: '换手龙' };
const states: Record<string, string> = { missing: '尚无保存区间', current_same_slot_cohort_change: '读取时有效的本时段区间',
  historical_same_slot_cohort_change_not_current: '历史区间，已不代表当前',
  unknown_no_common_300_to_420_second_baseline: '约五分钟前的同一时段基线待核验',
  unknown_no_compatible_member_pairs: '有效证券配对待核验', unavailable_latest_attempt: '最新采集轮失败，区间不可用',
  unavailable_invalid_saved_cohort_change: '保存区间证据本次不可用',
  unavailable_locator_changed_during_read: '采集回执已更新，待下次读取' };
const count = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : fmt(value, 0);
const percent = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 2)}%`;
const breadth = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : percent(value * 100);
const bounds = (value?: [number, number] | null) => value ? `${breadth(value[0])} 至 ${breadth(value[1])}` : '待核验';

function ChangeSummaryView({ value }: { value: ChangeSummary }) {
  return <>
    <div className="window-summary"><span>完整分母 <strong>{count(value.member_count)}</strong></span><span>有效配对 / 未知 <strong>{count(value.paired_count)} / {count(value.unknown_count)}</strong></span><span>区间上涨 / 下跌 / 平盘 <strong>{count(value.up_count)} / {count(value.down_count)} / {count(value.flat_count)}</strong></span><span>已配对子集条件中位变化 <strong>{percent(value.conditional_median_window_return_percent)}</strong></span></div>
    <p className="subtle">全体区间广度 {breadth(value.full_cohort_breadth)}；已配对子集条件广度 {breadth(value.conditional_paired_breadth)}；含未知成员的保守区间 {bounds(value.breadth_bounds)}。</p>
  </>;
}

export function LeaderCohortRecentChange({ change, memberCount }: { change?: CohortRecentChange | null; memberCount?: number }) {
  const valid = change?.schema === 'fixed_leader_cohort_intraday_change_view_v1';
  const current = valid && change.status === 'current_same_slot_cohort_change' ? change.current_hint : null;
  const historical = valid && change.status === 'historical_same_slot_cohort_change_not_current' ? change.summary : null;
  const summary = current || historical;
  return <details className="offline-details" open aria-label="固定候选近五分钟变化">
    <summary>固定候选近五分钟变化 · {valid ? states[change.status] || '区间状态待核验' : '尚无可核验区间'}</summary>
    <p className="subtle">仅观察登记全体 {count(memberCount ?? change?.member_count)} 名候选，区间变化与较昨收的当日涨跌分别展示，不是全市场广度或交易信号。缺失和不兼容成员仍留在完整分母。</p>
    {valid && change.baseline_response_received_at && change.latest_response_received_at && <p className="subtle">保存响应区间 {change.baseline_response_received_at} 至 {change.latest_response_received_at}，实际跨度 {count(change.window_response_span_seconds)} 秒。先固定同一时段约五分钟前的完整采集轮次，再逐票核对行情源时差和相同昨收，不跨午休拼接。</p>}
    {summary ? <>
      <p><strong>{current ? '本次读取时仍有效的区间配对' : '保存历史区间统计，现已失效'}</strong></p>
      <ChangeSummaryView value={summary} />
      {current && change?.summary && <details className="offline-details"><summary>保存区间的历史配对统计 · 不代表当前</summary><ChangeSummaryView value={change.summary} /></details>}
      {!!change?.groups.length && <details className="offline-details"><summary>原龙型分组区间变化 · {change.groups.length} 类</summary>
        <p className="subtle">{current ? '以下仅统计本次读取时仍有效的配对。' : '以下为已失效历史区间。'}多标签分组人数不相加，条件中位数不代表全组；空组表示登记时没有已知匹配，不代表市场不存在该类龙股。</p>
        <div className="table-scroll"><table className="compact" aria-label="固定候选分组近五分钟变化"><thead><tr><th>登记龙型</th><th>全组人数</th><th>有效配对 / 未知</th><th>区间上涨 / 下跌 / 平盘</th><th>已配对条件中位变化</th><th>全组区间广度</th><th>保守广度区间</th></tr></thead><tbody>{change.groups.map(group => {
          const value = current ? group.current_hint : group.summary;
          return <tr key={group.id}><td>{names[group.id] || group.name}</td><td>{count(group.member_count)}</td><td>{value ? `${count(value.paired_count)} / ${count(value.unknown_count)}` : '待核验'}</td><td>{value ? `${count(value.up_count)} / ${count(value.down_count)} / ${count(value.flat_count)}` : '待核验'}</td><td>{percent(value?.conditional_median_window_return_percent)}</td><td>{breadth(value?.full_cohort_breadth)}</td><td>{bounds(value?.breadth_bounds)}</td></tr>;
        })}</tbody></table></div>
      </details>}
    </> : <p role="status">尚无本次可用的近五分钟配对统计。缺基线、零个有效配对或最新采集失败均保持待核验，不当作零涨跌，也不回退到旧成功区间。</p>}
    <p className="subtle">区间价格变化不验证流通盘、盘口承接或资金意图，不据此确认龙头、切换策略或生成订单。</p>
  </details>;
}
