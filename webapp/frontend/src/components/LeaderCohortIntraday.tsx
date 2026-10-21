import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';
import { LeaderCohortRecentChange, type CohortRecentChange } from './LeaderCohortRecentChange';

type Summary = { member_count: number; known_count: number; unknown_count: number;
  advance_count: number; decline_count: number; flat_count: number;
  conditional_median_change_percent: number | null; full_cohort_breadth: number | null;
  conditional_known_breadth: number | null; breadth_bounds: [number, number] | null };
type Group = { id: string; name: string; member_count: number;
  current_summary: Summary | null; observation_summary: Summary | null };
type Quote = { symbol: string; registered_name: string; source_name: string | null; frozen_labels: string[];
  source_quote_time: string | null; age_seconds_at_read: number | null; current_at_read: boolean;
  read_status: string; day_change_percent: number | null; last_price_rmb: string | null;
  name_changed_from_registry: boolean | null; status: string };
type View = { status: string; read_at: string; source_file: string | null;
  registry: { registered_at: string; member_count: number; source_day: string } | null;
  session: { session_day: string; session_name: string; session_start: string; stop_at: string } | null;
  collector: { recorded_status: string; attempts: number; successes: number; process: { status: string }; stop_at: string } | null;
  snapshot: { started_at: string; finished_at: string; response_received_at: string | null; attempt_status: string;
    planned_batches: number; completed_batches: number; quote_request_count: number } | null;
  current_summary: Summary | null; observation_summary: Summary | null; groups: Group[]; quotes: Quote[];
  recent_change?: CohortRecentChange | null; error?: string };
const names: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙',
  trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙', cross_regime_candidate: '穿越龙候选',
  rebound_repair_candidate: '反包龙候选', sector_catchup_leader: '补涨龙', turnover_leader: '换手龙' };
const states: Record<string, string> = { waiting_for_first_cohort_observation: '等待首轮固定集合观察',
  current_fixed_cohort_observation: '读取时有本时段有效报价', saved_cohort_observation_not_current: '历史观察，已不代表当前',
  unavailable_latest_cohort_attempt: '最新采集轮失败，本次不可用' };
const readStates: Record<string, string> = { current: '读取时有效', stale: '已陈旧', outside_current_slot: '不在当前交易时段',
  future_source_time: '源时间在未来，待核验', suspended_or_nonregular: '停牌或非正常报价，待核验',
  missing_or_invalid: '缺失或无效，待核验', latest_attempt_failed: '最新轮失败，待核验' };
const collectorStates: Record<string, string> = { running: '采集中', completed_single_observation: '单轮完成',
  completed_max_observations: '已达本次批数上限', completed_session_boundary: '本时段结束',
  failed_stopped_no_retry: '失败，已停止', blocked_outside_trading_session: '不在采集时段' };
const count = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : fmt(value, 0);
const percent = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 2)}%`;
const breadth = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : percent(value * 100);
const bounds = (value?: [number, number] | null) => value ? `${breadth(value[0])} 至 ${breadth(value[1])}` : '待核验';

function CohortSummary({ value }: { value: Summary }) {
  return <>
    <div className="window-summary"><span>完整分母 <strong>{count(value.member_count)}</strong></span><span>已知 / 未知 <strong>{count(value.known_count)} / {count(value.unknown_count)}</strong></span><span>已知上涨 / 下跌 / 平盘 <strong>{count(value.advance_count)} / {count(value.decline_count)} / {count(value.flat_count)}</strong></span><span>已知样本条件中位涨跌 <strong>{percent(value.conditional_median_change_percent)}</strong></span></div>
    <p className="subtle">全体广度 {breadth(value.full_cohort_breadth)}；已知子集条件广度 {breadth(value.conditional_known_breadth)}；含未知成员的保守广度区间 {bounds(value.breadth_bounds)}。条件统计只描述已知子集，缺报价不视为平盘，也不移出完整分母。</p>
  </>;
}

export function LeaderCohortIntraday({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<View | null>(null), [error, setError] = useState('');
  const [filter, setFilter] = useState('all'), [page, setPage] = useState(1);
  useEffect(() => {
    const controller = new AbortController(); setError('');
    api<View>('/trading/research/leader-cohort-intraday', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } });
    return () => controller.abort();
  }, [refreshVersion]);
  const failed = view?.status === 'unavailable_latest_cohort_attempt';
  const current = view?.status === 'current_fixed_cohort_observation' && !failed ? view.current_summary : null;
  const observed = !failed ? view?.observation_summary : null;
  const summary = current || observed;
  const quotes = (view?.quotes || []).filter(quote => filter === 'all' || quote.frozen_labels.includes(filter));
  const pages = Math.max(1, Math.ceil(quotes.length / 20)), shownPage = Math.min(page, pages);
  const shown = quotes.slice((shownPage - 1) * 20, shownPage * 20);
  return <details className="offline-details" open aria-label="固定候选盘中观察">
    <summary>固定候选盘中观察 · {view ? states[view.status] || '状态待核验' : error ? '读取失败' : '读取中'}</summary>
    <p className="subtle">只跟踪已事前登记的固定候选集合，不代表全 A 股，也不是当前选股或买卖信号。报价与收盘龙型证据分别观察，不能识别资金净流入、参与者意图或盘口承接。</p>
    {error && <p role="status">固定候选观察读取失败：{error}。不采用旧成功结果替代，其他研究卡独立读取。</p>}
    {!view && !error && <p role="status">正在读取保存的观察回执，不启动采集。</p>}
    {view && <>
      {view.registry && <p className="subtle">固定全体 {count(view.registry.member_count)} 名成员；实际注册 {view.registry.registered_at}，身份来源收盘 {view.registry.source_day}。多标签来自登记时规则，盘中报价不改变原标签。</p>}
      <p className="subtle">本次页面读取 {view.read_at}；{view.session ? `观察时段 ${view.session.session_day} ${view.session.session_name === 'morning' ? '上午' : view.session.session_name === 'afternoon' ? '下午' : view.session.session_name}，${view.session.session_start} 至 ${view.session.stop_at}` : '当前没有可核验的采集时段'}。每30秒随父页面读取回执，刷新频率不代表报价实时性。</p>
      {view.status === 'waiting_for_first_cohort_observation' && <p role="status">尚无首轮观察回执；仅展示固定注册信息，没有盘中统计或报价。</p>}
      {failed && <p role="status">最新一轮采集失败：{view.error || '本次没有可用报价证据'}。本次汇总不可用，不回退到旧成功观察。</p>}
      {summary && <div aria-label={current ? '读取时固定集合统计' : '历史失效集合统计'}><p><strong>{current ? '本次读取时的有效样本' : '保存轮结束时的历史统计，现已失效'}</strong></p><CohortSummary value={summary} /></div>}
      {current && observed && <details className="offline-details"><summary>保存轮结束时统计 · 不代表当前</summary><CohortSummary value={observed} /></details>}
      <LeaderCohortRecentChange change={view.recent_change} memberCount={view.registry?.member_count} />
      {view.groups.length > 0 && summary && <details className="offline-details"><summary>原龙型分组覆盖 · {view.groups.length} 类</summary>
        <p className="subtle">{current ? '本次读取时有效报价的分组统计。' : '历史统计已失效，不代表当前。'}成员可以有多个标签，各组人数不能相加作为总人数。空组表示登记时没有已知匹配，不代表市场不存在该类龙股；空组没有中位数。</p>
        <div className="table-scroll"><table className="compact" aria-label="固定候选分组盘中覆盖"><thead><tr><th>登记龙型</th><th>全组人数</th><th>已知 / 未知</th><th>上涨 / 下跌 / 平盘</th><th>已知条件中位涨跌</th><th>全组广度</th><th>保守广度区间</th></tr></thead><tbody>{view.groups.map(group => {
          const value = current ? group.current_summary : group.observation_summary;
          return <tr key={group.id}><td>{names[group.id] || group.name}</td><td>{count(group.member_count)}</td><td>{value ? `${count(value.known_count)} / ${count(value.unknown_count)}` : '待核验'}</td><td>{value ? `${count(value.advance_count)} / ${count(value.decline_count)} / ${count(value.flat_count)}` : '待核验'}</td><td>{percent(value?.conditional_median_change_percent)}</td><td>{breadth(value?.full_cohort_breadth)}</td><td>{bounds(value?.breadth_bounds)}</td></tr>;
        })}</tbody></table></div>
      </details>}
      {view.quotes.length > 0 && <details className="offline-details"><summary>固定成员逐项报价 · 保留 {count(view.quotes.length)} 名</summary>
        <p className="subtle">按接口的固定证券顺序展示，每页20项；过滤仅改变展示，不排名、不补位、不选优，也不改变集合和分组统计的完整分母。{!current && '以下保存报价均不代表当前。'}</p>
        <label className="inline-field">登记龙型筛选<select aria-label="固定盘中候选龙型筛选" value={filter} onChange={event => { setFilter(event.target.value); setPage(1); }}><option value="all">全体固定成员</option>{Object.entries(names).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
        <label className="inline-field">页码<select aria-label="固定盘中候选页码" value={shownPage} onChange={event => setPage(Number(event.target.value))}>{Array.from({ length: pages }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1} / {pages}</option>)}</select><span>本筛选 {count(quotes.length)} 名，展示 {quotes.length ? (shownPage - 1) * 20 + 1 : 0} 至 {Math.min(shownPage * 20, quotes.length)}</span></label>
        <div className="table-scroll"><table className="compact" aria-label="固定候选逐项盘中报价"><thead><tr><th>证券 / 登记名称</th><th>固定标签</th><th>保存价（元）</th><th>较昨收涨跌</th><th>行情源日期时点</th><th>本次读取时效</th></tr></thead><tbody>{shown.map(quote => <tr key={quote.symbol}><td>{quote.symbol}<small className="window-stale">{quote.registered_name || '登记名称未提供'}{quote.name_changed_from_registry ? `；源名称 ${quote.source_name || '未知'}，与登记名称不同` : ''}</small></td><td>{quote.frozen_labels.map(id => names[id] || id).join('、') || '无已知登记标签'}</td><td>{failed ? '待核验' : quote.last_price_rmb ?? '待核验'}</td><td>{failed ? '待核验' : percent(quote.day_change_percent)}</td><td>{failed ? '最新轮失败，无可用源时点' : quote.source_quote_time || '无有效源时点'}</td><td>{readStates[failed ? 'latest_attempt_failed' : quote.read_status] || '报价状态待核验'}{!failed && quote.age_seconds_at_read != null && quote.age_seconds_at_read >= 0 ? `；源时点距读取 ${count(quote.age_seconds_at_read)} 秒` : ''}</td></tr>)}</tbody></table></div>
        <p className="subtle">缺失、陈旧、源时间在未来或停牌及非正常报价都保留为未知；源名称变化不能据此替换固定成员。报价不证明账户可成交，流通盘、盘口及资金意图仍未核验。</p>
      </details>}
      <details className="offline-details"><summary>采集回执与实际时间</summary>
        {view.collector && <p className="subtle">保存任务状态 {collectorStates[view.collector.recorded_status] || view.collector.recorded_status}；成功 {count(view.collector.successes)} / 尝试 {count(view.collector.attempts)}；{view.collector.process.status === 'verified_alive' ? '进程身份已核实存活' : view.collector.process.status === 'not_running' ? '进程未运行' : '进程身份待核验'}。本时段边界 {view.collector.stop_at || '待核验'}，不据此承诺自动跨时段采集。</p>}
        {view.snapshot && <p className="subtle">保存轮开始 {view.snapshot.started_at}；结束 {view.snapshot.finished_at || '尚未结束'}；响应接收 {view.snapshot.response_received_at || '无接收证据'}。已完成分批 {count(view.snapshot.completed_batches)} / {count(view.snapshot.planned_batches)}，报价请求 {count(view.snapshot.quote_request_count)} 次。</p>}
        <p className="mono subtle" style={{ overflowWrap: 'anywhere' }}>{view.source_file || '尚无保存回执'}</p>
        <p className="subtle">页面只读回执，不创建注册、不启动行情采集、不生成订单。selection_enabled=false · execution_admitted=false。</p>
      </details>
    </>}
  </details>;
}
