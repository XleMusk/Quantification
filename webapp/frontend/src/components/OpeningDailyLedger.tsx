import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';
import { OpeningPlanReconciliation } from './OpeningPlanReconciliation';
import type { PlanExecutionReconciliation, PlanReconciliationHistory } from './OpeningPlanReconciliation';

type SavedRow = Record<string, unknown>;
type Ref = { path: string; sha256: string };
type TargetDiagnostic = SavedRow & {
  projection?: {
    target_budget_rmb?: number | null; minimum_buy_notional_rmb?: number | null;
    budget_below_minimum_buy_notional?: boolean | null;
  } | null;
};
type LedgerView = {
  schema: string; status: string; reason?: string | null; read_at: string;
  route_id: string; case_id: string; strategy_id: string | null;
  source_day: string | null; planned_execution_day: string | null; ledger_day: string | null;
  source_head: Ref | null; product_source_head: Ref | null; stale: boolean;
  source_binding_status: string | null; context: unknown; context_basis?: string;
  cash: { total_cents: number | null; available_cents: number | null; equity_cents?: number | null } | null;
  positions: SavedRow[] | null; orders: SavedRow[] | null; fills: SavedRow[] | null;
  order_blocks?: SavedRow[] | null; order_count: number | null; fill_count: number | null;
  blocked_count?: number | null; daily_summary: SavedRow | null;
  target_execution_diagnostics?: TargetDiagnostic[] | null;
  target_execution_diagnostic_count?: number | null;
  executed_decision: (SavedRow & { source_day?: string | null; execution_day?: string | null }) | null;
  executed_decision_reason?: string | null; read_references?: Ref[];
  plan_execution_reconciliation?: PlanExecutionReconciliation | null;
  latest_plan_reconciliation?: PlanExecutionReconciliation | null;
  last_executed_plan_reconciliation?: PlanExecutionReconciliation | null;
  plan_reconciliation_history?: PlanReconciliationHistory | null;
};
const text = (value: unknown) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const number = (value: unknown, decimals = 0) => typeof value === 'number' && Number.isFinite(value) ? fmt(value, decimals) : '—';
const money = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? fmt(value / 100, 2) : '—';
const quote = (value: unknown, unit: unknown) => typeof value !== 'number' || !Number.isFinite(value) ? '—'
  : unit === 'cent' ? fmt(value / 100, 2) : unit === 'mill' ? fmt(value / 1000, 3) : `${value}（单位待核）`;
const side = (value: unknown) => value === 'buy' ? '买入' : value === 'sell' ? '卖出' : text(value);
const states: Record<string, string> = { submitted: '已报模拟单', partial: '模拟部分成交', filled: '模拟全部成交', rejected: '模拟拒绝', cancelled: '模拟已撤销', cancel_pending: '模拟撤销待确认' };
const diagnosticNumber = (value: unknown, decimals = 0) => typeof value === 'number' && Number.isFinite(value) ? fmt(value, decimals) : '待核';
const diagnosticReasons: Record<string, string> = {
  below_minimum_no_entry: '数量取整后未形成新买入', target_below_minimum_exit: '原持仓对应目标数量取整为零',
  new_entry: '新建目标持仓', unchanged_quantity: '目标数量未变化', internal_reweight: '组合内部调整',
  independent_risk_only_hold_quantity: '独立退出规则下保留原数量', target_removed_exit: '原目标移除，计划退出',
  forced_risk_exit: '风险规则要求退出', residual_full_exit: '继续退出残余持仓',
  independent_replay_risk_exit: '独立研究退出规则触发', exposure_cap_reduction: '持仓上限要求减仓',
};
const diagnosticFlags: Record<string, string> = {
  sellable_T1: 'T+1可卖数量限制', exposure_caps: '持仓上限限制', legal_order_quantity: '申报数量取整',
  cash_fee_reservation: '现金及费用预留限制', prior_volume_participation: '前日成交量参与限制',
  missing_quote: '缺少开盘行情', insufficient_cash: '可用资金不足',
  minimum_commission_internal_reweight: '小额内部调整因最低佣金暂缓',
};
const diagnosticLabel = (value: unknown, names: Record<string, string>) => {
  const raw = text(value);
  return names[raw] ? `${names[raw]}（${raw}）` : value == null ? '待核' : raw;
};
function TargetExecutionDiagnostics({ rows, count }: { rows: TargetDiagnostic[] | null | undefined; count: number | null | undefined }) {
  return <section aria-label="目标如何形成委托">
    <h4>目标如何形成委托 · {diagnosticNumber(count)} 条</h4>
    <p className="subtle opening-daily-case-note">以下只解释账本日保存的数量计算。目标预算按当时模拟权益和权重计算，不能当作可用现金；最低参考金额未含费用与滑点，保存参考价也不是券商报价。</p>
    {rows == null ? <p className="window-stale">该账本未保存目标数量计算记录，不能从0笔委托推断为资金不足、保持原仓或清仓。</p>
      : rows.length === 0 ? <p className="subtle">该账本保存的目标数量计算为0条，原因仍以原执行决策为准。</p>
        : <div className="table-scroll"><table className="compact opening-sizing-table" aria-label="已处理日目标数量计算"><thead><tr>
          <th>证券 / 目标权重</th><th>目标预算（元）</th><th>保存参考价（元）</th><th>最低买量 / 增量（股）<br />最低参考金额（元）</th><th>原持仓 → 引擎目标（股）</th><th>已请求 / 已成交（股）</th><th>原原因与约束</th>
        </tr></thead><tbody>{rows.map((row, index) => <tr key={`${text(row.symbol)}-${index}`} data-target-diagnostic-reason={text(row.mechanical_reason)}>
          <td>{text(row.symbol)}<br /><span className="subtle">{typeof row.target_weight === 'number' && Number.isFinite(row.target_weight) ? `${fmt(row.target_weight * 100, 2)}%` : '权重待核或不适用'}</span></td>
          <td>{diagnosticNumber(row.projection?.target_budget_rmb, 2)}<br /><span className="subtle">开盘权益 {money(row.open_equity_cents)}</span></td>
          <td>{quote(row.engine_mark, row.price_unit)}<br /><span className="subtle">{row.has_execution_open === true ? '原记录有执行日开盘价' : row.has_execution_open === false ? '原记录缺执行日开盘价' : '开盘价状态待核'}</span></td>
          <td>{diagnosticNumber(row.minimum_buy_quantity)} / {diagnosticNumber(row.quantity_step)}<br />{diagnosticNumber(row.projection?.minimum_buy_notional_rmb, 2)}{row.projection?.budget_below_minimum_buy_notional === true ? <><br /><span className="subtle">预算低于最低参考金额</span></> : null}</td>
          <td>{diagnosticNumber(row.held_before)} → {diagnosticNumber(row.engine_target_quantity)}</td>
          <td>{diagnosticNumber(row.requested_quantity)} / {diagnosticNumber(row.filled_quantity)}</td>
          <td className="opening-daily-wrapped">{diagnosticLabel(row.mechanical_reason, diagnosticReasons)}<br /><span className="subtle">{Array.isArray(row.constraint_flags) ? row.constraint_flags.length ? row.constraint_flags.map(flag => diagnosticLabel(flag, diagnosticFlags)).join('；') : '原记录无额外约束标记' : '约束标记待核'}</span>
            <details><summary>查看原记录与关联委托</summary><p className="subtle">委托编号：{row.order_ids == null ? '待核' : text(row.order_ids)}</p><p className="subtle">{text(row)}</p></details>
          </td>
        </tr>)}</tbody></table></div>}
  </section>;
}

export function OpeningDailyLedger({ routeId, caseId, sourceDay, executionDay, sourceSha, productSha }: {
  routeId: string; caseId: string; sourceDay: string | null; executionDay: string | null;
  sourceSha: string | null; productSha: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [view, setView] = useState<LedgerView | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setBusy(true); setView(null); setError('');
    api<LedgerView>(`/trading/research/opening-daily/${encodeURIComponent(routeId)}/cases/${encodeURIComponent(caseId)}/ledger`, { signal: controller.signal })
      .then(next => {
        if (controller.signal.aborted) return;
        if (next.route_id !== routeId || next.case_id !== caseId || next.source_day !== sourceDay
          || next.planned_execution_day !== executionDay
          || (next.source_head?.sha256 ?? null) !== sourceSha
          || (next.product_source_head?.sha256 ?? null) !== productSha) throw new Error('账本与当前案例或保存方案的来源不一致，请刷新上方方案。');
        if (next.ledger_day != null && next.ledger_day !== sourceDay) throw new Error('账本完成日期与方案来源日不一致，保留待核。');
        setView(next);
      })
      .catch((problem: Error) => { if (!controller.signal.aborted) { setView(null); setError(problem.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [expanded, routeId, caseId, sourceDay, executionDay, sourceSha, productSha, version]);
  return <section className="opening-ledger" aria-label="案例模拟账本回读" aria-busy={busy}>
    <button className="opening-daily-case-button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起模拟账本回读' : '查看最近已处理日的模拟委托与成交'}</button>
    {expanded && <div className="opening-ledger-content">
      <div className="opening-daily-heading"><h4>已处理日的模拟账本回读</h4><button className="icon-button" aria-label="刷新本案例模拟账本回读" title="只读刷新账本" disabled={busy} onClick={() => setVersion(value => value + 1)}><RefreshCw size={16} /></button></div>
      {busy && <p className="subtle" role="status">正在读取本案例已保存账本…</p>}
      {error && <p className="window-stale" role="status">账本回读失败：{error}</p>}
      {view && <div data-ledger-case={view.case_id} data-ledger-status={view.status}>
        <p className="subtle opening-ledger-date">最近账本日 <strong data-ledger-field="day">{view.ledger_day || '—'}</strong>；上方 {view.planned_execution_day || '—'} 仍是待执行预案。以下是保存的模拟记录，尚不是券商成交。</p>
        {view.stale && <p className="window-stale">当前来源已变化；只保留能与原方案对应的账本，缺失时不读取新账本补齐。</p>}
        {view.status === 'unavailable' && <p className="window-stale" role="status">原账本记录尚不可对应：{view.reason || '未保存准确来源'}。</p>}
        <dl className="opening-daily-facts">
          <div><dt>期末总现金（元）</dt><dd data-ledger-field="cash">{money(view.cash?.total_cents)}</dd></div>
          <div><dt>账本日可用现金（元）</dt><dd>{money(view.cash?.available_cents)}</dd></div>
          <div><dt>账本模拟权益（元）</dt><dd>{money(view.cash?.equity_cents)}</dd></div>
          <div><dt>原日汇总费用（元）</dt><dd data-ledger-field="fees">{number(view.daily_summary?.fees, 2)}</dd></div>
          <div><dt>当日委托 / 逐笔成交</dt><dd data-ledger-field="counts">{number(view.order_count)} / {number(view.fill_count)}</dd></div>
        </dl>
        <p className="subtle opening-daily-case-note">现金为原期末余额；可用现金扣除未成交买单预留。委托费用是累计值，逐笔成交费用是增量，不重复累加。</p>
        {!view.daily_summary && view.status !== 'unavailable' && <p className="subtle">原日汇总未保存；仅展示已对应的账户快照和逐笔记录，不补日汇总费用。</p>}
        {view.executed_decision ? <p className="subtle" data-ledger-field="decision">原决策 {view.executed_decision.source_day || '—'} → 执行 {view.executed_decision.execution_day || '—'}。</p>
          : <p className="subtle" data-ledger-field="decision">原执行决策尚未准确对应，不使用上方新候选补齐。</p>}
        <OpeningPlanReconciliation latest={view.latest_plan_reconciliation !== undefined ? view.latest_plan_reconciliation : view.plan_execution_reconciliation}
          executed={view.last_executed_plan_reconciliation} history={view.plan_reconciliation_history} />
        <h4>账本日持仓与可卖</h4>
        {view.positions == null ? <p className="window-stale">持仓明细待核。</p> : view.positions.length === 0 ? <p className="subtle">该已处理账本日持仓为0行。</p>
          : <div className="table-scroll"><table className="compact" aria-label="账本日持仓与可卖"><thead><tr><th>证券</th><th>持仓（股）</th><th>账本日可卖（股）</th><th>保存估值价（元）</th></tr></thead><tbody>{view.positions.map((row, index) => <tr key={`${text(row.symbol)}-${index}`}><td>{text(row.symbol)}</td><td>{number(row.quantity)}</td><td>{number(row.sellable_on_ledger_day)}</td><td>{quote(row.mark_price, row.price_unit)}</td></tr>)}</tbody></table></div>}
        <TargetExecutionDiagnostics rows={view.target_execution_diagnostics} count={view.target_execution_diagnostic_count} />
        <h4>当日原模拟委托</h4>
        {view.orders == null ? <p className="window-stale">委托明细待核，不能判断为0笔。</p> : view.orders.length === 0 ? <p className="subtle">该已处理账本日保存委托为0笔。</p>
          : <div className="table-scroll"><table className="compact" aria-label="当日原模拟委托"><thead><tr><th>委托编号 / 日期</th><th>证券 / 方向</th><th>委托 / 成交（股）</th><th>限价（元）</th><th>累计费用（元）</th><th>状态 / 原因</th></tr></thead><tbody>{view.orders.map((row, index) => <tr key={`${text(row.order_id ?? row.id)}-${index}`}><td className="opening-ledger-id">{text(row.order_id ?? row.id)}<br />{text(row.day)}</td><td>{text(row.symbol)}<br />{side(row.side)}</td><td>{number(row.qty)} / {number(row.filled)}</td><td>{quote(row.limit_price, row.price_unit)}</td><td>{money(row.fees)}</td><td className="opening-daily-wrapped">{states[text(row.status)] || text(row.status)}<br />{text(row.reason)}</td></tr>)}</tbody></table></div>}
        <h4>当日逐笔模拟成交</h4>
        {view.fills == null ? <p className="window-stale">逐笔成交明细待核，不能按委托数补齐。</p> : view.fills.length === 0 ? <p className="subtle">该已处理账本日保存逐笔成交为0笔，不据此补零收益。</p>
          : <div className="table-scroll"><table className="compact" aria-label="当日逐笔模拟成交"><thead><tr><th>成交编号 / 委托编号</th><th>日期 / 证券 / 方向</th><th>股数</th><th>成交价（元）</th><th>本笔增量费用（元）</th><th>可用日期</th></tr></thead><tbody>{view.fills.map((row, index) => <tr key={`${text(row.fill_id ?? row.id)}-${index}`}><td className="opening-ledger-id">{text(row.fill_id ?? row.id)}<br /><span className="subtle">{text(row.order_id)}</span></td><td>{text(row.day)}<br />{text(row.symbol)} / {side(row.side)}</td><td>{number(row.qty)}</td><td>{quote(row.price, row.price_unit)}</td><td>{money(row.fees)}</td><td>{text(row.available_day)}</td></tr>)}</tbody></table></div>}
        <h4>当日模拟受阻事件 · {number(view.blocked_count)}</h4>
        {view.order_blocks == null ? <p className="window-stale">受阻事件明细待核。</p> : view.order_blocks.length === 0 ? <p className="subtle">原当日事件中未列出受阻记录。</p>
          : <div className="table-scroll"><table className="compact" aria-label="当日模拟受阻事件"><thead><tr><th>日期</th><th>证券 / 方向</th><th>数量</th><th>原受阻原因</th></tr></thead><tbody>{view.order_blocks.map((row, index) => <tr key={index}><td>{text(row.day)}</td><td>{text(row.symbol)} / {side(row.side)}</td><td>{number(row.qty)}</td><td className="opening-daily-wrapped">{text(row.reason)}</td></tr>)}</tbody></table></div>}
        <details className="offline-details"><summary>账本来源与原执行决策</summary><dl className="opening-daily-case-audit">
          <dt>独立账户上下文</dt><dd>{text(view.context)}</dd><dt>上下文依据</dt><dd>{text(view.context_basis)}</dd><dt>来源对应状态</dt><dd>{text(view.source_binding_status)}</dd>
          <dt>原执行决策</dt><dd>{text(view.executed_decision)}</dd><dt>决策缺项</dt><dd>{text(view.executed_decision_reason)}</dd>
          <dt>账本引用</dt><dd>{text(view.read_references)}</dd><dt>读取时间</dt><dd>{view.read_at}</dd>
        </dl></details>
      </div>}
    </div>}
  </section>;
}
