import { fmt } from '../types';
import { MarketRosterActivity, type MarketActivityContext } from './MarketRosterActivity';

type Summary = { member_count: number; paired_count: number; unknown_count: number;
  up_count: number; down_count: number; flat_count: number;
  conditional_median_window_return_percent: number | null; full_cohort_breadth: number | null;
  conditional_paired_breadth: number | null; breadth_bounds: [number, number] | null };
type Group = { id: string; name: string; member_count: number; summary: Summary | null; current_hint: Summary | null };
export type MarketRosterChange = { schema: string; status: string; latest_attempt_status?: string;
  member_count?: number; latest_receipt: string | null; baseline_receipt: string | null;
  latest_response_received_at: string | null; baseline_response_received_at: string | null;
  window_response_span_seconds: number | null; summary: Summary | null; current_hint: Summary | null;
  groups: Group[]; archetype_groups: Group[]; activity_context?: MarketActivityContext | null };
const names: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙',
  trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙', cross_regime_candidate: '穿越龙候选',
  rebound_repair_candidate: '反包龙候选', sector_catchup_leader: '补涨龙', turnover_leader: '换手龙' };
const statuses: Record<string, string> = { current_same_watch_roster_change: '读取时有效的同任务区间',
  historical_same_watch_roster_change_not_current: '历史区间，已不代表当前',
  unknown_no_common_300_to_420_second_baseline: '同任务约五分钟前的基线待核验',
  unknown_no_compatible_member_pairs: '有效证券配对待核验', no_same_watch_window: '尚无同任务区间',
  unavailable_latest_attempt: '最新采集未完成或失败，本次区间不可用',
  unavailable_invalid_saved_market_roster_change: '保存区间证据本次不可用',
  unavailable_locator_changed_during_read: '回执已更新，待下次读取' };
const count = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : fmt(value, 0);
const percent = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 2)}%`;
const breadth = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : percent(value * 100);
const bounds = (value?: [number, number] | null) => value ? `${breadth(value[0])} 至 ${breadth(value[1])}` : '待核验';

function WindowSummary({ value }: { value: Summary }) {
  return <>
    <div className="window-summary"><span>完整名册分母 <strong>{count(value.member_count)}</strong></span><span>有效配对 / 未知 <strong>{count(value.paired_count)} / {count(value.unknown_count)}</strong></span><span>区间上涨 / 下跌 / 平盘 <strong>{count(value.up_count)} / {count(value.down_count)} / {count(value.flat_count)}</strong></span><span>已配对子集条件中位变化 <strong>{value.paired_count ? percent(value.conditional_median_window_return_percent) : '待核验'}</strong></span></div>
    <p className="subtle">全名册区间广度 {breadth(value.full_cohort_breadth)}；已配对子集条件广度 {breadth(value.conditional_paired_breadth)}；含未知成员的保守区间 {bounds(value.breadth_bounds)}。未知不当作零变化，也不移出完整分母。</p>
  </>;
}

function WindowGroups({ groups, current, archetypes = false }: { groups: Group[]; current: boolean; archetypes?: boolean }) {
  return <details className="offline-details"><summary>{archetypes ? `原590龙型区间分组 · ${groups.length} 类` : `宽名册背景区间分组 · ${groups.length} 组`}</summary>
    <p className="subtle">{current ? '本次读取时仍有效的区间配对。' : '以下为已失效历史区间。'}{archetypes ? '仅沿用原固定590的收盘标签，多标签人数不得相加；不据盘中变化重新确认龙头。空组表示登记时没有已知匹配，不代表市场不存在该类龙股。' : '全体、原590与其余成员保留各自分母；前日ST等背景组与前述分组重叠，人数不能相加，也不作为今日资格。'}没有有效配对的组不显示中位数。</p>
    <div className="table-scroll"><table className="compact" aria-label={archetypes ? '宽名册原龙型近五分钟变化' : '宽名册背景近五分钟变化'}><thead><tr><th>{archetypes ? '登记龙型' : '固定背景组'}</th><th>完整人数</th><th>有效配对 / 未知</th><th>区间上涨 / 下跌 / 平盘</th><th>已配对条件中位变化</th><th>全组区间广度</th><th>保守广度区间</th></tr></thead><tbody>{groups.map(group => {
      const value = current ? group.current_hint : group.summary;
      return <tr key={group.id}><td>{archetypes ? names[group.id] || group.name : group.name}</td><td>{count(group.member_count)}</td><td>{value ? `${count(value.paired_count)} / ${count(value.unknown_count)}` : '待核验'}</td><td>{value ? `${count(value.up_count)} / ${count(value.down_count)} / ${count(value.flat_count)}` : '待核验'}</td><td>{group.member_count && value?.paired_count ? percent(value.conditional_median_window_return_percent) : '待核验'}</td><td>{breadth(value?.full_cohort_breadth)}</td><td>{bounds(value?.breadth_bounds)}</td></tr>;
    })}</tbody></table></div>
  </details>;
}

export function MarketRosterRecentChange({ change, memberCount }: { change?: MarketRosterChange | null; memberCount?: number | null }) {
  const valid = change?.schema === 'prior_dated_market_roster_intraday_change_view_v1';
  const current = valid && change.status === 'current_same_watch_roster_change' ? change.current_hint : null;
  const historical = valid && change.status === 'historical_same_watch_roster_change_not_current' ? change.summary : null;
  const summary = current || historical;
  const label = valid && change.status === 'unavailable_latest_attempt' && change.latest_attempt_status === 'started'
    ? '最新轮正在采集，本次区间待核验' : valid ? statuses[change.status] || '区间状态待核验' : '尚无可核验区间';
  return <details className="offline-details" open aria-label="宽名册近五分钟变化">
    <summary>宽名册近五分钟变化 · {label}</summary>
    <p className="subtle">完整名册分母 {count(memberCount ?? change?.member_count)}，仅限前交易日沪深名册。区间变化与较昨收的当日涨跌分别展示；当日覆盖与资格仍待核验，本段不改变策略准入。</p>
    {valid && change.baseline_response_received_at && change.latest_response_received_at && <p className="subtle">保存响应区间 {change.baseline_response_received_at} 至 {change.latest_response_received_at}，实际跨度 {change.window_response_span_seconds == null ? '待核验' : fmt(change.window_response_span_seconds, 1)} 秒。先按响应时间固定300至420秒前的同任务、同时段基线，再逐票核验源时差也在此范围内且昨收参考一致；不按配对数量或涨跌结果另选基线。昨收修订、缺失或不兼容成员保留为未知。</p>}
    {summary ? <>
      <p><strong>{current ? '本次读取时仍有效的宽名册区间配对' : '保存宽名册历史区间，现已失效'}</strong></p>
      <WindowSummary value={summary} />
      {current && change?.summary && <details className="offline-details"><summary>保存宽名册区间统计 · 不代表当前</summary><WindowSummary value={change.summary} /></details>}
      {!!change?.groups.length && <WindowGroups groups={change.groups} current={!!current} />}
      {!!change?.archetype_groups.length && <WindowGroups groups={change.archetype_groups} current={!!current} archetypes />}
    </> : <p role="status">尚无本次可用的宽名册近五分钟统计。没有同一采集任务、缺基线、零个有效配对或最新轮尚未成功时均待核验，不当作零涨跌，不用旧成功区间替代。</p>}
    <MarketRosterActivity value={summary ? change?.activity_context : undefined} current={!!current} mode="same_watch_window_delta" />
    <p className="subtle">短时价格分布不识别资金净流入、资金意图或盘口承接，不据此确认穿越龙、当前选股或生成交易。</p>
  </details>;
}
