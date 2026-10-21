import { fmt } from '../types';

type Description = { scope_rows: number; observed_nonnegative_turnover_rows: number;
  missing_or_invalid_turnover_rows: number; unit: string; median_percent: number | null;
  quantiles_percent: { p10: number | null; p50: number | null; p90: number | null } };
type Group = { id: string; name: string; existing_status: string; known_proxy_candidate_count: number;
  description: Description; unknown_membership_included_as_known: boolean;
  selection_enabled: boolean; leader_identity_confirmed_by_turnover: boolean };
export type TurnoverContext = { schema: string; status: string; day: string; observed_at: string;
  raw_acquisition_finished_at: string; source_inputs_available_at: string; extras_actual_derived_at: string;
  extras_result: string; active_non_ST_rows: number; ordinary_inventory_rows: number;
  all_active_non_ST_features_matched: boolean; overall: Description; archetypes: Group[];
  candidate_panel_only: boolean; strategy_validated: boolean; selection_enabled: boolean;
  execution_admitted: boolean; orders_generated: boolean; ledger_mutated: boolean; model_refit: boolean;
  future_labels_used_for_score: boolean; frozen_rules_modified: boolean;
  existing_archetype_states_modified: boolean; identified_capital_flow: boolean; participant_intent_inferred: boolean;
  unit_evidence: { turn_unit: string } };

const ids = ['space_height_leader', 'relay_continuation_leader', 'trend_leader', 'liquidity_capacity_proxy',
  'cross_regime_candidate', 'rebound_repair_candidate', 'sector_catchup_leader', 'turnover_leader'];
const states: Record<string, string> = { matched: '原日线代理匹配', not_matched: '原证据未匹配', unknown: '原分类证据不足' };
const percentage = (value: number | null) => value == null || !Number.isFinite(value) ? '待核验' : `${fmt(value, 2)}%`;
const count = (value: number) => Number.isFinite(value) ? fmt(value, 0) : '待核验';
function validDescription(value: Description | undefined, expectedRows: number) {
  return !!value && value.unit === 'percent' && Number.isInteger(expectedRows) && expectedRows >= 0
    && value.scope_rows === expectedRows && Number.isInteger(value.observed_nonnegative_turnover_rows)
    && value.observed_nonnegative_turnover_rows >= 0 && Number.isInteger(value.missing_or_invalid_turnover_rows)
    && value.missing_or_invalid_turnover_rows >= 0
    && value.observed_nonnegative_turnover_rows + value.missing_or_invalid_turnover_rows === expectedRows
    && !!value.quantiles_percent;
}
function isSameDayResearchContext(day: string | undefined, value: TurnoverContext | undefined) {
  if (!value || !day || value.day !== day || value.schema !== 'leader_exact_dated_observed_turnover_context_v1'
    || value.status !== 'observed_same_day_turnover_descriptive_context_only' || value.candidate_panel_only !== true
    || value.all_active_non_ST_features_matched !== true || value.unit_evidence?.turn_unit !== 'percent'
    || [value.strategy_validated, value.selection_enabled, value.execution_admitted, value.orders_generated,
      value.ledger_mutated, value.model_refit, value.future_labels_used_for_score, value.frozen_rules_modified,
      value.existing_archetype_states_modified, value.identified_capital_flow, value.participant_intent_inferred]
      .some(flag => flag !== false)
    || !validDescription(value.overall, value.active_non_ST_rows) || !Array.isArray(value.archetypes)
    || value.archetypes.length !== ids.length || new Set(value.archetypes.map(group => group.id)).size !== ids.length) return false;
  return value.archetypes.every(group => ids.includes(group.id) && group.unknown_membership_included_as_known === false
    && group.selection_enabled === false && group.leader_identity_confirmed_by_turnover === false
    && Object.hasOwn(states, group.existing_status) && validDescription(group.description, group.known_proxy_candidate_count));
}

export function LeaderTurnoverContext({ day, context }: { day?: string; context?: TurnoverContext }) {
  const dayLabel = day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? `${Number(day.slice(-2))}日` : '本收盘日';
  if (!isSameDayResearchContext(day, context) || !context) return <details className="offline-details" open aria-label="同日换手资料">
    <summary>换手率观察 · 同日资料待核验</summary>
    <p role="status">{dayLabel}同日换手资料待核验。</p>
    <p className="subtle">仅显示与本次收盘日完全一致、来源及研究边界合格的换手观察；其他日期资料不补作当日数据。</p>
  </details>;
  const overall = context.overall;
  return <details className="offline-details" open aria-label="同日换手资料">
    <summary>换手率观察 · {context.day} 同日完整来源</summary>
    <div className="metrics trade-metrics leader-market-metrics">
      <div><span>活跃非ST样本</span><strong>{count(context.active_non_ST_rows)}</strong></div>
      <div><span>换手已知 / 未知</span><strong>{count(overall.observed_nonnegative_turnover_rows)} / {count(overall.missing_or_invalid_turnover_rows)}</strong></div>
      <div><span>换手率 P10</span><strong>{percentage(overall.quantiles_percent.p10)}</strong></div>
      <div><span>换手率 P50</span><strong>{percentage(overall.quantiles_percent.p50)}</strong></div>
      <div><span>换手率 P90</span><strong>{percentage(overall.quantiles_percent.p90)}</strong></div>
    </div>
    <p className="subtle">原始采集完成 {context.raw_acquisition_finished_at}；完整来源可用 {context.source_inputs_available_at}；换手派生 {context.extras_actual_derived_at}；环境观察记录 {context.observed_at}。</p>
    <div className="table-scroll"><table className="compact" aria-label="原龙型候选换手描述"><thead><tr><th>原龙型</th><th>原分类证据</th><th>全量已知候选</th><th>换手已知 / 未知</th><th>候选换手中位数</th></tr></thead><tbody>{context.archetypes.map(group => <tr key={group.id}>
      <td>{group.name}</td><td>{states[group.existing_status]}</td>
      <td>{group.existing_status === 'unknown' ? `未知，已知子集 ${count(group.known_proxy_candidate_count)}` : count(group.known_proxy_candidate_count)}</td>
      <td>{count(group.description.observed_nonnegative_turnover_rows)} / {count(group.description.missing_or_invalid_turnover_rows)}</td>
      <td>{group.existing_status === 'unknown' || group.known_proxy_candidate_count === 0 ? '待核验' : percentage(group.description.median_percent)}</td>
    </tr>)}</tbody></table></div>
    <p className="subtle">换手率单位为%。统计覆盖保存的全量已知候选，页面展示上限不改变这里的分母；未知成员不算已知，空集合没有中位数。</p>
    <p className="subtle">流通股本分母尚未独立核验，历史资格、财务资料的当时可得性和盘中换手路径仍缺证据。换手率不能识别资金净流入或参与者意图，也未据此认定换手龙或改变原标签。</p>
    <p className="mono subtle">{context.extras_result}</p>
  </details>;
}
