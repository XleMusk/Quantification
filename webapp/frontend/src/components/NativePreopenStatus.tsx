import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';

type SavedRecord = Record<string, unknown>;
type ModelPlan = { model_kind: string; decision: string; reason: string | null;
  weights: Record<string, number> | null; target_cash_weight?: number | null;
  selected_count: number | null; scope_complete: boolean | null };
type Order = { id?: string; day: string; symbol: string; side: string; qty: number;
  filled: number; limit_price: number; price_unit?: string; status: string; reason: string | null };
type Fill = { order_id: string; day: string; symbol: string; side: string; qty: number;
  price: number; price_unit?: string; fees: number | null; available_day: string };
type Account = { model_kind: string; context: SavedRecord; last_processed_day: string | null;
  cash_cents: number | null; cash_available_cents?: number | null; equity_cents: number | null;
  positions: Record<string, { quantity: number; available: number | null }>; order_count: number;
  fill_count: number; orders?: Record<string, Order>; fills?: Record<string, Fill>;
  blocked_order_count?: number; blocked_orders?: { sequence: number; day: string; symbol: string; side?: string; qty?: number; reason?: string }[];
  daily: SavedRecord[]; metrics: SavedRecord | null };
type StatusView = { schema: string; status: string; checked_at?: string; read_at?: string;
  source_day: string | null; execution_day: string | null; plan_published_at: string | null;
  policy_id: string | null; model_generation: unknown; unknown_stage_symbols: string[] | null;
  models: ModelPlan[]; account_count: number | null; accounts: Account[];
  cash_cents: number | null; equity_cents: number | null; return_metrics: null;
  stage_scope?: null | { raw_stage_membership_complete: boolean | null; raw_unknown_stage_symbols: string[] | null;
    excluded_no_limit_symbols: string[] | null; remaining_unknown_stage_symbols: string[] | null;
    scored_no_limit_conflicts: string[] | null; qualified_stage_membership_complete: boolean | null;
    decision_as_of: string | null; qualification_inputs: unknown; source_ref: unknown };
  source_refs: Record<string, unknown>;
  observation: null | { status: string; read_at?: string; observed_at?: string;
    observed_at_basis?: string; evidence_level?: string; missing_source_days?: string[]; required_execution_day?: string | null;
    error?: unknown; blocking_evidence?: unknown; stale_for_current_head: boolean };
  error?: string };
type PolicyView = { policy_id: string; label: string; scope: string; view: StatusView | null };
type ComparisonView = { schema: string; checked_at?: string; read_at?: string;
  policy_count: number; policies: PolicyView[] };

const modelName = (kind: string) => kind === 'lr' ? '逻辑回归 LR' : kind === 'lgbm' ? 'LightGBM' : kind;
const text = (value: unknown) => value == null ? '—' : typeof value === 'string' ? value : JSON.stringify(value);
const money = (cents?: number | null) => cents == null ? '—' : `${fmt(cents / 100, 2)} 元`;
const price = (value: number, unit?: string) => unit && !['cent', 'mill'].includes(unit)
  ? `${value}（${unit}）` : `${fmt(value / (unit === 'mill' ? 1000 : 100), unit === 'mill' ? 3 : 2)} 元`;
const sideName = (side: string) => side === 'buy' ? '买入' : side === 'sell' ? '卖出' : side;
const statusNames: Record<string, string> = {
  unavailable: '尚无已保存方案', unknown_saved_evidence: '已保存证据待核验',
  waiting_for_execution_source: '等待准确执行日收盘源，尚未推进模拟账本',
  active_research_books: '已保存独立模拟账本', blocked_or_failed_research_books: '本次推进受阻或失败',
  saved_preopen_plan_only: '已保存隔夜方案，尚无推进观察',
};
const evidenceNames: Record<string, string> = {
  legacy_cli_summary_only: '旧 CLI 摘要；不是当前执行回读',
  full_saved_runner_result: '已保存的完整模拟推进回执',
  command_failure_progress_unknown: '命令失败记录；本次推进进度未知',
};
const reasonNames: Record<string, string> = {
  stage_membership_incomplete_whole_day_NO_CHANGE: '阶段归属未完全核验，整日保持不变',
  fixed_native_stagewise_top10pct_daily_top5: '按保存的分阶段前10%与合并前5规则',
};
const orderNames: Record<string, string> = { submitted: '已报', partial: '部分成交', filled: '全部成交',
  rejected: '被拒绝', cancelled: '已撤销', cancel_pending: '撤销待确认' };
const observationNames: Record<string, string> = {
  waiting_for_exact_complete_raw_close: '等待准确执行日收盘源',
  blocked_required_raw_reference_or_IPO_evidence: '准确价格参考或上市资格证据不足，未提交账本',
  failed_engine_session_no_commit: '本次模拟推进失败，未提交试算账本',
  advanced_isolated_native_research_books: '已推进两个独立模拟账本',
  already_consumed_original_plan_prefix: '原方案已处理，本次未重复执行',
};
function weightsText(plan: ModelPlan) {
  if (plan.decision === 'NO_CHANGE') return '不变更持仓；目标未定，不能解释为空仓';
  if (plan.weights == null) return '未保存目标，待核验';
  return Object.keys(plan.weights).length === 0 ? '明确的空研究目标，仍须以模拟账本结果为准'
    : Object.entries(plan.weights).map(([symbol, weight]) => `${symbol} ${fmt(weight * 100, 2)}%`).join('；');
}
function AccountDetails({ account }: { account: Account }) {
  const holdings = Object.entries(account.positions);
  const orders = account.orders && Object.entries(account.orders);
  const fills = account.fills && Object.entries(account.fills);
  return <details className="offline-details" aria-label={`${account.model_kind}模拟账户明细`}>
    <summary>{modelName(account.model_kind)} · 持仓、委托与逐笔成交</summary>
    <p className="subtle" style={{ overflowWrap: 'anywhere', marginTop: 10 }}>独立研究账户 {text(account.context)}；最后处理 {account.last_processed_day || '—'}。数量与可卖数按保存账本展示，不合并两个模型的持仓或资金。</p>
    <div className="window-summary" style={{ marginTop: 12 }}><span>现金 <strong>{money(account.cash_cents)}</strong></span><span>可用现金 <strong>{money(account.cash_available_cents)}</strong></span><span>权益 <strong>{money(account.equity_cents)}</strong></span><span>收益 <strong>—</strong></span><span>委托 / 逐笔成交 <strong>{account.order_count} / {account.fill_count}</strong></span><span>被阻止的操作 <strong>{account.blocked_order_count == null ? '—' : account.blocked_order_count}</strong></span></div>
    <div className="table-scroll"><table className="compact" aria-label={`${account.model_kind}保存持仓`}><thead><tr><th>证券</th><th>持仓（股）</th><th>可卖（股）</th></tr></thead><tbody>{holdings.map(([symbol, row]) => <tr key={symbol}><td>{symbol}</td><td>{fmt(row.quantity, 0)}</td><td>{row.available == null ? '—' : fmt(row.available, 0)}</td></tr>)}</tbody></table></div>
    {holdings.length === 0 && <p className="subtle">该已创建模拟账户当前保存持仓为空。</p>}
    <details className="offline-details" aria-label={`${account.model_kind}原始模拟委托`}><summary>原始模拟委托 · {account.order_count} 笔</summary>
      {orders ? orders.length > 0 ? <div className="table-scroll"><table className="compact" aria-label={`${account.model_kind}委托逐笔列表`}><thead><tr><th>委托编号 / 日期</th><th>证券 / 方向</th><th>委托 / 已成交（股）</th><th>限价</th><th>状态 / 原因</th></tr></thead><tbody>{orders.map(([id, row]) => <tr key={id}><td style={{ whiteSpace: 'normal', overflowWrap: 'anywhere', minWidth: 150 }}>{id}<br />{row.day}</td><td>{row.symbol}<br />{sideName(row.side)}</td><td>{fmt(row.qty, 0)} / {fmt(row.filled, 0)}</td><td>{price(row.limit_price, row.price_unit)}</td><td style={{ whiteSpace: 'normal' }}>{orderNames[row.status] || row.status}<br />{row.reason || '—'}</td></tr>)}</tbody></table></div> : <p className="subtle">已保存委托为 0 笔，不据此计算收益。</p> : <p className="subtle">当前回执未提供委托明细，不能凭数量重建委托。</p>}
    </details>
    <details className="offline-details" aria-label={`${account.model_kind}原始模拟成交`}><summary>逐笔模拟成交 · {account.fill_count} 笔</summary>
      {fills ? fills.length > 0 ? <div className="table-scroll"><table className="compact" aria-label={`${account.model_kind}成交逐笔列表`}><thead><tr><th>成交编号 / 委托编号</th><th>日期 / 证券</th><th>方向 / 股数</th><th>成交价</th><th>本笔费用</th><th>可卖日</th></tr></thead><tbody>{fills.map(([id, row]) => <tr key={id}><td style={{ whiteSpace: 'normal', overflowWrap: 'anywhere', minWidth: 150 }}>{id}<br />{row.order_id}</td><td>{row.day}<br />{row.symbol}</td><td>{sideName(row.side)} / {fmt(row.qty, 0)}</td><td>{price(row.price, row.price_unit)}</td><td>{money(row.fees)}</td><td>{row.available_day}</td></tr>)}</tbody></table></div> : <p className="subtle">已保存成交为 0 笔，不等于收益为 0。</p> : <p className="subtle">当前回执未提供逐笔成交，不能把一笔委托当作一笔成交。</p>}
    </details>
    {account.blocked_orders && <details className="offline-details" aria-label={`${account.model_kind}被阻止的模拟操作`}><summary>被阻止的模拟操作 · {account.blocked_order_count} 条</summary>
      <p className="subtle">这些是原事件记录，可能尚未形成委托；委托和成交为 0 不代表无信号或收益为 0。</p>
      {account.blocked_orders.length > 0 && <div className="table-scroll"><table className="compact" aria-label={`${account.model_kind}受阻事件列表`}><thead><tr><th>原序号 / 日期</th><th>证券 / 方向</th><th>原数量（股）</th><th>原阻止原因</th></tr></thead><tbody>{account.blocked_orders.map(row => <tr key={row.sequence}><td>{row.sequence} / {row.day}</td><td>{row.symbol} / {row.side ? sideName(row.side) : '—'}</td><td>{row.qty == null ? '—' : fmt(row.qty, 0)}</td><td style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{row.reason || '未保存原因'}</td></tr>)}</tbody></table></div>}
    </details>}
  </details>;
}

function PolicyPanel({ policy }: { policy: PolicyView }) {
  const view = policy.view;
  const observation = view?.observation;
  const scope = view?.stage_scope;
  const unknownSymbols = scope?.raw_unknown_stage_symbols ?? view?.unknown_stage_symbols;
  const symbolsText = (symbols?: string[] | null) => symbols == null ? '待核验' : symbols.length ? symbols.join('、') : '无（仅本方案范围）';
  return <article aria-label={`${policy.label}保存状态`} data-policy-id={policy.policy_id} data-policy-scope={policy.scope}
    data-status={view?.status || 'unavailable'} style={{ minWidth: 0, overflowWrap: 'anywhere', borderTop: '1px solid #e3e8ed', paddingTop: 16 }}>
    <h4 style={{ fontSize: 14, margin: 0 }}>{policy.label}</h4>
    {!view && <p className="window-stale" role="status">此版本回执缺失，不采用另一版本补齐。</p>}
    {view && <>
      <p role="status" style={{ marginTop: 14 }}>{statusNames[view.status] || '保存状态待核验'}</p>
      <div className="window-summary" style={{ marginTop: 12 }}><span>背景收盘日 <strong>{view.source_day || '—'}</strong></span><span>计划执行日 <strong>{view.execution_day || '—'}</strong></span><span>已保存账户 <strong>{view.account_count == null ? '待核验' : view.account_count}</strong></span></div>
      <p className="subtle">方案实际发表 {view.plan_published_at || '—'}；页面核对 {view.checked_at || view.read_at || '—'}。</p>
      <p className="subtle" style={{ marginTop: 10 }} data-scope-field="raw-unknown">原始阶段未知（原方案范围）：{symbolsText(unknownSymbols)}。原始分数与阶段记录保留，不回写为已知阶段。</p>
      {scope ? scope.qualified_stage_membership_complete != null ? <>
        <p className="subtle" data-scope-field="excluded">已证实不限价新股排除：{symbolsText(scope.excluded_no_limit_symbols)}。</p>
        <p className="subtle" data-scope-field="remaining">资格排除后剩余未知：{symbolsText(scope.remaining_unknown_stage_symbols)}；已评分不限价冲突：{symbolsText(scope.scored_no_limit_conflicts)}。</p>
        <p className="subtle">仅应用已保存的不限价排除，不改九项特征、概率或排名；不代表全体 IPO、公司行动或历史资格均已验证。</p>
      </> : <p className="subtle">此版本使用原始阶段范围；后来取得的不限价信息不倒写或重算旧方案。</p>
        : <p className="subtle">资格范围资料待核验，不借用另一版本补齐。</p>}
      {view.error && <p className="window-stale">保存证据问题：{view.error}</p>}
      {observation && <p className="subtle" style={{ marginTop: 10 }}>保存的推进观察：{observationNames[observation.status] || observation.status}；{observation.observed_at_basis === 'saved_before_CLI_call_clock_not_exact_finish' ? '原 CLI 调用前记录（完成时刻未保存）' : '观察记录'} {observation.observed_at || observation.read_at || '—'}。{observation.missing_source_days?.length ? `缺少准确来源日 ${observation.missing_source_days.join('、')}。` : ''}{observation.required_execution_day ? `所需执行日 ${observation.required_execution_day}。` : ''}{observation.stale_for_current_head ? '此观察早于当前账本，不代表当前仍处于旧等待状态。' : ''}</p>}
      {!observation && <p className="subtle" data-observation-state="none" style={{ marginTop: 10 }}>尚无本版本运行观察；不借用另一版本的等待或成交回执。</p>}
      {observation?.error != null && <p className="window-stale">原观察错误：{text(observation.error)}</p>}
      {view.models.length > 0 && <details className="offline-details" aria-label="原生短线已保存方案" open><summary>两模型已保存方案 · 目标与缺项</summary>
        <div className="table-scroll" style={{ marginTop: 12 }}><table className="compact" aria-label="原生短线两模型方案" style={{ tableLayout: 'fixed' }}><thead><tr><th style={{ width: '19%', whiteSpace: 'normal' }}>模型</th><th style={{ width: '24%', whiteSpace: 'normal' }}>保存决策</th><th style={{ whiteSpace: 'normal' }}>目标 / 研究预算比例</th></tr></thead><tbody>{view.models.map(plan => <tr key={plan.model_kind} data-model-kind={plan.model_kind}><td style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{modelName(plan.model_kind)}</td><td style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{plan.decision === 'NO_CHANGE' ? '不变更持仓' : plan.decision === 'FULL_TARGET' ? '研究目标' : plan.decision}<br /><span className="subtle">{plan.selected_count == null ? '—' : plan.selected_count} 只 / {plan.scope_complete === true ? '本方案范围完整' : plan.scope_complete === false ? '范围不完整' : '范围待核验'}</span></td><td style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{weightsText(plan)}<br />{plan.weights != null && plan.target_cash_weight != null && <span data-weight-budget="saved">研究预算比例：股票 {fmt(Object.values(plan.weights).reduce((sum, weight) => sum + weight, 0) * 100, 2)}% / 现金 {fmt(plan.target_cash_weight * 100, 2)}%；不是账户现金。<br /></span>}<span className="subtle" title={plan.reason || undefined}>{plan.reason ? reasonNames[plan.reason] || plan.reason : '未保存原因'}</span></td></tr>)}</tbody></table></div>
      </details>}
      {view.account_count == null ? <p data-account-state="unknown" className="window-stale">账户证据待核验；不能判断账户数量、现金、权益或成交，不使用旧成功结果补齐。</p>
        : view.account_count === 0 ? <div data-account-state="not-created" style={{ marginTop: 16 }}><p>尚未创建模拟账户；当前没有已保存账户成交结果。</p><div className="window-summary" style={{ marginTop: 10 }}><span>现金 <strong>—</strong></span><span>权益 <strong>—</strong></span><span>收益 <strong>—</strong></span></div><p className="subtle">计划资金预算不等于账户现金；不变更持仓的未知目标不等于空仓。页面刷新不会建账或提交委托。</p></div>
        : <div data-account-state="created">{view.accounts.map(account => <AccountDetails key={account.model_kind} account={account} />)}</div>}
      <p className="subtle" style={{ marginTop: 14 }}>本线路为隔离的历史回放模拟，不是券商成交。公司行动与历史资格边界仍未核验，不提供策略收益；不变更持仓不触发主动再平衡，原有强制退出纪律另行保留。</p>
      <details className="offline-details" aria-label="原生短线保存证据来源"><summary>保存证据与核对时间</summary><dl style={{ gridTemplateColumns: 'minmax(85px, 28%) minmax(0, 1fr)' }}><dt>证据级别</dt><dd>{observation?.evidence_level ? evidenceNames[observation.evidence_level] || observation.evidence_level : '尚无本版本运行观察'}</dd><dt>固定方法</dt><dd>{view.policy_id || '—'}</dd><dt>资格判断截点</dt><dd>{scope?.decision_as_of || '—'}</dd><dt>资格输入引用</dt><dd>{text(scope?.qualification_inputs)}</dd><dt>冻结模型代际</dt><dd>{text(view.model_generation)}</dd><dt>原受阻证据</dt><dd>{text(observation?.blocking_evidence)}</dd><dt>文件引用</dt><dd>{text(view.source_refs)}</dd><dt>边界</dt><dd>selection_enabled=false · execution_admitted=false；仅读文件，不调用评分、模拟或订单操作。</dd></dl></details>
    </>}
  </article>;
}

export function NativePreopenStatus({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<ComparisonView | null>(null), [error, setError] = useState('');
  const [version, setVersion] = useState(0), [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); setBusy(true); setError('');
    api<ComparisonView>('/trading/research/native-preopen-comparison', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [refreshVersion, version]);
  return <section className="offline-subsection native-preopen-status" aria-label="原生短线盘前模拟状态" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}><div><h3>原生短线 · 两版隔夜方案与模拟账本</h3><p className="subtle">原始方案与资格排除方案分别保存；每个模型、每个版本的账户独立，不自动选择或合并仓位。</p></div><button aria-label="刷新原生短线保存状态" onClick={() => setVersion(value => value + 1)} disabled={busy}><RefreshCw size={15} />{busy ? '读取中' : '刷新保存状态'}</button></div>
    {error && <p className="window-stale" role="status">本卡读取失败：{error}。不采用旧成功回执替代，不影响其他研究卡。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取保存证据，不启动评分、模拟或行情采集。</p>}
    {view && <><p className="subtle" style={{ marginTop: 12 }}>同一证券在不同模型、版本中的权重不能相加；目标权重不是已建持仓，真实账户未接入。</p><div data-policy-layout="comparison" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 480px), 1fr))', alignItems: 'start', gap: 24, marginTop: 16 }}>{view.policies.map(policy => <PolicyPanel key={policy.policy_id} policy={policy} />)}</div></>}
  </section>;
}
