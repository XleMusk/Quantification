import { Fragment, useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';
import { LeaderEightTypePositions } from './LeaderEightTypePositions';

type SavedFields = Record<string, unknown>;
type PriceRelation = 'above' | 'equal' | 'below' | null;
type CandidateTracking = {
  symbol: string; name: string | null; plan_decision: string; target_weight: number | null;
  tracking: {
    quote_status: string; snapshot_price_cny: number | null; source_quote_time: string | null;
    observed_at: string | null; fresh_at_read: boolean | null;
    reference_status: 'matched' | 'changed_or_incompatible' | 'unknown';
    reference_close_cny: number | null; threshold_name: string | null; threshold_cny: number | null;
    snapshot_relation: PriceRelation; current_relation: PriceRelation; snapshot_low_relation: PriceRelation;
    close_confirmation: unknown; held_entry_anchor: unknown; trade_ready: false;
  };
};
type TrackingRow = {
  case_id: string; type: string; decision: string; target_weights: Record<string, number> | null;
  candidates: CandidateTracking[];
};
type TrackingView = {
  status: string; read_at: string | null; paper_source_file: string | null;
  source_close_day: string | null; execution_day: string | null; observation_asof: string | null;
  source_file: string | null; reason?: string; error?: string;
  summary?: { candidate_appearances: number; unique_candidates: number; observed_unique: number;
    current_unique: number; missing_unique: number; reference_changed_unique: number };
  rows: TrackingRow[];
};
type CandidateDetail = {
  symbol: string; score: number; name: string | null; source_day: string | null;
  board: string | null; stage: number | null; amount_cny: number | null;
  pct_chg: number | null; amount_rank_pct: number | null; evidence: SavedFields | null;
  invalidation: string[] | null; ipo_feature_support: boolean | null; identity_status: string | null;
  trade_ready: false; setup_reference: SavedFields | null; current_raw_issues: SavedFields[] | null;
  current_reference_evidence: SavedFields | null; IPO_evidence: SavedFields | null;
  checks_status: 'saved_entry_checks' | 'not_run_independent_exit' | 'unavailable_in_saved_plan';
};
type PaperRow = {
  case_id: string; type: string; guard: string; capacity: string; decision: string;
  environment_mode: string; reason: string; original_candidate_count: number;
  cap_candidate_count: number; unresolved_member_count: number;
  target_weights: Record<string, number> | null; target_cash_weight: number | null;
  entry_phases: string[]; required_evidence: string[]; risk_and_invalidation: string[];
  structural_invalidation_policy: string; missing_evidence: string[] | Record<string, number>;
  candidates: { symbol: string; score: number }[];
  candidate_details?: CandidateDetail[];
};
type PaperView = {
  status: 'saved_paper_plan' | 'late_context_only' | 'unavailable'; read_at: string;
  source_file: string | null; recorded_at: string | null; source_close_day: string | null;
  proposed_execution_day: string | null; execution_day_bound: string | null;
  next_open_deadline: string | null; execution_window_elapsed: boolean | null;
  case_count: number | null; input_manifest: string | null; protocol_file: string | null;
  environment: null | { day: string; phase: string; mode: string; reason: string;
    two_close_confirmation: boolean; exact_previous_present: boolean; previous_session_required: string | null };
  rows: PaperRow[];
  intraday_tracking?: TrackingView | null;
};
const typeNames: Record<string, string> = {
  space_height_leader: '空间龙', relay_continuation_leader: '接力龙', trend_leader: '趋势龙',
  liquidity_capacity_proxy: '容量龙代理', cross_regime_candidate: '穿越龙候选',
  rebound_repair_candidate: '反包修复候选', sector_catchup_leader: '补涨龙代理', turnover_leader: '换手龙代理',
};
const phaseNames: Record<string, string> = {
  broad_strength_snapshot: '普涨', mixed_breadth_snapshot: '分化', broad_weakness_snapshot: '普跌', unknown: '未知',
};
const reasonNames: Record<string, string> = {
  known_weakness_independent_next_open_risk_exit: '保存的收盘为普跌，固定研究规则转为空目标',
  confirmed_phase_not_applicable_to_fixed_type: '已确认的环境不适用该类型',
  fixed_original_rank_and_capacity_target: '按原固定排名计算未绑定权重',
  environment_unknown_or_unconfirmed: '环境未知或尚未连续确认',
  type_observation_unknown: '类型所需证据不完整',
  whole_type_membership_unknown_in_fixed_panel: '固定名单中仍有成员无法判定',
  whole_type_membership_evidence_missing: '固定名单的完整性证据缺失',
  type_empty_or_capacity_filtered_all_no_signal: '无匹配，或已被固定名单规模限制排除',
  selected_current_raw_unknown_or_ineligible: '候选的当日行情或状态待核',
  selected_current_reference_unknown_or_incompatible: '候选的准确前收参考链待核',
  selected_current_IPO_unknown_or_unsupported: '候选的上市窗口资格待核',
};
const exitNames: Record<string, string> = {
  qualified_limit_false_or_known_higher_same_board_qualified_stage: '已确认断板，或出现同板制更高的已知梯队',
  qualified_limit_false: '资格与参考链明确后，收盘不再满足封板代理',
  complete_close_sma20_sma60_and_close_lt_sma20_or_sma20_le_sma60: '趋势字段完整时，收盘低于 SMA20，或 SMA20 不再高于 SMA60',
  'amount_rank_pct_lt_0.99': '当日成交额分位低于固定的 99% 分位',
  close_lt_entry_exact_previous_session_open: '收盘低于入场决策前一交易日的固定开盘价',
  close_lt_entry_episode_structure_close: '收盘低于入场时冻结的环境段参考收盘价',
  close_lt_entry_setup_raw_low: '收盘低于入场决策日的固定启动低点',
};
const missingNames: Record<string, string> = {
  exchange_PIT_peer_and_corporate_action_verification: '历史资格、可比梯队和公司行动',
  intraday_leadership_confirmation: '盘中带动性', next_open_clearance: '次日可成交性',
  intraday_queue_and_seal_quality: '封板与排队质量', dated_sector_cohesion: '有日期的板块共振',
  calendar_session_20_60_window_completeness: '准确20/60交易日窗口', dated_sector_relative_strength: '有日期的板块相对强弱',
  dated_free_float_market_cap: '有日期的流通市值', turnover_rate: '换手率', intraday_absorption: '盘中承接',
  fixed_first_episode_three_closes_not_available: '首个固定环境段尚缺三个准确收盘',
  intraday_break_and_reseal_path: '盘中炸板与回封路径', dated_sector_repair_confirmation: '有日期的板块修复',
  rebound_D_minus1_exact_reference_chain_unavailable: '反包前一交易日的准确参考链',
  rebound_D_minus2_exact_reference_chain_unavailable: '反包前两交易日的准确参考链',
  own_dated_turnover_percent_batch_missing: '准确日期的百分比换手批次',
};
const missingText = (value: PaperRow['missing_evidence']) => Array.isArray(value)
  ? value.map(key => missingNames[key] || key).join('；')
  : Object.entries(value).map(([key, count]) => `${missingNames[key] || key}：${count}`).join('；');
const decisionText = (row: PaperRow) => row.decision === 'EXIT_ALL' ? '研究空目标，未生成卖单'
  : row.decision === 'NO_CHANGE' ? '未产生新目标' : '未绑定研究权重';
const reasonText = (reason: string) => reason.split(';').map(item => reasonNames[item] || item).join('；');
const percent = (value: number) => `${fmt(value * 100, 2)}%`;
const weightsText = (row: PaperRow) => row.target_weights == null ? '目标未知，保留原状；不能解释为空仓'
  : Object.keys(row.target_weights).length === 0 ? '研究证券权重为空；尚未绑定已持仓与可卖数量'
  : Object.entries(row.target_weights).map(([symbol, weight]) => `${symbol} ${percent(weight)}`).join('；');

const fieldNames: Record<string, string> = {
  board_observed_max_stage: '同板制已知最高梯队', board_unknown_stage_rows: '同板制梯队未知数',
  peer_scope: '可比范围', global_space_leader_verified: '全市场空间身份已核验', known_stage: '已知连续封板阶段',
  limit_up_proxy: '日线封板代理', sma20: '观察行 SMA20', sma60: '观察行 SMA60',
  ret20: '观察行20行涨幅', ret60: '观察行60行涨幅', window_semantics: '观察窗口口径',
  calendar_contiguous_latest_pair: '最近两收盘日历相邻', amount_rank_pct: '成交额分位', rank_cutoff: '原分位门槛',
  capacity_basis: '容量观察依据', float_market_cap_available: '流通市值已保存', daily_rebound: '日线反包代理',
  path_evidence_basis: '路径证据依据', definition: '保存定义', previous_open: '前一交易日开盘价',
  structure_close: '冻结环境段参考收盘价', industry: '保存行业', industry_legs: '行业比较区间',
  setup_low: '同日原始低点', structure_high: '同日原始高点', turn_percent: '报告换手率（%）',
  previous_turn_percent: '准确前一交易日报告换手率（%）', turnover_increased: '报告换手增加',
  status: '原检查状态', reason: '原记录原因', day: '检查源日期', source_day: '源日期', symbol: '证券代码',
  previous_day_required: '要求的准确前一交易日', preclose_to_previous_close_ratio_minus1: '前收参考与前日收盘比值差',
  absence_of_corporate_actions_proven: '已证明无公司行动', ipo_date: '保存上市日',
  decision_as_of: '资格判断时点', available_at: '资料实际可得时点', knowledge_status: '资料可得性状态',
  engine_block_reason: '原执行资格阻碍', ipo_window_feature_support: '上市窗口支持固定特征',
  ipo_feature_exclusion_reason: '上市窗口特征缺项', source_basis: '资料来源口径',
  listing_session_ordinal: '上市后准确交易日序数', listing_session_ordinal_lower_bound: '交易日序数下界',
  price_limit_pct: '保存涨跌幅限制（%）', no_price_limit_first_five: '首五日无涨跌幅限制',
  st_status_inferred: 'ST状态经过推断', historical_publication_archive_verified: '历史发布档案已核验',
  pit_verified: '历史当时可得性已核验', corporate_actions_verified: '公司行动已核验',
  paper_admitted: '纸面执行已准入', execution_admitted: '交易执行已准入',
};
const valueNames: Record<string, string> = {
  same_board_complete_provider_active_non_ST_not_exchange_PIT: '同板制、源名册中活跃非ST；交易所历史当时可得性未核验',
  daily_price_volume_proxy_not_verified_market_or_sector_leader: '日线量价代理，尚未核验市场或板块龙头身份',
  observed_daily_turnover_only: '已观察日成交额代理', valid_observed_rows_not_guaranteed_calendar_sessions: '有效观察行，不保证准确交易日窗口',
  exact_reference_chain_and_verified_shared_producer_own_decision_listing_policy: '准确参考链与原决策日上市窗口资格',
  'D-2_limit_proxy_D-1_negative_D_close_above_D-1_open_with_calendar_continuity': '前两日封板代理、前日阴线、当日收复前日开盘价；日历连续',
  consistent_proxy: '数值一致性代理', incompatible: '数值不一致', unknown: '未知',
  known_after_source_availability: '实际资料可得后已知',
  retrieval_time_provider_master_not_exchange_PIT: '按实际取回时点的资料；非交易所历史当时名册',
  not_evaluated_intraday_snapshot: '未评估收盘失效，仅为盘中快照',
};
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const priceText = (value: unknown) => finite(value) ? `${fmt(value, 4)}元` : '未保存，待核验';
function savedText(value: unknown): string {
  if (value == null) return '未保存 / 待核验';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return finite(value) ? fmt(value, 6) : '待核验';
  if (typeof value === 'string') return valueNames[value] || value;
  if (Array.isArray(value)) return value.length ? value.map(savedText).join('；') : '原记录为空';
  return Object.entries(value as SavedFields).map(([key, entry]) => `${fieldNames[key] || key}：${savedText(entry)}`).join('；');
}
function SavedEvidence({ value }: { value: SavedFields | null }) {
  if (value == null) return <p className="subtle">原方案未保存，待核验。</p>;
  const entries = Object.entries(value);
  if (!entries.length) return <p className="subtle">原记录为空，没有补入新证据。</p>;
  return <ul style={{ paddingLeft: 18, margin: '8px 0', overflowWrap: 'anywhere' }}>{entries.map(([key, entry]) =>
    <li key={key}>{fieldNames[key] || key}：{finite(entry) && ['ret20', 'ret60', 'amount_rank_pct', 'rank_cutoff'].includes(key)
      ? percent(entry) : savedText(entry)}</li>)}</ul>;
}
const relationText = (relation: PriceRelation) => relation === 'above' ? '高于观察线'
  : relation === 'equal' ? '恰好触线' : relation === 'below' ? '低于观察线' : '未能判断';
const thresholdNames: Record<string, string> = {
  saved_candidate_setup_low: '原候选启动低点', saved_sma20_observed_rows: '原方案观察行 SMA20',
  saved_exact_previous_open: '原方案准确前一交易日开盘价',
  requires_complete_rule_evidence: '需要原规则的完整证据，现价不能单独判定',
  saved_observed_sma20: '原方案观察行 SMA20', saved_sma20: '原方案观察行 SMA20',
  sma20_observed: '原方案观察行 SMA20', saved_setup_low: '原方案启动低点',
  setup_low_cny: '原方案启动低点', saved_previous_open: '原方案前一交易日开盘价',
  previous_open: '原方案前一交易日开盘价', saved_structure_close: '原方案环境段参考收盘价',
  structure_close: '原方案环境段参考收盘价',
};
function CandidateIntradayTracking({ row, symbol, candidate }: {
  row: PaperRow; symbol: string; candidate?: CandidateTracking;
}) {
  if (!candidate || candidate.plan_decision !== row.decision) return <section
    data-testid="leader-candidate-intraday" data-symbol={symbol} data-quote-status="not_connected">
    <strong>盘中风险观察</strong><p className="subtle">本票尚未连接同一原方案的报价观察；下方保存证据仍完整保留。</p>
  </section>;
  const observation = candidate.tracking;
  const matched = observation.reference_status === 'matched';
  const snapshotRelation = matched ? observation.snapshot_relation : null;
  const currentRelation = matched && observation.fresh_at_read === true ? observation.current_relation : null;
  const lowRelation = matched ? observation.snapshot_low_relation : null;
  const targetWeight = row.target_weights?.[symbol];
  return <section data-testid="leader-candidate-intraday" data-case-id={row.case_id}
    data-symbol={symbol} data-quote-status={observation.quote_status} style={{ margin: '12px 0' }}>
    <strong>盘中风险观察 · 原计划保持不变</strong>
    <p>原计划：{decisionText(row)}；本票研究权重：{row.decision === 'NO_CHANGE'
      ? '未知，保留原状' : finite(targetWeight) ? percent(targetWeight) : '原方案未保存本票目标'}。</p>
    <p>快照价 {priceText(observation.snapshot_price_cny)}；行情源时间 {observation.source_quote_time || '未保存'}；
      实际观察 {observation.observed_at || '未保存'}。</p>
    <p className={observation.fresh_at_read === true ? 'subtle' : 'window-stale'} role="status">
      {observation.fresh_at_read === true ? '页面读取时仍在有效期内；之后不会自动续期。'
        : observation.fresh_at_read === false ? '页面读取时已过期，只保留快照当时的关系，当前关系未知。'
          : '报价有效期未知，不能将快照当作当前报价。'}</p>
    <p>前收参考：{observation.reference_status === 'matched' ? '与原方案一致'
      : observation.reference_status === 'changed_or_incompatible' ? '发生变化或不兼容，暂停价格关系比较'
        : '尚未核对，价格关系未知'}；原参考收盘 {priceText(observation.reference_close_cny)}。</p>
    <p>观察线：{observation.threshold_name ? thresholdNames[observation.threshold_name] || '原方案保存的结构参考' : '本型没有可用的单一价格参考'}
      {observation.threshold_cny == null ? '，数值未保存或本型不适用' : ` ${priceText(observation.threshold_cny)}`}。
      快照当时：{relationText(snapshotRelation)}；页面读取时：{relationText(currentRelation)}。</p>
    <p className="subtle">该快照记录的当日最低价关系：{relationText(lowRelation)}。恰好触线与严格低于分别记录；
      单次报价和日内触及都不能确认收盘失效。这里没有计算当日更新后的均线，也没有绑定实际持仓入场锚点。</p>
    <details><summary>查看本票观察状态原记录</summary>
      <dl><dt>报价状态</dt><dd>{observation.quote_status}</dd>
        <dt>参考线名称</dt><dd>{observation.threshold_name || '未保存'}</dd>
        <dt>收盘确认记录</dt><dd>{savedText(observation.close_confirmation)}</dd>
        <dt>持仓入场锚点记录</dt><dd>{savedText(observation.held_entry_anchor)}</dd></dl>
    </details>
  </section>;
}
function CandidateEvidence({ row, trackingRow }: { row: PaperRow; trackingRow?: TrackingRow }) {
  const details = row.candidate_details;
  if (!details) return <div><p className="subtle">旧方案未提供逐票证据。保留代码与原分数，不补行情。</p>
    <p>{row.candidates.map(item => item.symbol).join('；') || '原名单为空'}</p></div>;
  if (!details.length) return <p className="subtle">该项保存的容量后名单为空；批次动作仍以原记录为准。</p>;
  return <div aria-label={`${typeNames[row.type] || row.type}逐票证据`} style={{ maxWidth: 'min(980px, calc(100vw - 64px))', overflowWrap: 'anywhere' }}>
    <p>逐票名单沿原容量后顺序；批次：{decisionText(row)}。单票匹配不改变整批未知或独立退出。</p>
    {details.map(item => <details key={item.symbol} aria-label={`候选${item.symbol}已保存证据`} className="offline-details" style={{ marginTop: 10, paddingTop: 8 }}>
      <summary>{item.symbol} · {item.name || '名称未保存'}</summary>
      <p>源日期 {item.source_day || '未保存'}；{({ main: '主板', chinext: '创业板', star: '科创板' } as Record<string, string>)[item.board || ''] || item.board || '板制未保存'}；
        连续封板阶段 {item.stage == null ? '未知' : item.stage}；当日涨跌幅 {item.pct_chg == null ? '未知' : `${fmt(item.pct_chg, 2)}%`}；
        成交额 {item.amount_cny == null ? '未知' : `${fmt(item.amount_cny / 1e8, 2)}亿元`}；成交额分位 {item.amount_rank_pct == null ? '未知' : percent(item.amount_rank_pct)}。</p>
      <p>身份：{item.identity_status ? savedText(item.identity_status) : '未保存，待核验'}。本票未获交易准入。</p>
      <CandidateIntradayTracking row={row} symbol={item.symbol}
        candidate={trackingRow?.candidates.find(candidate => candidate.symbol === item.symbol)} />
      <strong>原匹配证据</strong><SavedEvidence value={item.evidence} />
      <p>原逐票观察失效条件：{item.invalidation == null ? '未保存，待核验' : item.invalidation.join('；') || '原记录为空'}。</p>
      <p>同日研究参考：收盘 {priceText(item.setup_reference?.reference_close_cny)}；同日原始低点 {priceText(item.setup_reference?.setup_low_cny)}。原参考不是委托价或实际入场价。</p>
      {row.type === 'rebound_repair_candidate' && <p>反包原规则用前一交易日开盘价：{priceText(item.evidence?.previous_open)}。同日低点不替代此项。</p>}
      {['space_height_leader', 'relay_continuation_leader'].includes(row.type) && <p>本型依据原合格梯队与断板条件观察；同日低点不是该型的固定失效阈值。</p>}
      {row.type === 'trend_leader' && <p>已保存观察行 SMA20 {priceText(item.setup_reference?.sma20_observed)}；SMA60 {priceText(item.setup_reference?.sma60_observed)}。</p>}
      <p>{item.checks_status === 'not_run_independent_exit' ? '独立退出已优先；原方案未运行候选入场检查，缺项不作通过处理。'
        : item.checks_status === 'unavailable_in_saved_plan' ? '旧方案未完整保存入场检查，缺项待核验。' : '以下为原方案保存的入场检查，不代表已获执行准入。'}</p>
      <p>上市窗口特征支持：{item.ipo_feature_support == null ? '未知' : item.ipo_feature_support ? '原记录支持' : '原记录不支持'}。</p>
      <details><summary>查看原行情、参考链与上市资格检查</summary>
        <p>当日行情缺项：{item.current_raw_issues == null ? '未保存，待核验' : item.current_raw_issues.length ? item.current_raw_issues.map(savedText).join('；') : '原记录未列出问题；不等于全部质量已核验'}。</p>
        <p>准确前收参考链</p><SavedEvidence value={item.current_reference_evidence} />
        <p>上市资格原检查</p><SavedEvidence value={item.IPO_evidence} />
      </details>
    </details>)}
  </div>;
}

export function LeaderEightTypePaper({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<PaperView | null>(null), [error, setError] = useState('');
  const [guard, setGuard] = useState('two_close_envguard'), [capacity, setCapacity] = useState('causal_first_seen_cap20');
  useEffect(() => {
    const controller = new AbortController(); setView(null); setError('');
    api<PaperView>('/trading/research/leader-eight-type-paper', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [refreshVersion]);
  const rows = view?.rows.filter(row => row.guard === guard && row.capacity === capacity) || [];
  const tracking = view?.intraday_tracking;
  const trackingMatches = Boolean(tracking && view && tracking.paper_source_file === view.source_file
    && tracking.source_close_day === view.source_close_day
    && tracking.execution_day === (view.execution_day_bound || view.proposed_execution_day)
    && !tracking.error && !['unavailable', 'error', 'failed'].includes(tracking.status)
    && Array.isArray(tracking.rows));
  const trackingRows = new Map<string, TrackingRow>();
  if (trackingMatches && tracking) tracking.rows.forEach(row => trackingRows.set(row.case_id, row));
  return <div className="offline-subsection leader-paper-plan" aria-label="八型已保存纸面方案">
    <h3>八类龙型 · 已保存纸面方案</h3>
    {error && <p className="subtle" role="status">八型方案读取失败：{error}。暂不显示旧方案替代。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取已保存的八型方案。</p>}
    {view?.status === 'unavailable' && <p className="subtle" role="status">尚无保存的八型纸面方案。</p>}
    {view && view.status !== 'unavailable' && <>
      <div className="leader-clock"><strong>源收盘 {view.source_close_day}</strong><span>原下一交易日 {view.proposed_execution_day}</span><span>实际记录 {view.recorded_at}</span><span>固定比较 {view.case_count} 项，全部保留</span></div>
      {(view.status === 'late_context_only' || view.execution_window_elapsed) && <p role="status" className="window-stale">这是较早方案的背景记录，不能作为当前操作方案；原日期保留，没有顺延。</p>}
      <p className="subtle">保存环境：{phaseNames[view.environment?.phase || 'unknown']}；相邻两收盘同环境{view.environment?.two_close_confirmation ? '已确认' : '尚未确认'}。结合市场环境的版本要求进入条件得到确认，已知普跌时按固定规则转为空目标；原规则版本保留为未采用这些环境约束的对照。适配关系仍待验证。</p>
      <div className="leader-paper-controls">
        <label>规则对照<select aria-label="查看八型规则对照" value={guard} onChange={event => setGuard(event.target.value)}><option value="two_close_envguard">结合市场环境</option><option value="unguarded">原规则对照</option></select></label>
        <label>研究名单规模<select aria-label="查看八型名单规模对照" value={capacity} onChange={event => setCapacity(event.target.value)}><option value="causal_first_seen_cap20">按首次出现累计20只</option><option value="uncapped">不限制研究名单规模</option></select></label>
      </div>
      <p className="subtle" aria-live="polite">{guard === 'unguarded' ? '当前显示原规则对照；右侧的环境条件仅供比较，不作用于本版研究动作。' : '当前显示结合市场环境的版本。'}</p>
      <p className="subtle">筛选只改变展示，没有选择交易策略。权重尚未绑定持仓、现金、同日报价和实际买入锚点；空目标没有生成卖单，保留原状也不表示空仓。</p>
      <p className="subtle">同一证券可能出现在多个独立龙型或规则对照中，研究权重不能跨类型或对照相加。</p>
      <section aria-label="八型盘中观察状态" data-testid="leader-intraday-tracking-status">
        <strong>已保存候选的盘中风险观察</strong>
        {!tracking ? <p className="subtle" role="status">尚未连接盘中观察，原方案和逐票证据照常显示。</p>
          : !trackingMatches ? <p className="window-stale" role="status">盘中观察不可用，或未匹配这份原方案及日期；没有合并其他方案的报价。原方案照常显示。</p>
            : <>
              <p>快照截至 {tracking.observation_asof || '未保存'}；页面读取 {tracking.read_at || '未保存'}。</p>
              {tracking.summary && <p className="subtle">候选涉及 {tracking.summary.unique_candidates} 只证券，
                已观察 {tracking.summary.observed_unique} 只；读取时仍有效 {tracking.summary.current_unique} 只；
                缺报价 {tracking.summary.missing_unique} 只；前收参考变化或不兼容 {tracking.summary.reference_changed_unique} 只。
                同票在不同方案中的 {tracking.summary.candidate_appearances} 次出现不算独立交易。</p>}
              <p className="subtle">逐票展开可查看快照关系和读取时有效性。页面只展示已保存观察，不采集报价、不改原计划；
                时间过去后报价会过期，触及观察线不等于收盘确认或实际持仓的止损单。</p>
            </>}
      </section>
      <div className="table-scroll"><table className="compact leader-paper-table" aria-label="八型环境与纸面动作">
        <thead><tr><th>龙型</th><th>当前研究动作</th><th>候选<br />容量后 / 原前五</th><th>未知成员</th><th>结合环境版本的进入条件<br />连续两收盘确认</th><th>条件与失效</th></tr></thead>
        <tbody>{rows.map(row => <Fragment key={row.case_id}><tr data-case-id={row.case_id}>
          <td>{typeNames[row.type] || row.type}</td><td>{decisionText(row)}<small className="window-stale">{reasonText(row.reason)}</small></td>
          <td>{row.cap_candidate_count} / {row.original_candidate_count}</td><td>{row.unresolved_member_count}</td><td>{row.entry_phases.map(phase => phaseNames[phase] || phase).join('、')}</td>
          <td><details className="leader-paper-detail"><summary>查看条件、退出与研究权重</summary>
            <p>进入研究前：{row.required_evidence.join('；')}</p>
            <p>观察失效信号：{row.risk_and_invalidation.join('；')}</p>
            <p>已编码结构退出：{exitNames[row.structural_invalidation_policy] || row.structural_invalidation_policy}</p>
            <p>{weightsText(row)}{row.target_cash_weight == null ? '' : `；研究现金权重 ${percent(row.target_cash_weight)}`}</p>
            {Object.keys(row.missing_evidence).length > 0 && <p className="subtle">原证据缺项：{missingText(row.missing_evidence)}</p>}
          </details></td>
        </tr><tr data-details-for={row.case_id}>
          <td colSpan={6} style={{ minWidth: 0, maxWidth: 'none', whiteSpace: 'normal', textAlign: 'left' }}>
            <details className="leader-paper-detail" aria-label={`${typeNames[row.type] || row.type}逐票复盘列表`}>
              <summary>逐票复盘 · {typeNames[row.type] || row.type} · 保存{row.cap_candidate_count}只</summary>
              <CandidateEvidence row={row} trackingRow={trackingRows.get(row.case_id)} />
            </details>
          </td>
        </tr></Fragment>)}</tbody>
      </table></div>
      <p className="subtle">以上为原发表方案的副本，保留当时的候选、研究权重与检查结果。独立研究账户的实际模拟持仓、成交锚点和已保存失效记录列在下方，不由本方案副本推断。日线量价、行业与换手代理不能识别个人资金意图。</p>
      <details className="offline-details"><summary>全部32项原纸面记录</summary><div className="table-scroll"><table className="compact" aria-label="全部八型32项纸面记录"><thead><tr><th>龙型</th><th>规则对照</th><th>名单规模</th><th>研究动作</th><th>研究目标</th></tr></thead><tbody>{view.rows.map(row => <tr key={row.case_id}><td>{typeNames[row.type] || row.type}</td><td>{row.guard === 'unguarded' ? '原规则' : '结合环境'}</td><td>{row.capacity === 'uncapped' ? '不限制研究名单' : '首次出现累计20只'}</td><td>{decisionText(row)}</td><td>{weightsText(row)}</td></tr>)}</tbody></table></div></details>
    </>}
    {view && <details className="offline-details"><summary>八型方案来源与读取时间</summary><dl><dt>方案</dt><dd>{view.source_file || '尚未形成'}</dd><dt>原输入</dt><dd>{view.input_manifest || '尚未形成'}</dd><dt>固定规则</dt><dd>{view.protocol_file || '尚未形成'}</dd><dt>原开盘时点</dt><dd>{view.next_open_deadline || '尚未形成'}</dd><dt>页面读取</dt><dd>{view.read_at}</dd>
      <dt>盘中观察来源</dt><dd>{tracking?.source_file || '尚未连接'}</dd>
      <dt>盘中观察状态</dt><dd>{tracking?.status || '尚未连接'}</dd>
      {(tracking?.error || tracking?.reason) && <><dt>盘中观察原记录说明</dt><dd>{tracking.error || tracking.reason}</dd></>}
    </dl></details>}
    <LeaderEightTypePositions refreshVersion={refreshVersion} />
  </div>;
}
