import { useEffect, useState } from 'react';
import { RefreshCw, ShieldCheck } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage } from './Common';
import { fmt } from '../types';
import { LeaderIdentityTracking } from './LeaderIdentityTracking';
import { LeaderCohortIntraday } from './LeaderCohortIntraday';
import { MarketRosterIntraday } from './MarketRosterIntraday';
import { LeaderIntradayPathPanel } from './LeaderIntradayPathPanel';
import { LeaderTurnoverContext, type TurnoverContext } from './LeaderTurnoverContext';
import { LeaderEightTypePaper } from './LeaderEightTypePaper';
import { LeaderEightTypeHistory } from './LeaderEightTypeHistory';
import { NativePreopenStatus } from './NativePreopenStatus';

type Candidate = { symbol: string; name?: string; amount_cny: number; pct_chg: number; stage: number | null; ipo_feature_support?: boolean; evidence: Record<string, unknown>; invalidation: string[]; trade_ready: false };
type Archetype = { id: string; name: string; status: string; candidate_count: number; stored_candidate_count?: number; unknown_rebound_path_liquid_rows?: number | null; candidates: Candidate[]; missing_evidence: string[] };
type Sector = { industry: string; observed_names: number; breadth: number; median_return_pct: number; amount_share_of_observed_cross_section: number };
type RelayFeedback = { prior_cohort_count: number; known_outcome_count: number; unknown_outcome_count: number; continuation_count: number; continuation_rate: number | null; possible_denominator_range?: number[]; unknown_stage_membership_count?: number; continuation_rate_bounds?: number[] | null; known_stage_conditional_continuation_rate?: number | null };
type IndexChange = { status: string; session_day?: string; session_name?: string; current_hint: null | { index_proxy_positive_count: number; index_proxy_negative_count: number; index_proxy_flat_count: number }; indices: { symbol: string; name: string; start_source_quote_time: string; end_source_quote_time: string; window_span_seconds: number; window_return_bp: number; day_change_delta_pp: number }[] };
type View = { status: string; source_file: string | null; day?: string; created_at?: string; read_at: string; display_limit?: number;
  analysis: null | { freshness: string; market_environment: { phase: string; label: string; evidence: Record<string, number | null>; limit_and_ladder: { IPO_supported_limit_up_proxy_count: number; IPO_supported_limit_up_stage_unknown_count?: number; IPO_supported_limit_down_proxy_count: number; IPO_unsupported_feature_rows: number; same_board_max_known_stage: Record<string, number> }; change: { status: string; previous_day_required: string; breadth_change: number | null; amount_ratio_to_previous: number | null; unknown_stage_cohort?: { member_count: number }; relay_by_prior_stage: Record<string, RelayFeedback>; strong_stock_feedback: { prior_stage2plus_count?: number; negative_return_count?: number; mean_return_pct?: number | null; unknown_stage_membership_count?: number; conditional_known_stage_mean_return_pct?: number | null } } }; archetypes: Archetype[]; scenario_matrix: { market_state: string; current_match: boolean; preferred_archetypes: string[]; trigger: string; invalidations: string[] }[]; data_availability: Record<string, boolean>; limitations: string[] };
  input_previews?: { history_repair?: { repair_day: string; derived_at: string }; recent_source_view?: { derived_at: string; view_sessions: string[]; base_history_requested_end: string }; feature_qualification?: { method_id: string; decision_as_of: string } }[];
  intraday_index_observation?: { status: string; source_file: string | null; read_at: string; source_observed_at?: string; error?: string; observation_attempt_status?: string; recent_change?: IndexChange; collector?: { recorded_status?: string; attempts?: number; successes?: number; stop_at?: string; process: { status: string; pid?: number } }; quotes: { symbol: string; name: string; source_quote_time: string; age_seconds_at_read: number; last_index_points: number; change_percent: number; current_at_read: boolean }[] };
  sector_context?: { snapshot_date: string; known_at: string; snapshot_staleness_calendar_days: number; observed_mapping_name_coverage: number | null; unmapped_observed_names: number; sectors: Sector[] };
  data_observation?: { checked_at: string; intraday_probe?: { ended_at: string; status: string; fresh_quote_count: number | null }; index_model_context?: { source_dates: string[]; result: { label: string; score: number }; forecast_accuracy_unverified: boolean } };
  turnover_context?: TurnoverContext;
};

const names: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙', trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙', cross_regime_candidate: '穿越龙候选', rebound_repair_candidate: '反包龙候选', sector_catchup_leader: '补涨龙', turnover_leader: '换手龙' };
const definitions: Record<string, string> = { space_height_leader: '同板制已知连板梯队的局部高度', relay_continuation_leader: '已知连板延续，另查板块和次日可成交性', trend_leader: '趋势结构与持续强度，明确观察行历史缺口', liquidity_capacity_proxy: '成交额承载代理，另需流通盘、真实换手和承接', cross_regime_candidate: '先有已登记身份，再观察环境变化中的持续性', rebound_repair_candidate: '日线反包修复，另需盘中路径确认', sector_catchup_leader: '需要同题材先行龙与滞后启动关系', turnover_leader: '需要口径明确的换手率、流通盘及盘口' };
const statusNames: Record<string, string> = { matched: '匹配日线代理', not_matched: '已知证据未匹配', unknown: '证据不足' };
const collectorNames: Record<string, string> = { running: '采集中', completed_single_observation: '单次采集完成', completed_max_observations: '已达本次批数上限', completed_session_boundary: '本时段采集结束', failed_stopped_no_retry: '采集失败，已停止', blocked_outside_trading_session: '当前不在采集时段' };
const phaseNames: Record<string, string> = { broad_strength_snapshot: '普涨快照', broad_weakness_snapshot: '普跌快照', mixed_breadth_snapshot: '涨跌分化', unknown: '证据不足' };
const missingNames: Record<string, string> = { rebound_D_minus2_exact_reference_chain_unavailable: 'D−2准确价格参考链', rebound_D_minus1_exact_reference_chain_unavailable: 'D−1准确价格参考链', qualified_liquid_limit_up_peer_stage_unknown: '可比涨停标的梯队未知', pre_episode_frozen_leader_identity: '事前登记的龙股身份', at_least_three_adjacent_complete_closes: '连续完整收盘', defined_environment_episode: '明确环境切换', sector_relative_resilience: '板块相对抗跌', dated_sector_membership: '有日期的题材归属', prior_sector_leader_identity: '先行龙身份', sector_rotation_and_lag_evidence: '轮动和启动时差', dated_turnover_rate_or_free_float_shares: '换手率或流通股数', intraday_exchange_of_holders_and_queue_quality: '盘中换手与排队质量', exchange_PIT_peer_and_corporate_action_verification: '历史资格和公司行动', intraday_leadership_confirmation: '盘中带动性', next_open_clearance: '次日可成交性', intraday_queue_and_seal_quality: '封板与排队质量', dated_sector_cohesion: '题材共振', calendar_session_20_60_window_completeness: '20/60交易日完整性', dated_sector_relative_strength: '板块相对强弱', dated_free_float_market_cap: '流通市值', turnover_rate: '换手率', intraday_absorption: '盘中承接', intraday_break_and_reseal_path: '炸板回封路径', dated_sector_repair_confirmation: '板块修复确认' };
const evidenceLabels: Record<string, string> = { known_stage: '已知连板阶段', board_observed_max_stage: '同板制已知最高阶段', board_unknown_stage_rows: '同板制阶段未知数', sma20: '20观察行均价', sma60: '60观察行均价', ret20: '20观察行收益', ret60: '60观察行收益', amount_rank_pct: '成交额分位', daily_rebound: '日线修复', calendar_contiguous_latest_pair: '最近两日连续' };
const number = (value?: number | null, digits = 0) => value == null ? '待核验' : fmt(value, digits);
const pct = (value?: number | null) => value == null ? '待核验' : `${fmt(value * 100, 2)}%`;
const points = (value?: number | null) => value == null ? '待核验' : `${fmt(value * 100, 2)} 个百分点`;
function evidenceText(evidence: Record<string, unknown>) {
  return Object.entries(evidence).filter(([key]) => evidenceLabels[key]).map(([key, value]) => `${evidenceLabels[key]}：${typeof value === 'boolean' ? value ? '是' : '否' : value == null ? '未知' : typeof value === 'number' ? ['ret20', 'ret60', 'amount_rank_pct'].includes(key) ? pct(value) : number(value, key.startsWith('sma') ? 2 : 0) : String(value)}`).join('；');
}

function RecentIndexChange({ change }: { change?: IndexChange }) {
  if (!change) return null;
  const current = change.status === 'current_same_slot_index_change' && change.current_hint;
  return <details className="offline-details" open><summary>三指数近五分钟变化 · {current ? '当前时段观察' : change.indices.length ? '历史观察，已不代表当前' : '窗口待核验'}</summary>
    {change.indices.length > 0 ? <>
      <p className="subtle">{change.session_day} {change.session_name === 'morning' ? '上午' : '下午'}，同一时段的保存行情。{current ? `短时上涨 ${current.index_proxy_positive_count} / 3，下跌 ${current.index_proxy_negative_count} / 3。` : '最新行情已陈旧或不在原采集时段，仅保留历史区间。'}近五分钟变化与当日涨跌分别观察。</p>
      <div className="table-scroll"><table className="compact" aria-label="三指数近五分钟变化"><thead><tr><th>指数</th><th>行情源时点区间</th><th>实际跨度</th><th>区间涨跌</th><th>较昨收涨幅变化</th></tr></thead><tbody>{change.indices.map(row => <tr key={row.symbol}><td>{row.name}</td><td>{row.start_source_quote_time.slice(11, 19)} 至 {row.end_source_quote_time.slice(11, 19)}</td><td>{number(row.window_span_seconds / 60, 1)} 分钟</td><td>{number(row.window_return_bp / 100, 3)}%</td><td>{number(row.day_change_delta_pp, 3)} 个百分点</td></tr>)}</tbody></table></div>
    </> : <p className="subtle">同一时段尚无可核验的完整近五分钟窗口。最新采集失败、时点不一致或资料缺失时保持未知。</p>}
    <p className="subtle">只比较上证、创业板和科创50；不代表全市场广度、题材扩散或龙股承接，不自动切换策略。区间采用实际源时点，不跨午休、不插值。</p>
  </details>;
}

export function LeaderEnvironmentWorkbench() {
  const [view, setView] = useState<View | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0), [filter, setFilter] = useState('all');
  useEffect(() => {
    const controller = new AbortController(); setBusy(true); setError('');
    api<View>('/trading/research/leader-environment', { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setView(value); }).catch(e => { if (!controller.signal.aborted) { setView(null); setError(e.message); } }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [version]);
  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden) setVersion(value => value + 1); }, 30000);
    return () => window.clearInterval(timer);
  }, []);
  const analysis = view?.analysis, environment = analysis?.market_environment, evidence = environment?.evidence;
  const archetypes = analysis?.archetypes || [];
  const visible = archetypes.filter(type => filter === 'all' || type.id === filter);
  const candidates = visible.flatMap(type => type.candidates.map(candidate => ({ ...candidate, type: type.id })));
  const sector = view?.sector_context, observation = view?.data_observation;
  const recentView = view?.input_previews?.find(item => item.recent_source_view)?.recent_source_view;
  const featureProof = view?.input_previews?.find(item => item.feature_qualification?.decision_as_of.slice(0, 10) === view?.day)?.feature_qualification;
  return <section className="trade-section leader-environment" aria-label="龙头战法与市场环境">
    <div className="research-heading"><div><h2>龙头战法 · 市场环境</h2><p className="subtle">先看环境与板块，再看龙型、触发和失效；同一股票可有多个标签。</p></div><button aria-label="刷新龙头环境" onClick={() => setVersion(value => value + 1)} disabled={busy}><RefreshCw size={15} />{busy ? '读取中' : '刷新分析'}</button></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>研究描述</strong><span>环境适配待验证</span><span>执行准入关闭</span></div>
    <LeaderEightTypePaper refreshVersion={version} />
    <LeaderEightTypeHistory />
    <NativePreopenStatus refreshVersion={version} />
    <LeaderIdentityTracking refreshVersion={version} />
    <MarketRosterIntraday refreshVersion={version} />
    <LeaderCohortIntraday refreshVersion={version} />
    <LeaderIntradayPathPanel refreshVersion={version} />
    <LeaderTurnoverContext day={view?.day} context={view?.turnover_context} />
    {error && <ErrorMessage message={error} />}
    {!analysis && <p role="status">等待完整收盘分析。八类龙型的定义已建立，尚未据当前数据确认候选。</p>}
    {analysis && <>
      <div className="leader-clock"><strong>行情收盘日 {view.day}</strong><span>分析记录 {view.created_at}</span><span>页面读取 {view.read_at}</span></div>
      {view.input_previews?.filter(item => item.history_repair).map((item, index) => <p className="subtle" key={index}>历史特征采用混合来源：仅补 {item.history_repair?.repair_day} 整日缺口，实际重建 {item.history_repair?.derived_at}；旧快照与分数保留，其他个股缺口和历史资格仍未验证。</p>)}
      {recentView && <p className="subtle">近期完整来源视图：原历史使用至 {recentView.base_history_requested_end}，其后采用 {recentView.view_sessions.join('、')} 各自日期的完整独立行情；实际重建 {recentView.derived_at}。原快照、分数和旧结果保留，历史资格与公司行动仍待核验。</p>}
      {featureProof && <p className="subtle">IPO特征与固定生产规则共用方法 <span className="mono">{featureProof.method_id}</span>；本行决策截点 {featureProof.decision_as_of}。历史资格仅使用截至本行截点可见的证据，后继日资料不回填较早日；未知成员仍保留为未知。</p>}
      <p className="window-stale">这是已收盘快照，全市场盘中变化未核验；页面每30秒读取新分析，刷新频率不代表行情实时性。</p>
      {observation?.intraday_probe && <p className="subtle">本回执的全ETF盘中获取：{observation.intraday_probe.ended_at} · {observation.intraday_probe.status === 'unavailable_HTTP502' ? '接口 HTTP 502，未取得本次报价' : observation.intraday_probe.status}。旧缓存不补作本次报价。</p>}
      {view.intraday_index_observation && <details className="offline-details" open><summary>盘中指数独立观察 · 固定三指数</summary>{view.intraday_index_observation.collector && <p className="subtle">采集任务回执 {collectorNames[view.intraday_index_observation.collector.recorded_status || ''] || '状态待核验'} · 成功 {number(view.intraday_index_observation.collector.successes)} / 尝试 {number(view.intraday_index_observation.collector.attempts)}；{view.intraday_index_observation.collector.process.status === 'verified_alive' ? '进程已核实存活' : view.intraday_index_observation.collector.process.status === 'not_running' ? '进程未运行' : '进程身份待核实'}。本时段采集边界 {view.intraday_index_observation.collector.stop_at || '待确认'}；刷新页面只读回执。</p>}<p className="subtle">原请求接收 {view.intraday_index_observation.source_observed_at || '无可读回执'}；本次读取 {view.intraday_index_observation.read_at}。仅反映各自行情源时点，不代替全市场广度、龙股盘口或账户可成交报价，不用于14日收盘信号。</p>{view.intraday_index_observation.quotes.length > 0 ? <div className="table-scroll"><table className="compact" aria-label="独立指数时点观察"><thead><tr><th>指数</th><th>点位</th><th>较昨收</th><th>行情源时间</th><th>读取时效</th></tr></thead><tbody>{view.intraday_index_observation.quotes.map(row => <tr key={row.symbol}><td>{row.name}</td><td>{number(row.last_index_points, 2)}</td><td>{number(row.change_percent, 2)}%</td><td>{row.source_quote_time}</td><td>{row.current_at_read ? '读取时在120秒窗口' : row.age_seconds_at_read < 0 ? '源时间在未来，待核验' : `已陈旧 ${number(row.age_seconds_at_read / 60, 1)} 分钟`}</td></tr>)}</tbody></table></div> : <p className="subtle">暂无有效的三指数观察：{view.intraday_index_observation.observation_attempt_status || view.intraday_index_observation.status}。{view.intraday_index_observation.error || ''}</p>}</details>}
      <RecentIndexChange change={view.intraday_index_observation?.recent_change} />
      <div className="metrics trade-metrics leader-market-metrics"><div><span>全样本收盘分布</span><strong>{environment?.label}</strong></div><div><span>上涨 / 下跌</span><strong>{number(evidence?.advance_count)} / {number(evidence?.decline_count)}</strong></div><div><span>涨跌广度</span><strong>{pct(evidence?.breadth)}</strong></div><div><span>成交额</span><strong>{evidence?.amount_cny == null ? '待核验' : `${number(evidence.amount_cny / 1e8, 1)}亿`}</strong></div><div><span>前20成交额集中度</span><strong>{pct(evidence?.top20_turnover_share)}</strong></div><div><span>IPO支持的涨停 / 跌停代理</span><strong>{number(environment?.limit_and_ladder.IPO_supported_limit_up_proxy_count)} / {number(environment?.limit_and_ladder.IPO_supported_limit_down_proxy_count)}</strong></div></div>
      {observation?.index_model_context && <p className="subtle">指数/ETF旧模型观察（输入收盘日 {observation.index_model_context.source_dates.join('、')}）：{observation.index_model_context.result.label}，分值 {number(observation.index_model_context.result.score, 3)}。与全样本广度分别展示，预测能力仍待核验。</p>}
      <p className="subtle">涨停代理中连板阶段未知 {number(environment?.limit_and_ladder.IPO_supported_limit_up_stage_unknown_count)} 只；缺历史证据时不能把零个已知匹配解读为不存在空间龙或接力龙。</p>
      <p className="subtle">单日分布不能确认情绪周期。量价关联不是主力净流入；IPO支持的价格代理也不等于封板队列可成交。</p>
    </>}
    <div className="table-scroll"><table className="compact leader-type-table" aria-label="龙型证据分类"><thead><tr><th>龙型</th><th>内部研究定义</th><th>当前证据</th><th>匹配数</th><th>仍需确认</th></tr></thead><tbody>{Object.entries(names).map(([id, name]) => { const type = archetypes.find(row => row.id === id); return <tr key={id}><td><strong>{name}</strong></td><td>{definitions[id]}</td><td>{type ? statusNames[type.status] || type.status : '等待完整分析'}{!!type?.unknown_rebound_path_liquid_rows && <small className="window-stale">{type.unknown_rebound_path_liquid_rows} 条流动性样本路径待核</small>}</td><td>{type?.status === 'unknown' || !type ? '未知' : number(type.candidate_count)}</td><td>{type?.missing_evidence.map(item => missingNames[item] || (item.startsWith('feature_field:') ? '原始特征缺失' : item)).join('、') || '等待来源核对'}</td></tr>; })}</tbody></table></div>
    {analysis && <>
      <details className="offline-details"><summary>候选证据与失效条件 · 多标签研究集合</summary>
        <label className="inline-field">龙型<select aria-label="龙型候选筛选" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部类型</option>{Object.entries(names).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
        <p className="subtle">每类最多展示 {view.display_limit || 10} 条，按成交额排序；完整匹配数量保留。展示上限不是组合选择，不表示交易优先级；容量集合包含上市初期的纯量额观测，IPO标记不等于历史资格或公司行动已验证。</p>
        <div className="table-scroll"><table className="compact leader-candidate-table" aria-label="龙型候选证据"><thead><tr><th>龙型</th><th>证券</th><th>日涨幅</th><th>成交额</th><th>阶段</th><th>分类证据</th><th>失效与待确认</th></tr></thead><tbody>{candidates.map(row => <tr key={`${row.type}-${row.symbol}`}><td>{names[row.type]}</td><td>{row.name || row.symbol}<small className="window-stale">{row.symbol}</small>{row.ipo_feature_support === false && <small className="window-stale">IPO价格窗口未支持，仅量额观察</small>}</td><td>{number(row.pct_chg, 2)}%</td><td>{number(row.amount_cny / 1e8, 2)}亿</td><td>{row.stage == null ? '未知' : row.stage}</td><td>{evidenceText(row.evidence)}</td><td>{row.invalidation.join('；')}</td></tr>)}</tbody></table></div>{candidates.length === 0 && <p>当前所选类型没有可展示的已知证据匹配，不能据此推断其他未知类型不存在。</p>}
      </details>
      <details className="offline-details" open><summary>环境变化与待验证的适配关系</summary><div className="table-scroll"><table className="compact leader-scenario-table" aria-label="龙型环境适配"><thead><tr><th>收盘环境</th><th>研究类型</th><th>进入研究前的确认</th><th>失效条件</th></tr></thead><tbody>{analysis.scenario_matrix.map(row => <tr key={row.market_state}><td>{phaseNames[row.market_state] || row.market_state}{row.current_match && <small className="window-stale">本次快照匹配</small>}</td><td>{row.preferred_archetypes.map(id => names[id] || id).join('、')}</td><td>{row.trigger}</td><td>{row.invalidations.join('；')}</td></tr>)}</tbody></table></div><p className="subtle">以上关系是固定待验证假设；退潮、分歧和修复尚需多日及盘中证据，未自动路由交易。</p></details>
      <details className="offline-details"><summary>接力反馈 · 分母保留缺失标的</summary><p>要求准确前一交易日 {environment?.change.previous_day_required} 的完整来源。{environment?.change.status === 'unknown' ? '当前缺相邻完整证据，不能计算晋级率、量能变化或强势股亏钱反馈。' : `广度变化 ${points(environment?.change.breadth_change)}；成交额比 ${number(environment?.change.amount_ratio_to_previous, 2)}。`}</p><p className="subtle">前日涨停但阶段未知 {number(environment?.change.unknown_stage_cohort?.member_count)} 只，共用未分配集合；这些标的可能属于各阶段，存在时不显示全体精确晋级率。</p><div className="table-scroll"><table className="compact" aria-label="接力晋级反馈"><thead><tr><th>前一阶段</th><th>已知阶段人数</th><th>结局已知 / 未知</th><th>可能分母范围</th><th>延续数</th><th>延续率 / 保守范围</th></tr></thead><tbody>{Object.entries(environment?.change.relay_by_prior_stage || {}).map(([id, row]) => <tr key={id}><td>{id}</td><td>{row.prior_cohort_count}</td><td>{row.known_outcome_count} / {row.unknown_outcome_count}</td><td>{row.possible_denominator_range?.map(value => number(value)).join(' 至 ') || '待核验'}</td><td>{row.continuation_count}</td><td>{pct(row.continuation_rate)}{row.continuation_rate == null && row.continuation_rate_bounds && <small className="window-stale">{row.continuation_rate_bounds.map(value => pct(value)).join(' 至 ')}</small>}{row.continuation_rate == null && row.known_stage_conditional_continuation_rate != null && <small className="window-stale">仅已知阶段条件值 {pct(row.known_stage_conditional_continuation_rate)}</small>}</td></tr>)}</tbody></table></div><p className="subtle">前日已知二板以上 {number(environment?.change.strong_stock_feedback.prior_stage2plus_count)} 只；已观察负收益 {number(environment?.change.strong_stock_feedback.negative_return_count)} 只；全体平均涨幅 {environment?.change.strong_stock_feedback.mean_return_pct == null ? '待核验' : `${number(environment.change.strong_stock_feedback.mean_return_pct, 2)}%`}。阶段归属或结局未知时，不能用已知成员均值代表全体强股。</p></details>
    </>}
    {sector && <details className="offline-details"><summary>行业扩散代理 · 滞后大行业映射</summary><p className="subtle">映射日期 {sector.snapshot_date}，实际采集 {sector.known_at}，距行情日 {sector.snapshot_staleness_calendar_days} 个自然日；覆盖 {pct(sector.observed_mapping_name_coverage)}，未知 {sector.unmapped_observed_names} 只仍保留成交额分母。大行业不等于当天题材，历史修订未验证。</p><div className="table-scroll"><table className="compact" aria-label="滞后行业扩散"><thead><tr><th>行业</th><th>观察数</th><th>涨跌广度</th><th>中位涨幅</th><th>成交额占比</th></tr></thead><tbody>{sector.sectors.slice(0, 12).map(row => <tr key={row.industry}><td>{row.industry}</td><td>{row.observed_names}</td><td>{pct(row.breadth)}</td><td>{number(row.median_return_pct, 2)}%</td><td>{pct(row.amount_share_of_observed_cross_section)}</td></tr>)}</tbody></table></div><p className="subtle">仅按成交额展示前12个行业，不按涨幅挑强势版本。</p></details>}
    <details className="offline-details"><summary>分析来源和边界</summary><p className="mono subtle">{view?.source_file || '完整分析尚未生成'}</p><p className="subtle">穿越需要事前身份和连续环境证据；换手需要明确分母；补涨需要真实题材关系。当前所有候选均非交易指令，旧回测与40日固定窗口不变。</p></details>
  </section>;
}
