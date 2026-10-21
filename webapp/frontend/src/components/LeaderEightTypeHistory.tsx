import { Fragment, useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';
import './LeaderEightTypeHistory.css';

type Saved = Record<string, unknown>;
type SourceSupplement = {
  replayed: boolean; original_result: string; original_status: string;
  original_engine_sessions_committed: number; original_last_day: string | null;
  original_blocking_evidence: Saved | null; prefix_preserved: boolean | null;
  new_action_evidence_applied: boolean;
};
type CaseRow = {
  case_id: string; type: string; guard: string; capacity: string; status: string;
  metrics: null | { net_return?: number | null; max_drawdown?: number | null; fees?: number | null };
  order_count: number; fill_count: number; residual_positions: string[] | null;
  engine_sessions_committed?: number; skipped?: boolean; entry_readiness?: Saved | null;
  source_supplement?: SourceSupplement | null;
  source_continuation?: { continued_from_checkpoint: boolean; reused_original_case: boolean;
    reused_sessions: number; newly_completed_sessions: number } | null;
};
type WindowRow = {
  id: string; label: string; start: string; end: string; decision_days: string[]; tail_days: string[];
  environment_counts: Record<string, number>; case_rows: CaseRow[];
  guards?: string[]; source_family?: string; checked_case_count?: number;
  simulated_case_count?: number; skipped_case_count?: number;
  case_ids?: string[]; known_identity_scope?: Saved;
};
type HistoryDay = {
  day: string; execution_day: string | null; in_decision_window: boolean;
  environment: Saved; decision: Saved; cash_cny: number | null; equity_cny: number | null;
  holdings_count: number | null; fees_cny: number | null; orders: Saved[]; fills: Saved[];
  risk_assessments: Saved[]; order_blocks: Saved[];
  classifications: { code: string; label: string; reason: string }[];
};
type HistoryView = {
  schema: string; status: string; read_at: string; built_at: string | null;
  windows: WindowRow[]; selected_window_id: string | null; selected_case_id: string | null;
  detail: null | { case_id: string; days: HistoryDay[]; skipped?: boolean; entry_readiness?: Saved | null;
    source_supplement?: SourceSupplement | null };
};

const TYPES = [
  ['space_height_leader', '空间龙'], ['relay_continuation_leader', '接力龙'],
  ['trend_leader', '趋势龙'], ['liquidity_capacity_proxy', '容量龙代理'],
  ['cross_regime_candidate', '穿越龙候选'], ['rebound_repair_candidate', '反包修复候选'],
  ['sector_catchup_leader', '补涨龙代理'], ['turnover_leader', '换手龙代理'],
] as const;
const GUARDS = [['unguarded', '原规则对照'], ['two_close_envguard', '结合市场环境：相邻两收盘确认'],
  ['two_close_entry_filter_only', '环境只限制入场：保留结构与到期退出']] as const;
const CAPACITIES = [['causal_first_seen_cap20', '按首次出现累计20只'], ['uncapped', '不限制研究名单规模']] as const;
const phaseNames: Record<string, string> = { broad_strength_snapshot: '普涨', mixed_breadth_snapshot: '涨跌分化', broad_weakness_snapshot: '普跌', unknown: '未知' };
const words: Record<string, string> = {
  FULL_TARGET: '更新研究目标', EXPLICIT_EXIT_ALL: '退出全部研究持仓', EXIT_ALL: '退出全部研究持仓',
  NO_CHANGE: '保留原状', ALLOW_FIXED_CANDIDATES: '允许固定候选', buy: '买入', sell: '卖出',
  filled: '已成交', partial: '部分成交', pending: '待处理', rejected: '拒绝', cancelled: '已取消',
  invalid: '已记录结构失效', not_invalid: '本次未发现规则失效', unknown: '证据不足',
  limit_down_queue_unknown: '跌停排队成交条件未知', limit_up_queue_unknown: '涨停排队成交条件未知',
  environment_unknown_or_unconfirmed: '环境未知或尚未连续确认',
  known_weakness_independent_next_open_risk_exit: '已知普跌，按固定规则排到下一交易日退出',
  two_exact_adjacent_closes_confirmed: '准确相邻两收盘环境相同，已确认',
  awaiting_two_exact_adjacent_same_phase_closes: '等待准确相邻两收盘环境相同',
  confirmed_phase_not_applicable_to_fixed_type: '已确认环境不适合该类型',
  type_membership_unknown_in_fixed_panel: '固定研究范围仍有类型归属未知成员',
  type_empty_or_capacity_filtered_all_no_signal: '没有候选，或名单规模约束后没有候选',
  type_observation_unknown: '龙型观察证据不足', execution_tail_no_new_entry: '退出观察期，不再新增入场',
  environment_exit_all_no_new_entry: '环境要求退出，不允许新增入场',
  held_or_pending_raw_or_action_reference_unknown: '持仓或待执行目标缺少原价、公司行动或参考价依据',
  withheld_missing_exact_successor_full_source: '缺少准确下一交易日的完整来源',
  unguarded_original_rule_environment_observation_only: '原规则对照中，环境只作观察',
  explicit_environment_exit_disabled_known_evidence_retains_NO_CHANGE_not_feature_unknown: '本对照停用环境强制退出，保留原仓；环境证据并未变成未知',
  current_close_below_fixed_entry_structure: '收盘跌破入场时固定结构价',
  current_qualified_limit_explicitly_broken: '已确认不再封板',
  current_amount_rank_below_fixed_0_99: '成交额分位低于原固定阈值',
};
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const number = (value: unknown, digits = 0) => finite(value) ? fmt(value, digits) : '—';
const money = (value: unknown) => finite(value) ? `${fmt(value, 2)} 元` : '—';
const percent = (value: unknown) => finite(value) ? `${fmt(value * 100, 2)}%` : '—';
const label = (value: unknown) => typeof value === 'string' ? words[value] || value : '未保存';
const reasons = (value: unknown): string => Array.isArray(value) ? value.map(label).join('；') || '原记录为空' : label(value);
const object = (value: unknown): Saved | null => value != null && typeof value === 'object' && !Array.isArray(value) ? value as Saved : null;
const rows = (value: unknown): Saved[] => Array.isArray(value) ? value.map(object).filter((row): row is Saved => row != null) : [];

function SavedRecord({ value }: { value: unknown }) {
  return <details className="leader-history-original"><summary>原记录字段</summary>
    <pre>{JSON.stringify(value, null, 2)}</pre>
  </details>;
}

function AssessmentFacts({ assessment }: { assessment: Saved }) {
  const facts = object(assessment.facts), entry = object(assessment.entry);
  const text = (value: unknown) => typeof value === 'string' && value ? value : '—';
  const boolean = (value: unknown) => value === true ? '是' : value === false ? '否' : '—';
  const symbols = (value: unknown) => Array.isArray(value) ? value.length ? value.map(String).join('、') : '原记录为空' : '—';
  const fields: [string, string, (value: unknown) => string][] = [
    ['close', '评估日收盘价', money], ['structure_price', '入场时固定结构价', money],
    ['sma20', 'SMA20', money], ['sma60', 'SMA60', money],
    ['amount_rank_pct', '成交额分位', percent], ['fixed_cutoff', '原固定分位阈值', percent],
    ['qualified_limit_up', '合格封板', boolean], ['qualified_stage', '合格梯队', value => number(value)],
    ['board', '板制', value => ({ main: '主板', chinext: '创业板', star: '科创板' } as Record<string, string>)[String(value)] || text(value)],
    ['known_higher_same_board_symbols', '同板制已知更高梯队', symbols],
    ['unknown_same_board_height_count', '同板制未知梯队数', value => number(value)],
    ['entry_reference_day', '入场参考日', text],
  ];
  return <dl className="leader-history-risk-facts">
    <dt>原入场决定日</dt><dd>{text(entry?.decision_day)}</dd>
    <dt>原买入成交日</dt><dd>{text(entry?.fill_day)}</dd>
    {facts && fields.filter(([key]) => key in facts).map(([key, name, format]) => <Fragment key={key}>
      <dt>{name}</dt><dd>{format(facts[key])}</dd>
    </Fragment>)}
    {!facts && <><dt>结构事实</dt><dd>—</dd></>}
  </dl>;
}

function price(row: Saved, kind: 'orders' | 'fills') {
  if (finite(row.price_cny)) return money(row.price_cny);
  if (kind === 'orders' && finite(row.limit_price_cny)) return money(row.limit_price_cny);
  const minor = kind === 'orders' ? row.limit_price : row.price;
  return finite(minor) ? `${fmt(minor / 100, 2)} 元（原值 ${minor} 分）` : '—';
}

function Trades({ items, kind }: { items: Saved[]; kind: 'orders' | 'fills' }) {
  if (!items.length) return <p className="subtle">该会话没有保存的{kind === 'orders' ? '委托' : '成交'}记录。</p>;
  return <>{kind === 'orders' && <p className="subtle">委托按创建日归组；状态与已成交数量来自期末保存记录，不是该日时点快照。</p>}<div className="table-scroll"><table className="compact" aria-label={`该日模拟${kind === 'orders' ? '委托' : '成交'}`}>
    <thead><tr><th>证券 / 方向</th><th>股数</th><th>{kind === 'orders' ? '委托限价' : '成交价'}</th><th>费用</th><th>{kind === 'orders' ? '状态 / 已成交' : '结算可用日'}</th><th>原件</th></tr></thead>
    <tbody>{items.map((row, index) => <tr key={String(row.id || row.order_id || '') + index}>
      <td>{String(row.symbol || '未保存')}<small>{label(row.side)}</small></td><td>{number(row.qty)}</td>
      <td>{price(row, kind)}</td><td>{money(finite(row.fees_cny) ? row.fees_cny : finite(row.fees) ? row.fees / 100 : null)}</td>
      <td>{kind === 'orders' ? `${label(row.status)} / ${number(row.filled)} 股` : String(row.available_day || '未保存')}</td>
      <td><SavedRecord value={row} /></td>
    </tr>)}</tbody>
  </table></div></>;
}

function DayDetails({ day }: { day: HistoryDay }) {
  const targets = rows(day.decision.cap_candidates);
  const exitSuppressed = day.environment.explicit_exit_suppressed === true;
  return <div className="leader-history-day">
    <p>保存的环境处理：{label(day.environment.effective_mode ?? day.environment.mode)}；{label(day.environment.effective_reason ?? day.environment.reason)}。
      相邻两收盘确认：{day.environment.two_close_confirmation === true ? '是' : day.environment.two_close_confirmation === false ? '否' : '未保存'}。</p>
    {day.environment.explicit_exit_suppressed === true && <p>原环境规则为 {label(day.environment.original_type_mode)}；本对照改为保留原状。结构失效、独立五会话到期及后续目标替换仍生效。</p>}
    <p>决定日 {day.day} → 对应执行日 {day.execution_day || '未保存'}：{label(day.decision.status)}。
      {day.decision.status === 'NO_CHANGE' ? '保留原仓位，候选列表不是新增买单。' : day.decision.status === 'EXPLICIT_EXIT_ALL' ? '空研究目标不代表已经卖出。' : ''}</p>
    {exitSuppressed
      ? <p>原接口批次标签：{reasons(day.decision.reasons)}{day.decision.no_change_reason ? `；${label(day.decision.no_change_reason)}` : ''}。这是沿用的原记录标签；本次保留原仓是因为停用环境强制退出，不表示环境资料未知。</p>
      : <p>原决定原因：{reasons(day.decision.reasons)}{day.decision.no_change_reason ? `；${label(day.decision.no_change_reason)}` : ''}。</p>}
    <p>保存的目标候选：{targets.length ? targets.map(row => String(row.symbol || '未保存')).join('、') : '原列表为空'}；容量约束移除 {number(day.decision.cap_dropped_rows)} 只。</p>
    <p className="subtle">本日费用 {money(day.fees_cny)}。原日账只保存收盘持仓只数，不据此补造逐股持仓或可卖量。</p>
    <SavedRecord value={{ environment: day.environment, decision: day.decision }} />
    <h4>模拟委托 · {day.orders.length} 条</h4><Trades items={day.orders} kind="orders" />
    <h4>模拟成交分笔 · {day.fills.length} 条</h4><Trades items={day.fills} kind="fills" />
    <h4>保存的结构判断 · {day.risk_assessments.length} 条</h4>
    {day.risk_assessments.length ? day.risk_assessments.map((assessment, index) => <div className="leader-history-fact" key={String(assessment.symbol) + index}>
      <p><strong>{String(assessment.symbol || '证券未保存')} · {label(assessment.status)}</strong>；{reasons(assessment.reasons)}。</p>
      <AssessmentFacts assessment={assessment} />
      <SavedRecord value={assessment} />
    </div>) : <p className="subtle">本日未保存结构评估记录，不能据此断言结构有效。</p>}
    <h4>委托受阻 · {day.order_blocks.length} 条</h4>
    {day.order_blocks.length ? day.order_blocks.map((block, index) => <div className="leader-history-fact" key={String(block.symbol) + index}>
      <p>{String(block.symbol || '证券未保存')} · {label(block.side)} {number(block.qty)} 股：{label(block.reason)}。</p>
      <SavedRecord value={block} />
    </div>) : <p className="subtle">本日没有保存的委托受阻记录。</p>}
    {day.classifications.length > 0 && <><h4>本日记录说明</h4><ul>{day.classifications.map((item, index) => <li key={item.code + index}>{exitSuppressed && item.code === 'environment_blocked'
      ? `保留原仓：本对照停用环境强制退出；原记录标签 ${item.reason} 不表示环境资料未知`
      : `${item.label}：${item.reason}`}</li>)}</ul></>}
  </div>;
}

function EntryReadiness({ value }: { value: Saved }) {
  const failures = object(value.failure_reason_counts);
  const unknowns = rows(value.common_unresolved_symbols);
  return <div className="leader-history-day" aria-label="保存面板入场必要条件">
    <p>决定日 {number(value.decision_day_count)}；容量内候选非空 {number(value.nonempty_candidate_days)} 日；
      类型归属仍有未知成员 {number(value.membership_unknown_days)} 日；满足入场必要条件 {number(value.potential_entry_days)} 日。
      首个日期 {typeof value.first_potential_entry_day === 'string' ? value.first_potential_entry_day : '无'}。</p>
    {failures && <ul>{Object.entries(failures).map(([reason, count]) => <li key={reason}>{label(reason)}：{number(count)} 日</li>)}</ul>}
    {unknowns.length > 0 && <p>常见未明确成员：{unknowns.map(row => `${String(row.symbol)}（${number(row.decision_days)} 日）`).join('、')}。</p>}
    <p className="subtle">这只检查保存面板的入场必要条件；有候选仍需原价、公司行动、资格、资金与实际成交检查，不能据此认定可交易。</p>
    <SavedRecord value={value} />
  </div>;
}

function SourceSupplementNote({ value }: { value: SourceSupplement }) {
  const block = value.original_blocking_evidence;
  const originalBlockDay = typeof block?.day === 'string' ? block.day : '未知';
  const originalBlockReason = typeof block?.reason === 'string' ? label(block.reason) : '原因未知';
  const prefix = value.prefix_preserved === true ? '一致' : value.prefix_preserved === false ? '不一致' : '未知';
  return <p className="subtle" aria-label="本例补充资料与原运行说明">{value.replayed
    ? <>行动资料补充后回放；原运行截至 {value.original_last_day ?? '未知'}、{number(value.original_engine_sessions_committed)} 会话；
      原中断 {originalBlockDay} / {originalBlockReason}；此前账户记录核对{prefix}。</>
    : '复用原结果；本例未重跑，未采用新行动版本。'}</p>;
}

function ContinuationNote({ value }: { value: NonNullable<CaseRow['source_continuation']> }) {
  return <p className="subtle" aria-label="原账户接续说明">{value.continued_from_checkpoint
    ? `从原账户断点继续：保留 ${number(value.reused_sessions)} 个已提交会话，新增 ${number(value.newly_completed_sessions)} 个后续会话；原现金、持仓和成交记录保持。`
    : `复用原已完成账户的 ${number(value.reused_sessions)} 个会话，本次没有重放。`}</p>;
}

export function LeaderEightTypeHistory() {
  const [selection, setSelection] = useState<{ windowId: string; caseId: string } | null>(null);
  const [catalog, setCatalog] = useState<WindowRow[]>([]);
  const [view, setView] = useState<HistoryView | null>(null), [error, setError] = useState('');
  const [readVersion, setReadVersion] = useState(0);
  const windowId = selection?.windowId ?? view?.selected_window_id ?? '';
  const caseId = selection?.caseId ?? view?.selected_case_id ?? '';
  const parts = caseId.split('__');
  const typeId = parts[0] || TYPES[0][0], guard = parts[1] || GUARDS[0][0], capacity = parts[2] || CAPACITIES[0][0];

  useEffect(() => {
    const controller = new AbortController();
    setView(null); setError('');
    const requested = selection;
    const params = requested ? new URLSearchParams({ window_id: requested.windowId, case_id: requested.caseId }) : new URLSearchParams();
    api<HistoryView>(`/trading/research/leader-eight-type-history?${params}`, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        if (!Array.isArray(value.windows) || (value.detail != null && (
          (requested != null && (value.selected_window_id !== requested.windowId || value.selected_case_id !== requested.caseId))
          || value.detail.case_id !== value.selected_case_id || !Array.isArray(value.detail.days)))) {
          throw new Error('保存的历史明细与所选月份或账户不一致');
        }
        setCatalog(value.windows); setView(value);
      })
      .catch(cause => { if (!controller.signal.aborted) { setView(null); setError(cause.message); } });
    return () => controller.abort();
  }, [selection?.windowId, selection?.caseId, readVersion]);

  const detail = view?.selected_window_id === windowId && view.selected_case_id === caseId ? view.detail : null;
  const loading = !view || (view.detail != null && !detail);
  const selectedWindow = detail ? view?.windows.find(item => item.id === windowId) : undefined;
  const selectedCase = selectedWindow?.case_rows.find(item => item.case_id === caseId);
  const catalogWindow = catalog.find(item => item.id === windowId);
  const availableRows = catalogWindow?.case_rows || [];
  const typeOptions = TYPES.filter(([id]) => !availableRows.length || availableRows.some(row => row.type === id));
  const guardOptions = GUARDS.filter(([id]) => availableRows.length
    ? availableRows.some(row => row.type === typeId && row.guard === id)
    : id !== 'two_close_entry_filter_only');
  const capacityOptions = CAPACITIES.filter(([id]) => !availableRows.length
    || availableRows.some(row => row.type === typeId && row.guard === guard && row.capacity === id));
  const skipped = selectedCase?.skipped === true;
  const residuals = selectedCase?.residual_positions;
  const entryReadiness = detail?.entry_readiness || selectedCase?.entry_readiness;
  const sourceSupplement = detail?.source_supplement ?? selectedCase?.source_supplement;
  const comparisonRows = selectedWindow?.case_rows.filter(row => row.type === typeId).sort((a, b) =>
    CAPACITIES.findIndex(([id]) => id === a.capacity) - CAPACITIES.findIndex(([id]) => id === b.capacity)
    || GUARDS.findIndex(([id]) => id === a.guard) - GUARDS.findIndex(([id]) => id === b.guard)) || [];
  const selectCase = (row: CaseRow) => setSelection({ windowId, caseId: row.case_id });
  const changeWindow = (next: string) => {
    const nextWindow = catalog.find(item => item.id === next);
    const nextRows = nextWindow?.case_rows || [];
    const nextCase = nextRows.find(row => row.case_id === caseId)
      || nextRows.find(row => row.type === typeId && row.guard === guard)
      || nextRows.find(row => row.type === typeId) || nextRows[0];
    if (nextCase) setSelection({ windowId: next, caseId: nextCase.case_id });
  };
  const changeType = (next: string) => {
    const row = availableRows.find(item => item.type === next && item.guard === guard && item.capacity === capacity)
      || availableRows.find(item => item.type === next && item.guard === guard)
      || availableRows.find(item => item.type === next);
    if (row) selectCase(row);
  };
  const changeGuard = (next: string) => {
    const row = availableRows.find(item => item.type === typeId && item.guard === next && item.capacity === capacity)
      || availableRows.find(item => item.type === typeId && item.guard === next);
    if (row) selectCase(row);
  };
  const changeCapacity = (next: string) => {
    const row = availableRows.find(item => item.type === typeId && item.guard === guard && item.capacity === next);
    if (row) selectCase(row);
  };
  const windows = catalog.length ? catalog : [{ id: '2026-01', label: '2026年1月' }, { id: '2026-02', label: '2026年2月' }];
  return <section className="offline-subsection leader-eight-history" aria-label="八型历史环境与持仓响应">
    <div className="research-heading"><h3>八类龙型 · 历史环境与持仓响应</h3>
      <button aria-label="重新读取八型历史保存结果" disabled={loading && !error} onClick={() => setReadVersion(value => value + 1)}>读取保存结果</button></div>
    <p className="subtle">按原固定规则浏览保存的研究记录。先看环境、决定与实际模拟成交的先后，再比较同一名单规模下的环境处理；筛选只改变展示。</p>
    <div className="leader-paper-controls leader-history-controls">
      <label>历史区间<select aria-label="八型历史月份" value={windowId} onChange={event => changeWindow(event.target.value)}>{windows.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>研究龙型<select aria-label="八型历史龙型" value={typeId} onChange={event => changeType(event.target.value)}>{typeOptions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>环境处理<select aria-label="八型历史环境处理" value={guard} onChange={event => changeGuard(event.target.value)}>{guardOptions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>名单规模<select aria-label="八型历史名单规模" value={capacity} onChange={event => changeCapacity(event.target.value)}>{capacityOptions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
    </div>
    {error ? <p role="status" className="window-stale">历史复盘读取失败：{error}。暂不显示旧结果替代。</p>
      : loading ? <p role="status" className="subtle">正在读取所选研究账户的保存结果。</p>
        : !view || !detail || !selectedWindow || !selectedCase ? <p role="status" className="subtle">所选历史结果尚不可读取，资金、持仓和收益保持未知。</p> : <>
          <p className="subtle">决定期 {selectedWindow.start} 至 {selectedWindow.end}，共 {selectedWindow.decision_days.length} 个交易日；另有 {selectedWindow.tail_days.length} 个退出观察日。
            {Object.entries(selectedWindow.environment_counts).map(([phase, count]) => ` ${phaseNames[phase] || phase} ${count} 日`).join('；')}。</p>
          {selectedWindow.source_family === 'fixed_environment_exit_ablation48' && <p className="subtle">固定 48 组中，已检查 {number(selectedWindow.checked_case_count)} 组，实际模拟 {number(selectedWindow.simulated_case_count)} 组，未满足入场必要条件而跳过 {number(selectedWindow.skipped_case_count)} 组。不同区间结果不作为只改变环境规则的同样本对比。</p>}
          {selectedWindow.source_family === 'known_positive_crossing_scope4' && <p className="subtle" aria-label="已知身份穿越研究范围">本窗口单独研究已知身份的穿越候选，共四个独立研究账户。无关成员的身份未知不阻止入场；选中、持有或待执行证券自身未知仍保留原状。原八型窗口继续保留，不能把本结果当作原32组的完成结果。两期范围和行业来源不同、资料事后取得，尚未证明稳定收益优势。</p>}
          <div className="leader-history-comparison" aria-label="同期间同龙型环境对照">
            <h4>同期间、同龙型 · 环境规则对照</h4>
            <p className="subtle">每种名单规模内比较环境处理，全部结果保留。点击只读取该研究账户的逐日记录。</p>
            <div className="table-scroll"><table className="compact" aria-label="同名单规模环境规则比较"><thead><tr><th>名单规模</th><th>环境处理</th><th>完整会话</th><th>净收益</th><th>最大回撤</th><th>委托 / 成交分笔</th><th>逐日记录</th></tr></thead>
              <tbody>{comparisonRows.map(row => <tr key={row.case_id} aria-current={row.case_id === caseId ? 'true' : undefined}>
                <td>{CAPACITIES.find(([id]) => id === row.capacity)?.[1] || row.capacity}</td>
                <td>{GUARDS.find(([id]) => id === row.guard)?.[1] || row.guard}</td>
                <td>{row.skipped ? '未创建账户' : number(row.engine_sessions_committed)}</td>
                <td>{row.fill_count === 0 ? row.skipped ? '未模拟' : '未成交' : percent(row.metrics?.net_return)}</td>
                <td>{row.fill_count === 0 ? '—' : percent(row.metrics?.max_drawdown)}</td>
                <td>{row.order_count} / {row.fill_count}</td>
                <td><button aria-label={`查看${row.case_id}逐日记录`} disabled={row.case_id === caseId} onClick={() => selectCase(row)}>{row.case_id === caseId ? '当前账户' : '查看逐日'}</button></td>
              </tr>)}</tbody></table></div>
          </div>
          {selectedCase.source_continuation && <ContinuationNote value={selectedCase.source_continuation} />}
          {sourceSupplement != null ? <SourceSupplementNote value={sourceSupplement} /> : null}
          <div className="metrics trade-metrics leader-history-metrics" aria-label="所选历史账户指标">
            <div><span>含退出观察期及费用的条件模拟收益</span><strong>{percent(selectedCase.metrics?.net_return)}</strong></div>
            <div><span>最大回撤</span><strong>{percent(selectedCase.metrics?.max_drawdown)}</strong></div>
            <div><span>保存委托 / 成交分笔</span><strong>{selectedCase.order_count} / {selectedCase.fill_count}</strong></div>
            <div><span>期末残余持仓只数</span><strong>{number(Array.isArray(residuals) ? residuals.length : null)}</strong></div>
            {selectedCase.engine_sessions_committed != null && <div><span>实际提交的模拟会话</span><strong>{number(selectedCase.engine_sessions_committed)}</strong></div>}
          </div>
          {selectedCase.metrics == null && <p className="subtle">“—”表示没有完整可报告的模拟收益；无成交不会补成有效零收益。</p>}
          {Array.isArray(residuals) && residuals.length > 0 && <p className="window-stale">原残余持仓：{residuals.join('、')}。没有按期末价格假定清仓。</p>}
          {skipped && <p role="status" className="window-stale">该组合在保存面板中没有满足入场必要条件的日期，因此未创建或模拟账户。账户、残仓和收益保持未知；这不等于零收益或策略失效，也没有逐日模拟记录。</p>}
          {entryReadiness && <EntryReadiness value={entryReadiness} />}
          {!skipped && <div className="table-scroll"><table className="compact leader-history-table" aria-label="历史环境决定与持仓逐日记录">
            <thead><tr><th>会话日</th><th>环境</th><th>D决定 → E</th><th>现金 / 权益</th><th>收盘持仓只数</th><th>本日记录</th></tr></thead>
            <tbody>{detail.days.map(day => <Fragment key={day.day}>
              <tr><td>{day.day}<small>{day.in_decision_window ? '决定期' : '退出观察期'}</small></td>
                <td>{phaseNames[String(day.environment.phase)] || '未保存'}<small>{day.environment.two_close_confirmation === true ? '两收盘已确认' : day.environment.two_close_confirmation === false ? '尚未两收盘确认' : '确认情况未保存'}</small></td>
                <td>{label(day.decision.status)}<small>对应 {day.execution_day || '未保存'}</small></td>
                <td>{money(day.cash_cny)}<small>{money(day.equity_cny)}</small></td><td>{number(day.holdings_count)}</td>
                <td>委托 {day.orders.length} / 成交 {day.fills.length}<small>评估 {day.risk_assessments.length} / 受阻 {day.order_blocks.length}</small></td>
              </tr>
              <tr><td colSpan={6} className="leader-history-expand"><details key={`${caseId}-${day.day}`} aria-label={`${day.day}历史决定与成交详情`}>
                <summary>{day.day} · 展开环境、原目标、模拟成交与失效事实</summary><DayDetails day={day} />
              </details></td></tr>
            </Fragment>)}</tbody>
          </table></div>}
          <p className="subtle">结果含退出观察日，不是自然月收益或样本外验证。相同股票跨账户的成交不视为独立样本；日线量价与换手不能证明个人资金意图。历史资格、行动与当时可得性限制沿用原记录，未启用策略选择或交易。</p>
          <p className="subtle">保存投影时间 {view.built_at || '未保存'}；本次读取 {view.read_at}。页面读取不推进研究账户。</p>
        </>}
  </section>;
}
