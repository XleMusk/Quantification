import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';
import { MarketRosterRecentChange, type MarketRosterChange } from './MarketRosterRecentChange';
import { MarketRosterActivity, type MarketActivityContext } from './MarketRosterActivity';

type Summary = { member_count: number; known_count: number; unknown_count: number;
  advance_count: number; decline_count: number; flat_count: number;
  conditional_median_change_percent: number | null; full_cohort_breadth: number | null;
  conditional_known_breadth: number | null; breadth_bounds: [number, number] | null };
type Group = { id: string; name: string; member_count: number;
  current_summary: Summary | null; observation_summary: Summary | null };
type IndustryGroup = { industryClassification: string | null; industry: string | null; member_count: number;
  industry_mapping_covered_count: number; fixed_590_count: number;
  current_summary: Summary | null; observation_summary: Summary | null };
type IndustryContext = { schema: string; status: string; member_count: number; mapped_target_count: number;
  unknown_mapping_count: number; mapping_coverage: number; groups: IndustryGroup[]; unknown_mapping: IndustryGroup | null;
  mapping?: { snapshot_date: string; known_at: string; source_update_dates: string[];
    snapshot_staleness_calendar_days_at_source: number; snapshot_staleness_calendar_days_at_read: number }; error?: string };
type ClockRange = { count: number; minimum: string | null; maximum: string | null; span_seconds: number | null };
type View = { status: string; read_at: string; source_file: string | null; targets_file: string | null;
  target_count: number | null; source_day: string | null; observation_day: string | null;
  source_available_at: string | null; targets_created_at?: string; current_summary: Summary | null;
  observation_summary: Summary | null; groups: Group[]; industry_context?: IndustryContext | null;
  recent_change?: MarketRosterChange | null;
  activity_context?: MarketActivityContext | null;
  session: { session_day: string; session_name: string; session_start: string; stop_at: string } | null;
  snapshot: { started_at: string; finished_at: string | null; response_received_at: string | null;
    planned_batches: number; completed_batches: number; quote_request_count: number; status: string } | null;
  collector: { recorded_status: string; process: { status: string } } | null;
  source_quote_clocks: { all_parseable_including_stale_or_nonregular: ClockRange;
    regular_known_at_final_response: ClockRange; regular_current_at_read: ClockRange } | null;
  fixed_590_registry?: { member_count: number; present_in_prior_roster_count: number; absent_from_prior_roster_symbols: string[] };
  error?: string };
const statuses: Record<string, string> = { waiting_for_first_market_roster_snapshot: '等待首份名册报价回执',
  collecting_snapshot: '保存状态：本次采集尚未完成', current_prior_dated_roster_snapshot: '读取时仍有有效报价',
  saved_prior_dated_roster_snapshot_not_current: '历史观察，已不代表当前',
  unavailable_latest_market_roster_snapshot: '最新采集失败，本次不可用',
  unavailable_invalid_saved_market_roster_snapshot: '保存证据本次不可用',
  unavailable_locator_changed_during_read: '回执已更新，待下次读取' };
const count = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : fmt(value, 0);
const percent = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 2)}%`;
const breadth = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : percent(value * 100);
const bounds = (value?: [number, number] | null) => value ? `${breadth(value[0])} 至 ${breadth(value[1])}` : '待核验';
const clock = (value?: ClockRange | null) => value?.minimum && value.maximum
  ? `${value.minimum} 至 ${value.maximum}（${count(value.count)} 项，源时点相差 ${count(value.span_seconds)} 秒）` : '待核验';

function SummaryView({ value }: { value: Summary }) {
  return <>
    <div className="window-summary"><span>完整名册分母 <strong>{count(value.member_count)}</strong></span><span>已知 / 未知 <strong>{count(value.known_count)} / {count(value.unknown_count)}</strong></span><span>已知上涨 / 下跌 / 平盘 <strong>{count(value.advance_count)} / {count(value.decline_count)} / {count(value.flat_count)}</strong></span><span>已知子集条件中位涨跌 <strong>{percent(value.conditional_median_change_percent)}</strong></span></div>
    <p className="subtle">全名册广度 {breadth(value.full_cohort_breadth)}；已知子集条件广度 {breadth(value.conditional_known_breadth)}；含未知成员的保守区间 {bounds(value.breadth_bounds)}。未知不视为平盘，也不移出完整分母。</p>
  </>;
}

function IndustryView({ value, current }: { value: IndustryContext; current: boolean }) {
  const available = value.schema === 'market_roster_lagged_industry_intraday_context_v1' && value.status === 'observed_lagged_industry_price_context_only';
  const rows = [...value.groups, ...(value.unknown_mapping ? [value.unknown_mapping] : [])];
  return <details className="offline-details" aria-label="名册滞后行业观察"><summary>滞后大行业背景 · {available ? `${value.mapping?.snapshot_date || '日期待核验'} 映射` : '行业资料待核验'}</summary>
    {available ? <>
      <p className="subtle">映射截至 {value.mapping?.snapshot_date}，实际可得 {value.mapping?.known_at}；相对名册日滞后 {count(value.mapping?.snapshot_staleness_calendar_days_at_source)} 天，相对本次读取日滞后 {count(value.mapping?.snapshot_staleness_calendar_days_at_read)} 天。已映射 {count(value.mapped_target_count)} / {count(value.member_count)}，映射未知 {count(value.unknown_mapping_count)}。</p>
      <p className="subtle">{current ? '以下使用本次读取时仍有效的价格观察。' : '以下价格观察已经失效，仅供历史查看。'}行业按保存的分类和名称固定顺序展示，不是涨幅榜。旧大行业不等于当天题材，也不证明当前资格、资金净流入或资金意图。</p>
      <div className="table-scroll"><table className="compact" aria-label="前日名册滞后行业分布"><thead><tr><th>分类 / 行业</th><th>完整人数</th><th>原590内人数</th><th>报价已知 / 未知</th><th>上涨 / 下跌 / 平盘</th><th>已知条件中位涨跌</th><th>全行业广度</th><th>保守广度区间</th></tr></thead><tbody>{rows.map((row, index) => {
        const summary = current ? row.current_summary : row.observation_summary;
        return <tr key={`${row.industryClassification}-${row.industry}-${index}`}><td>{row.industry ? `${row.industryClassification || '分类待核验'} / ${row.industry}` : '行业映射未知，保留独立分母'}</td><td>{count(row.member_count)}</td><td>{count(row.fixed_590_count)}</td><td>{summary ? `${count(summary.known_count)} / ${count(summary.unknown_count)}` : '待核验'}</td><td>{summary ? `${count(summary.advance_count)} / ${count(summary.decline_count)} / ${count(summary.flat_count)}` : '待核验'}</td><td>{percent(summary?.conditional_median_change_percent)}</td><td>{breadth(summary?.full_cohort_breadth)}</td><td>{bounds(summary?.breadth_bounds)}</td></tr>;
      })}</tbody></table></div>
    </> : <p role="status">行业映射本次不可用，价格观察独立保留，不用其他行业日期补作当天资料。</p>}
  </details>;
}

export function MarketRosterIntraday({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<View | null>(null), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setError('');
    api<View>('/trading/research/market-roster-intraday', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } });
    return () => controller.abort();
  }, [refreshVersion]);
  const current = view?.status === 'current_prior_dated_roster_snapshot' ? view.current_summary : null;
  const historical = view?.status === 'saved_prior_dated_roster_snapshot_not_current' ? view.observation_summary : null;
  const summary = current || historical;
  return <details className="offline-details" open aria-label="前日沪深名册盘中观察">
    <summary>前日沪深名册盘中观察 · {view ? statuses[view.status] || '状态待核验' : error ? '读取失败' : '读取中'}</summary>
    <p className="subtle">以此前完整沪深名册观察较广价格分布，尚非已核验的当日全 A 股。今日新股、移除、ST 和交易资格仍未知；前日背景标签不作为当前可买资格或筛选条件。</p>
    {error && <p role="status">名册观察读取失败：{error}。不回退到旧成功结果，固定590等其他研究卡独立读取。</p>}
    {!view && !error && <p role="status">正在读取保存回执，不启动行情采集。</p>}
    {view && <>
      <p className="subtle">名册来源日 {view.source_day || '待核验'}，保留 {count(view.target_count)} 名；报价观察日 {view.observation_day || '待核验'}。本次页面读取 {view.read_at}。</p>
      <p className="subtle">每份回执来自一次分批采集，刷新页面只读取最近回执，采集频率不代表实时性。每30秒只读回执；仅同一交易时段、源时点在120秒内且有效的报价计为当前，过期后只保留历史统计。分批报价不是同一瞬间的全市场快照。</p>
      {view.status === 'waiting_for_first_market_roster_snapshot' && <p role="status">尚无完整名册报价回执，没有当前或历史统计。</p>}
      {view.status === 'collecting_snapshot' && <p role="status">本次采集尚未完整，当前不展示部分名册涨跌统计。</p>}
      {view.status.startsWith('unavailable_') && <p role="status">本次名册观察不可用：{view.error || statuses[view.status] || '等待完整、有效的保存证据'}。没有可用的当前或历史汇总，不采用旧成功快照替代。</p>}
      {view.snapshot && <div className="window-summary"><span>实际开始 <strong>{view.snapshot.started_at}</strong></span><span>实际结束 <strong>{view.snapshot.finished_at || '尚未结束'}</strong></span><span>已完成分批 <strong>{count(view.snapshot.completed_batches)} / {count(view.snapshot.planned_batches)}</strong></span><span>报价请求 <strong>{count(view.snapshot.quote_request_count)}</strong></span></div>}
      <MarketRosterRecentChange change={view.recent_change} memberCount={view.target_count} />
      {summary && <>
        <div aria-label={current ? '读取时前日名册统计' : '历史失效名册统计'}><p><strong>{current ? '本次读取时仍有效的名册子集' : '保存轮结束时的历史统计，已不代表当前'}</strong></p><SummaryView value={summary} /></div>
        {view.source_quote_clocks && <p className="subtle">{current ? '本次读取时有效报价源时点：' : '保存轮结束时有效报价源时点：'}{clock(current ? view.source_quote_clocks.regular_current_at_read : view.source_quote_clocks.regular_known_at_final_response)}。</p>}
        {current && view.observation_summary && <details className="offline-details"><summary>保存轮结束时统计 · 不代表当前</summary><SummaryView value={view.observation_summary} /></details>}
        <details className="offline-details"><summary>固定590、其余名册与前日 ST 背景 · {view.groups.length} 组</summary>
          <p className="subtle">590与其余成员按名册关系分组；非ST、ST及正常交易均是前一交易日背景，和前两组重叠，人数不能相加。未知成员保留在原组，空组没有中位数。</p>
          <div className="table-scroll"><table className="compact" aria-label="前日名册背景分组"><thead><tr><th>固定背景分组</th><th>完整人数</th><th>已知 / 未知</th><th>上涨 / 下跌 / 平盘</th><th>已知条件中位涨跌</th><th>全组广度</th><th>保守广度区间</th></tr></thead><tbody>{view.groups.map(group => {
            const value = current ? group.current_summary : group.observation_summary;
            return <tr key={group.id}><td>{group.name}</td><td>{count(group.member_count)}</td><td>{value ? `${count(value.known_count)} / ${count(value.unknown_count)}` : '待核验'}</td><td>{value ? `${count(value.advance_count)} / ${count(value.decline_count)} / ${count(value.flat_count)}` : '待核验'}</td><td>{percent(value?.conditional_median_change_percent)}</td><td>{breadth(value?.full_cohort_breadth)}</td><td>{bounds(value?.breadth_bounds)}</td></tr>;
          })}</tbody></table></div>
        </details>
        {view.industry_context && <IndustryView value={view.industry_context} current={!!current} />}
      </>}
      <MarketRosterActivity value={summary ? view.activity_context : undefined} current={!!current} mode="cumulative" />
      <details className="offline-details"><summary>名册来源与实际采集时间</summary>
        <p className="subtle">名册来源实际可得 {view.source_available_at || '待核验'}；本次目标名单记录 {view.targets_created_at || '待核验'}；批次响应接收 {view.snapshot?.response_received_at || '待核验'}。</p>
        {view.source_quote_clocks && <p className="subtle">所有可解析报价源时点（含陈旧或非正常报价）：{clock(view.source_quote_clocks.all_parseable_including_stale_or_nonregular)}。此范围不是当前有效报价覆盖。</p>}
        {view.fixed_590_registry && <p className="subtle">原登记集合 {count(view.fixed_590_registry.member_count)} 名，在本名册内 {count(view.fixed_590_registry.present_in_prior_roster_count)} 名；不在本名册内 {count(view.fixed_590_registry.absent_from_prior_roster_symbols.length)} 名。原登记记录不变。</p>}
        {view.collector && <p className="subtle">{view.collector.process.status === 'verified_alive' ? '采集进程身份已核实存活' : view.collector.process.status === 'not_running' ? '本次采集进程已不运行' : '采集进程身份待核验'}；进程状态与保存报价时效分别观察。</p>}
        <p className="mono subtle" style={{ overflowWrap: 'anywhere' }}>{view.source_file || '尚无保存回执'}<br />{view.targets_file || '尚无保存目标名单'}</p>
      </details>
    </>}
    <p className="subtle">这里只描述价格分布，不识别资金净流入或资金意图，不生成交易目标、订单或同花顺预填。页面读取不启动采集；selection_enabled=false · execution_admitted=false。</p>
  </details>;
}
