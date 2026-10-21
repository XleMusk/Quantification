import { Fragment, useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';

type SavedFields = Record<string, unknown>;
type Decision = { kind: string; decision_day: string; execution_day: string; weights: Record<string, number> | null };
export type SavedLeaderPosition = {
  symbol: string; quantity_shares: number; sellable_on_book_day_shares: number | null;
  t1_locked_shares: number | null; sell_reserved_shares?: number | null;
  mark_price_cny: number | null; marked_value_cny: number | null; mark_stale?: boolean;
  anchor: null | { status: string; reason?: string; fill_id: string | null; fill_day: string | null;
    decision_day: string | null; fill_price_cny: number | null; available_day: string | null;
    remaining_earliest_fill_shares: number | null; setup_low_cny: number | null; setup_high_cny: number | null;
    candidate_available_at?: string | null; setup_price_source?: string | null; evidence: SavedFields | null };
  assessment: null | { day: string; status: string; reasons: string[]; facts: SavedFields; policy_id: string };
  queued_risk_exit: null | { execution_on: string; reason: string; source: string; decided_on?: string; execution_day_elapsed?: boolean };
  public_quote_tracking?: { status: string; snapshot_price_cny: number | null;
    source_quote_time: string | null; received_at: string | null; fresh_at_read: boolean;
    book_reference_status: string; price_change_from_book_percent: number | null;
    rule_requirement: string; source_file: string | null };
};
type Account = {
  case_id: string; type: string; guard: string; capacity: string; processed_day: string | null;
  cash_cny: number | null; equity_cny: number | null; pending_decision: Decision | null;
  positions: SavedLeaderPosition[]; latest_order_blocks: SavedFields[]; error: unknown; blocking_evidence: unknown;
  latest_journal?: { file: string; sha256: string } | null; last_commit_at?: string | null;
};
type PositionView = {
  schema: string; status: 'saved_research_positions' | 'unavailable'; read_at: string;
  run_directory: string; head_ref: { file: string; sha256: string } | null;
  head_recorded_at: string | null; last_commit_at?: string | null; case_count: number | null;
  latest_processed_day: string | null; all_cases_same_day?: boolean | null; rows: Account[];
  position_case_count?: number; case_symbol_position_count?: number; unique_position_symbol_count?: number;
  intraday_tracking?: { status: string; error?: string; observation_asof?: string | null;
    observed_unique?: number; unique_position_symbols?: number; current_unique?: number;
    missing_symbols?: string[]; source_file?: string | null };
};

const typeNames: Record<string, string> = {
  space_height_leader: '空间龙', relay_continuation_leader: '接力龙', trend_leader: '趋势龙',
  liquidity_capacity_proxy: '容量龙代理', cross_regime_candidate: '穿越龙候选',
  rebound_repair_candidate: '反包修复候选', sector_catchup_leader: '补涨龙代理', turnover_leader: '换手龙代理',
};
const reasonNames: Record<string, string> = {
  current_qualified_limit_explicitly_broken: '该评估日已确认不再封板',
  current_qualified_limit_unknown: '该评估日封板资格待核',
  known_higher_same_board_qualified_ladder: '同板制出现已知更高的合格梯队',
  same_board_complete_current_height_unavailable: '同板制完整梯队证据不足',
  same_board_peer_height_unknown: '同板制仍有梯队未知成员',
  complete_current_trend_fields_unavailable: '该评估日趋势字段不完整',
  current_close_below_sma20_or_sma20_not_above_sma60: '该评估日收盘低于SMA20，或SMA20不再高于SMA60',
  current_amount_rank_unknown: '该评估日成交额分位待核',
  current_amount_rank_below_fixed_0_99: '该评估日成交额低于固定99%分位',
  current_close_below_fixed_entry_structure: '该评估日收盘低于入场时固定结构价',
  required_fixed_entry_structure_unavailable: '所需的固定入场结构或收盘证据不足',
  current_feature_source_day_mismatch: '特征来源日期不符',
  entry_previous_open_conflicts_with_exact_reference: '入场前一日开盘价与准确参考不符',
  saved_entry_setup_disagrees_with_exact_raw: '保存的入场结构与准确原行情不符',
  actual_buy_entry_candidate_evidence_unavailable: '实际买入所对应的原候选证据未保存',
  actual_fill_history_invalid: '实际模拟成交历史待核',
  actual_buy_settlement_unavailable: '买入成交的结算日期待核',
  actual_sell_exceeds_settled_buy_history: '卖出数量与已结算买入历史不符',
  actual_fill_position_reconciliation_unknown: '实际模拟成交与残余持仓的对应关系待核',
  buy_fill_exact_previous_decision_session_unavailable: '买入前的准确决策交易日待核',
  fixed_structure_or_five_session_expiry_queue_E_quality_checked_before_execution: '固定结构失效或满五交易日，等待执行日行情检查',
};
const fieldNames: Record<string, string> = {
  close: '评估日收盘价', sma20: 'SMA20', sma60: 'SMA60', amount_rank_pct: '成交额分位', fixed_cutoff: '固定分位阈值',
  structure_price: '入场时固定结构价', structure_basis: '结构价依据', entry_reference_day: '入场参考日',
  qualified_limit_up: '该评估日合格封板', qualified_stage: '合格梯队', board: '板制',
  known_higher_same_board_symbols: '同板制已知更高梯队', unknown_same_board_height_count: '同板制梯队未知数',
  window_semantics: '窗口口径', previous_open: '原前一交易日开盘价', structure_close: '冻结环境段参考收盘价',
  setup_low: '原启动低点', structure_high: '原启动高点', day: '日期', symbol: '证券', side: '方向', qty: '股数',
  filled: '已成交股数', reason: '原因', kind: '记录类型', order_id: '委托编号', status: '状态',
};
const valueNames: Record<string, string> = {
  main: '主板', chinext: '创业板', star: '科创板', buy: '买入', sell: '卖出', order_blocked: '模拟委托受阻', rejected: '拒绝',
  original_valid_observed_rows_not_exact_session_PIT: '原有效观察行；未证明准确交易日和历史当时可得性',
  fixed_entry_candidate_previous_open: '原入场候选保存的前一交易日开盘价',
  fixed_entry_exact_previous_session_raw_open: '原入场前准确交易日原始开盘价',
  fixed_entry_episode_structure_close: '原入场环境段固定收盘价', fixed_entry_setup_raw_low: '原入场决策日固定低点',
};
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const ruleRequirements: Record<string, string> = {
  qualified_close_and_complete_same_board_ladder: '准确收盘封板状态与完整同板制梯队',
  qualified_close_limit_status: '准确收盘封板状态',
  complete_close_amount_rank: '完整范围的收盘成交额排名',
  complete_close_sma20_sma60: '完整收盘SMA20、SMA60',
  close_vs_actual_entry_exact_previous_open: '收盘价与实际入场对应的前一交易日开盘价',
  close_vs_actual_entry_episode_structure_close: '收盘价与实际入场对应的环境段结构价',
  close_vs_actual_entry_setup_low: '收盘价与实际入场决策日固定低点',
};
const number = (value: unknown, digits = 0) => finite(value) ? fmt(value, digits) : '待核验';
const money = (value: unknown) => finite(value) ? `${fmt(value, 2)} 元` : '待核验';
const reason = (value: string) => reasonNames[value] || valueNames[value] || value;
function savedText(value: unknown): string {
  if (value == null) return '未保存，待核验';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return number(value, 6);
  if (typeof value === 'string') return reason(value);
  if (Array.isArray(value)) return value.length ? value.map(savedText).join('；') : '原记录为空';
  return Object.entries(value as SavedFields).map(([key, entry]) => `${fieldNames[key] || key}：${savedText(entry)}`).join('；');
}
function SavedEvidence({ value }: { value: SavedFields | null }) {
  return value == null ? <p className="subtle">未保存，待核验。</p>
    : Object.keys(value).length === 0 ? <p className="subtle">原记录未列出补充事实。</p>
      : <ul style={{ paddingLeft: 18, margin: '8px 0' }}>{Object.entries(value).map(([key, entry]) => <li key={key}>
        {fieldNames[key] || key}：{finite(entry) && ['amount_rank_pct', 'fixed_cutoff'].includes(key) ? `${fmt(entry * 100, 2)}%` : savedText(entry)}
      </li>)}</ul>;
}
function decisionText(value: Decision | null) {
  if (!value) return '未保存待执行决定，不能据此判断空仓';
  const action = value.kind === 'NO_CHANGE' ? '保留原状' : value.weights == null ? '目标待核验'
    : Object.keys(value.weights).length === 0 ? '空研究目标' : '已有研究目标';
  return `${action} · 决策 ${value.decision_day} / 待处理 ${value.execution_day}`;
}
export function SavedLeaderPositionDetail({ position, day }: { position: SavedLeaderPosition; day: string | null }) {
  const anchor = position.anchor, assessment = position.assessment, queued = position.queued_risk_exit;
  const quote = position.public_quote_tracking;
  return <details className="offline-details" aria-label={`持仓${position.symbol}成交与失效记录`} style={{ marginTop: 12, paddingTop: 10 }}>
    <summary>{position.symbol} · 保存持仓 {number(position.quantity_shares)} 股 · {assessment
      ? ({ invalid: '已记录结构失效', not_invalid: '本次未发现规则失效', unknown: '结构证据待核' } as Record<string, string>)[assessment.status] || '评估状态待核'
      : '未保存结构评估'}</summary>
    <div className="window-summary" style={{ marginTop: 12 }}>
      <span>账本日 {day || '待核验'}</span><span>账本日可卖 <strong>{number(position.sellable_on_book_day_shares)} 股</strong></span>
      <span>T+1未解锁 <strong>{number(position.t1_locked_shares)} 股</strong></span>
      <span>卖单占用 <strong>{number(position.sell_reserved_shares)} 股</strong></span>
      <span>保存估值价 <strong>{money(position.mark_price_cny)}</strong>{position.mark_stale ? '（原账本标记陈旧）' : ''}</span>
      <span>保存市值 <strong>{money(position.marked_value_cny)}</strong></span>
    </div>
    <p className="subtle">数量和T+1仅截至上述账本日；可卖量已扣保存卖单占用，不代表读取时或未来交易日的真实可卖数。估值价不是当前行情。</p>
    {quote && <div aria-label={`${position.symbol}持仓公开报价观察`} style={{ margin: '12px 0' }}>
      <h4>持仓公开报价观察</h4>
      <p>{finite(quote.snapshot_price_cny)
        ? `${quote.fresh_at_read ? '读取时有效的公开价' : '已过期的保存快照价'} ${money(quote.snapshot_price_cny)}；源时间 ${quote.source_quote_time || '待核'}。`
        : '没有可对应的有效保存报价，现价待核。'}</p>
      {finite(quote.price_change_from_book_percent) && <p>该快照相对账本日收盘价变动 {number(quote.price_change_from_book_percent, 2)}%；仅价格比较，不是策略收益。</p>}
      {quote.book_reference_status === 'changed_or_incompatible' && <p role="status">公开前收与账本价格不同，暂不比较涨跌；原因待核。</p>}
      <p className="subtle">原退出规则需要{ruleRequirements[quote.rule_requirement] || '保存规则所需的完整证据'}。
        盘中快照未重判收盘失效，也未新增固定止损价；已保存的退出安排继续保留。</p>
    </div>}
    <h4>实际模拟买入锚点</h4>
    {anchor?.status === 'matched_saved_FIFO_fill' ? <>
      <p>最早仍有残量的买入成交：{anchor.fill_day || '待核验'}，成交价 {money(anchor.fill_price_cny)}；对应决策日 {anchor.decision_day || '待核验'}；该笔剩余 {number(anchor.remaining_earliest_fill_shares)} 股。</p>
      <p>该笔结算可用日期 {anchor.available_day || '待核验'}；入场决策日固定低点 {money(anchor.setup_low_cny)}，固定高点 {money(anchor.setup_high_cny)}。这些结构参考不替代实际成交价。</p>
      <p className="mono subtle">成交编号 {anchor.fill_id || '待核验'}</p>
      <p className="subtle">原候选实际可得时间 {anchor.candidate_available_at || '未保存，待核验'}。</p>
      <details style={{ marginTop: 8 }}><summary>查看原入场证据</summary><SavedEvidence value={anchor.evidence} /></details>
    </> : <p>实际成交锚点待核验：{anchor?.reason ? reason(anchor.reason) : '未保存可对应的成交记录'}。不使用新候选的价格补齐。</p>}
    <h4>最后保存的结构评估</h4>
    {assessment ? <>
      <p>评估日 {assessment.day}：{({ invalid: '已记录结构失效', not_invalid: '本次未发现规则失效', unknown: '证据不足，结构退出待核' } as Record<string, string>)[assessment.status] || '状态待核验'}。
        {assessment.reasons.length > 0 ? assessment.reasons.map(reason).join('；') : '原记录没有附加原因。'}</p>
      <SavedEvidence value={assessment.facts} />
      <p className="mono subtle">原规则 {assessment.policy_id}</p>
    </> : <p>该账本日未保存结构评估，不能解释为风险检查通过。</p>}
    <p className="subtle">这是已经写入账本的评估记录，页面未重新判断当前风险。结构未知保留待核，原五交易日到期规则仍独立适用。</p>
    <p style={{ marginTop: 10 }}><strong>保存的退出安排：</strong>{queued
      ? `${queued.execution_day_elapsed ? `原计划执行日 ${queued.execution_on} 已过，账本仍保留待退出` : `${queued.execution_on} 待处理`}；${reason(queued.reason)}。仍须以实际模拟成交为准。`
      : '未保存该票排队退出；不等于已确认安全或已卖出。'}</p>
    {queued && <p className="mono subtle">退出来源 {queued.source}</p>}
  </details>;
}
function AccountDetails({ row }: { row: Account }) {
  return <div className="leader-paper-detail" style={{ maxWidth: 'min(980px, calc(100vw - 64px))', overflowWrap: 'anywhere', textAlign: 'left' }}>
    <p>独立研究账户：{row.case_id}。此处列出全部保存持仓，包括已不在新候选中的残仓；同证券在不同账户中分别复盘。</p>
    {row.positions.length ? row.positions.map(position => <SavedLeaderPositionDetail key={position.symbol} position={position} day={row.processed_day} />)
      : <p className="subtle">该已保存账户的持仓列表为空；未据此填入收益。</p>}
    <p style={{ marginTop: 12 }}><strong>待执行决定：</strong>{decisionText(row.pending_decision)}。</p>
    {row.pending_decision?.weights != null && <p>原研究权重：{Object.entries(row.pending_decision.weights).map(([symbol, weight]) => `${symbol} ${number(weight * 100, 2)}%`).join('；') || '明确的空证券目标'}。此为保存目标，尚不代表成交。</p>}
    <details className="offline-details"><summary>该账本日模拟委托受阻记录 · {row.latest_order_blocks.length} 条</summary>
      {row.latest_order_blocks.length ? <ul style={{ paddingLeft: 18 }}>{row.latest_order_blocks.map((item, index) => <li key={index}>{savedText(item)}</li>)}</ul>
        : <p>该账本日未列出受阻记录，不代表后续委托可以成交。</p>}
    </details>
    {(row.error != null || row.blocking_evidence != null) && <div role="status"><p>保存的账户问题：{savedText(row.error)}</p><p>相关证据：{savedText(row.blocking_evidence)}</p></div>}
    {row.latest_journal && <details className="offline-details"><summary>该账户最后保存记录</summary><p>提交时间 {row.last_commit_at || '待核验'}</p><p className="mono">{row.latest_journal.file}</p><p className="mono">SHA256 {row.latest_journal.sha256}</p></details>}
  </div>;
}

export function LeaderEightTypePositions({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<PositionView | null>(null), [error, setError] = useState('');
  const [guard, setGuard] = useState('two_close_envguard'), [capacity, setCapacity] = useState('causal_first_seen_cap20');
  useEffect(() => {
    const controller = new AbortController(); setView(null); setError('');
    api<PositionView>('/trading/research/leader-eight-type-positions', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [refreshVersion]);
  const rows = view?.rows.filter(row => row.guard === guard && row.capacity === capacity) || [];
  return <section className="offline-subsection leader-position-review" aria-label="八型独立模拟持仓复盘">
    <h3>八类龙型 · 独立模拟持仓复盘</h3>
    <p className="subtle">读取已经保存的研究账本、成交和失效记录。32个账户各自独立，不合并资金、持仓或权重；页面刷新不会推进账户或生成委托。</p>
    {error && <p role="status" className="window-stale">八型持仓读取失败：{error}。暂不显示旧账本替代，持仓、资金和风险状态均待核验。</p>}
    {!view && !error && <p role="status" className="subtle">正在读取已保存的八型持仓。</p>}
    {view?.status === 'unavailable' && <p role="status" className="subtle">尚无可读取的八型研究账本；资金、持仓和收益未知。</p>}
    {view?.status === 'saved_research_positions' && <>
      <div className="leader-clock"><strong>最后账本日 {view.latest_processed_day || '尚未推进'}</strong><span>独立研究账户 {number(view.case_count)} 个</span><span>最后提交 {view.last_commit_at || '未保存'}</span></div>
      <p className="subtle">全部对照中，有保存持仓的账户 {number(view.position_case_count)} 个；按账户分别记录的持仓 {number(view.case_symbol_position_count)} 项，涉及 {number(view.unique_position_symbol_count)} 只证券。相同证券不跨账户合仓。</p>
      {view.intraday_tracking && <p className="subtle" aria-label="实际持仓报价覆盖">{view.intraday_tracking.status === 'unavailable'
        ? `持仓公开报价暂不可读取：${view.intraday_tracking.error || '来源待核'}。原持仓、入场锚点和退出记录仍保留。`
        : `保存报价覆盖 ${number(view.intraday_tracking.observed_unique)} / ${number(view.intraday_tracking.unique_position_symbols)} 只实际持仓，读取时仍有效 ${number(view.intraday_tracking.current_unique)} 只；观察结束 ${view.intraday_tracking.observation_asof || '待核'}。${view.intraday_tracking.missing_symbols?.length ? `缺价：${view.intraday_tracking.missing_symbols.join('、')}。` : ''}`}</p>}
      {view.all_cases_same_day === false && <p className="window-stale">各账户处理日期不同，请以每行账本日为准，不能按同一交易日比较。</p>}
      <div className="leader-paper-controls">
        <label>持仓规则对照<select aria-label="查看八型持仓规则对照" value={guard} onChange={event => setGuard(event.target.value)}><option value="two_close_envguard">结合市场环境</option><option value="unguarded">原规则对照</option></select></label>
        <label>持仓研究名单规模<select aria-label="查看八型持仓名单规模对照" value={capacity} onChange={event => setCapacity(event.target.value)}><option value="causal_first_seen_cap20">按首次出现累计20只</option><option value="uncapped">不限制研究名单规模</option></select></label>
      </div>
      <p className="subtle" aria-live="polite">显示{guard === 'unguarded' ? '原规则' : '结合市场环境'}、{capacity === 'uncapped' ? '不限制名单规模' : '首次出现累计20只'}的 {rows.length} 个独立账户。筛选仅改变展示，没有选择或修改策略。</p>
      {rows.length > 0 && rows.every(row => row.positions.length === 0) && <p className="subtle">此组合的保存持仓均为空。{view.rows.some(row => row.positions.length > 0) ? '其他规则或名单规模对照有保存持仓，可切换查看；资金与持仓仍各自独立。' : '各账户的资金和待执行决定仍以保存记录为准。'}</p>}
      <div className="table-scroll"><table className="compact" aria-label="八型独立研究持仓账户">
        <thead><tr><th>龙型</th><th>账本处理日</th><th>持仓只数</th><th>现金</th><th>权益</th><th>保存的待执行决定</th></tr></thead>
        <tbody>{rows.map(row => <Fragment key={row.case_id}>
          <tr data-position-case-id={row.case_id}><td>{typeNames[row.type] || row.type}</td><td>{row.processed_day || '尚未推进'}</td><td>{row.positions.length}</td><td>{money(row.cash_cny)}</td><td>{money(row.equity_cny)}</td><td style={{ whiteSpace: 'normal', textAlign: 'left', minWidth: 210, maxWidth: 300 }}>{decisionText(row.pending_decision)}</td></tr>
          <tr><td colSpan={6} style={{ whiteSpace: 'normal', textAlign: 'left', maxWidth: 'none' }}><details aria-label={`${typeNames[row.type] || row.type}独立账户全部持仓`}>
            <summary>全部持仓与失效复盘 · {typeNames[row.type] || row.type} · {row.positions.length} 只</summary><AccountDetails row={row} />
          </details></td></tr>
        </Fragment>)}</tbody>
      </table></div>
      {rows.length === 0 && <p role="status" className="subtle">原账本未提供此组合，不能用其他账户替代。</p>}
      <p className="subtle" style={{ marginTop: 12 }}>这里只展示账本事实，不计算或补填收益。已保存的结构失效与待执行决定均不等于当前买卖信号；交易执行、策略选择和策略验证仍未启用。</p>
    </>}
    {view && <details className="offline-details"><summary>持仓账本来源与读取时间</summary><dl style={{ maxWidth: '100%' }}>
      <dt>研究目录</dt><dd className="mono">{view.run_directory}</dd><dt>账本文件</dt><dd className="mono">{view.head_ref?.file || '尚无可读账本'}</dd>
      <dt>文件SHA256</dt><dd className="mono">{view.head_ref?.sha256 || '待核验'}</dd><dt>账户创建时间</dt><dd>{view.head_recorded_at || '未保存'}</dd><dt>页面读取时间</dt><dd>{view.read_at}</dd>
    </dl></details>}
  </section>;
}
