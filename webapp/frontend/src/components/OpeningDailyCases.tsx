import { useEffect, useState } from 'react';
import { RefreshCw, X } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';
import { OpeningDailyLedger } from './OpeningDailyLedger';
import { OpeningDailyThsReview } from './OpeningDailyThsReview';
import { SavedLeaderPositionDetail } from './LeaderEightTypePositions';
import type { SavedLeaderPosition } from './LeaderEightTypePositions';

type SavedRow = Record<string, unknown>;
type Ref = { path: string; sha256: string };
type SavedCase = {
  case_id: string; strategy_id: string | null; status: string; trade_status: string | null;
  intent: (SavedRow & { kind?: string | null; weights?: Record<string, number> | null; remaining_exit_symbols?: string[] }) | null;
  cash_rmb: number | string | null; equity_rmb: number | string | null;
  positions: SavedRow[] | null; targets: SavedRow[] | null;
  reason?: string; saved_source?: { published_input?: { source_day: string; execution_day: string;
    published_at: string; decision_kind: string; reason?: string; candidate_symbols: string[] } | null };
  orders: (SavedRow & { preflight?: SavedRow | null })[] | null;
  preflight: SavedRow | null; release_blockers: unknown; missing: unknown; refs: unknown;
  holding_review_status?: string;
  holding_review?: { processed_day: string; positions: SavedLeaderPosition[]; latest_order_blocks: SavedRow[] } | null;
};
type DetailView = {
  schema: string; read_at: string; cases: SavedCase[];
  route: { id: string; family: string; status: string; stale: boolean; source_changed: boolean;
    source_day: string | null; execution_day: string | null; committed_at: string | null;
    error: string | null; source_head: Ref | null; product_source_head: Ref | null };
};
const value = (item: unknown) => item == null ? '—' : typeof item === 'object' ? JSON.stringify(item) : String(item);
const numeric = (item: unknown, decimals = 0) => {
  if (item == null || item === '') return '—';
  const number = Number(item);
  return Number.isFinite(number) ? fmt(number, decimals) : value(item);
};
const percent = (item: unknown) => item == null || item === '' ? '—' : Number.isFinite(Number(item)) ? `${fmt(Number(item) * 100, 2)}%` : value(item);
const decisionNames: Record<string, string> = { NO_CHANGE: '保持原目标', FULL_TARGET: '完整目标', EXIT_ALL: '退出目标',
  UNBOUND_FAMILY_RULE: '待账户确认的原规则', RANKED_CANDIDATES: '按原顺序保存的候选' };
const stateNames: Record<string, string> = { paper_preview_only: '已保存纸面预览', not_ready: '资料不足，方案未完成', NO_TRADE: '本日不调仓', PAPER_TARGET_PENDING: '保存目标待执行', paper_candidates_only: '纸面候选' };
const actionNames: Record<string, string> = { saved_queued_risk_exit: '原计划退出', saved_pending_full_target: '保存的完整目标' };
const typeNames: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙', trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙代理', cross_regime_candidate: '穿越龙候选', rebound_repair_candidate: '反包修复候选', sector_catchup_leader: '补涨龙代理', turnover_leader: '换手龙代理' };
const preflightNames: Record<string, string> = { accepted: '预检接受', rejected: '预检拒绝', submitted: '预检接受（复制账本）' };
const reasonNames: Record<string, string> = { insufficient_cash: '资金不足', insufficient_sellable: '可卖数量不足', invalid_quantity: '申报数量不合规则', invalid_lot: '申报单位不合规则', sell_minimum_200_or_full_exit: '卖出不足200股且非全额退出' };
const sizingStates: Record<string, string> = {
  blocked_below_minimum_buy_quantity: '参考预算不足最低买量', blocked_missing_research_quote: '缺研究参考价',
  blocked_research_close_not_tradable: '参考日交易状态受限', blocked_quantity_rule: '数量规则不符',
  paper_preview_not_created: '已有参考数量，未形成可提交单', no_change: '数量保持不变', integer_lot_target: '按申报单位估算目标',
};
const sizingNotes: Record<string, string> = {
  target_budget_below_minimum_buy_notional_at_saved_close: '按保存收盘价计算，目标预算低于最低买入参考金额；目标数量为零。',
  saved_close_sizing_fees_slippage_and_execution_quote_not_checked: '按保存收盘价估算，尚未检查费用、滑点和执行时报价。',
  no_change_exact_saved_quantity: '保持保存的原持仓数量，不重新分配买入预算。',
  carry_forward_scope_excluded: '范围外持仓沿用原数量，不重新分配买入预算。',
  saved_remaining_full_exit: '保留原计划退出，不按新买入预算估量。',
  saved_full_target_exit: '完整目标已移除该证券，计划退出。',
  saved_full_target_quote_unknown: '缺少准确来源日参考价，目标数量待核。',
};
const noBuySizingActions = new Set(['no_change_exact_saved_quantity', 'carry_forward_scope_excluded', 'saved_remaining_full_exit', 'saved_full_target_exit']);
function sizingNumber(row: SavedRow, field: string, decimals = 0) {
  const item = row[field];
  if (item == null || item === '') return field in row && noBuySizingActions.has(value(row.target_action)) ? '不适用' : '待核';
  return numeric(item, decimals);
}
function originalReason(reason: unknown) {
  const original = value(reason);
  return reasonNames[original] ? `${reasonNames[original]}（${original}）` : original;
}
const caseLabel = (id: string) => Object.entries(typeNames).reduce((label, [key, name]) => label.replace(key, name), id);
const hasMissing = (missing: unknown) => Array.isArray(missing) ? missing.length > 0 : missing != null && missing !== '';
function SavedTable({ rows, label, children }: { rows: SavedRow[] | null; label: string; children: React.ReactNode }) {
  return rows == null ? <p className="window-stale">未保存{label}明细，不能凭其他案例补齐。</p>
    : rows.length === 0 ? <p className="subtle">已保存的{label}为0行。</p>
      : <div className="table-scroll">{children}</div>;
}

export function OpeningDailyCases({ routeId, label, sourceSha, productSha, close }: {
  routeId: string; label: string; sourceSha: string | null; productSha: string | null; close: () => void;
}) {
  const [view, setView] = useState<DetailView | null>(null);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setView(null); setError('');
    api<DetailView>(`/trading/research/opening-daily/${encodeURIComponent(routeId)}`, { signal: controller.signal })
      .then(next => {
        if (controller.signal.aborted) return;
        if (!next.route) throw new Error('本路线暂无可读取的保存方案。');
        if (next.route.id !== routeId || (next.route.source_head?.sha256 ?? null) !== sourceSha
          || (next.route.product_source_head?.sha256 ?? null) !== productSha) throw new Error('路线来源已变化，请先刷新上方日更状态。');
        setView(next); setSelected(current => next.cases.some(item => item.case_id === current) ? current : next.cases[0]?.case_id || '');
      })
      .catch((problem: Error) => { if (!controller.signal.aborted) { setView(null); setError(problem.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [routeId, sourceSha, productSha, version]);
  const item = view?.cases.find(row => row.case_id === selected);
  return <section className="opening-daily-case-panel" aria-label={`${label}逐案方案`} aria-busy={busy}>
    <div className="opening-daily-heading"><h3>{label} · 逐案目标与委托</h3><div className="actions">
      <button className="icon-button" aria-label="刷新逐案保存明细" title="只读刷新明细" disabled={busy} onClick={() => setVersion(v => v + 1)}><RefreshCw size={16} /></button>
      <button className="icon-button" aria-label="收起逐案方案" title="收起" onClick={close}><X size={17} /></button>
    </div></div>
    {busy && <p className="subtle" role="status">正在读取本路线保存明细…</p>}
    {error && <p className="window-stale" role="status">明细读取失败：{error}</p>}
    {view && <>
      <p className="subtle">来源日 {view.route.source_day || '—'} · 计划执行日 {view.route.execution_day || '—'} · {view.cases.length}个独立案例。切换案例只改变查看内容。</p>
      {view.route.stale && <p className="window-stale" data-case-source="stale">正在查看旧方案；当前来源已有变化或不可读取，以下不是新来源的操作方案。</p>}
      {view.route.error && <p className="window-stale">{view.route.error}</p>}
      <label className="opening-daily-case-select">查看案例<select aria-label={`${label}查看案例`} value={selected} onChange={event => setSelected(event.target.value)}>
        {view.cases.map(row => <option key={row.case_id} value={row.case_id}>{caseLabel(row.case_id)}</option>)}
      </select></label>
      {item && <div data-opening-case={item.case_id}>
        <p className="opening-daily-case-identity"><strong>{caseLabel(item.case_id)}</strong><br /><span className="subtle">策略 {item.strategy_id || '—'} · {stateNames[item.status] || item.status}</span></p>
        {view.route.family === 'registered_research' && item.status !== 'paper_preview_only' && <section aria-label="已发表研究输入">
          <p className="window-stale">账户目标尚未形成，资金、目标数量和委托保持待核。</p>
          {item.saved_source?.published_input ? <>
            <p className="subtle">原输入于 {item.saved_source.published_input.published_at} 发表，来源日 {item.saved_source.published_input.source_day}，计划执行日 {item.saved_source.published_input.execution_day}。</p>
            <p>原输入类型：{decisionNames[item.saved_source.published_input.decision_kind] || item.saved_source.published_input.decision_kind}；研究候选：{item.saved_source.published_input.candidate_symbols.length ? item.saved_source.published_input.candidate_symbols.join('、') : '原输入未列候选'}。</p>
          </> : <p className="subtle">尚无本路线的实际发表记录。</p>}
          {item.reason && <details className="offline-details"><summary>查看原缺项原因</summary><p className="opening-daily-wrapped">{item.reason}</p></details>}
        </section>}
        <dl className="opening-daily-facts">
          <div><dt>保存决策</dt><dd data-case-field="decision">{item.intent?.kind ? decisionNames[item.intent.kind] || item.intent.kind : '—'}</dd></div>
          <div><dt>调仓状态</dt><dd>{item.trade_status ? stateNames[item.trade_status] || item.trade_status : '—'}</dd></div>
          <div><dt>来源日模拟现金（元）</dt><dd data-case-field="cash">{numeric(item.cash_rmb, 2)}</dd></div>
          <div><dt>来源日模拟权益（元）</dt><dd>{numeric(item.equity_rmb, 2)}</dd></div>
        </dl>
        {item.intent?.kind === 'NO_CHANGE' && <p className="subtle opening-daily-case-note">保持原目标不等于空仓；原已排队的退出单仍按下表保留。</p>}
        {view.route.family === 'native_preopen' && <p className="subtle opening-daily-case-note">原计划原因：{value(item.intent?.reason)}；阶段未知证券：{value(item.intent?.stage_unknown_symbols)}。两种模型独立记账。</p>}
        <OpeningDailyThsReview key={`review:${item.case_id}:${sourceSha}:${productSha}`} routeId={routeId} caseId={item.case_id} sourceDay={view.route.source_day} executionDay={view.route.execution_day} sourceSha={sourceSha} productSha={productSha} />
        <OpeningDailyLedger key={`${item.case_id}:${sourceSha}:${productSha}`} routeId={routeId} caseId={item.case_id} sourceDay={view.route.source_day} executionDay={view.route.execution_day} sourceSha={sourceSha} productSha={productSha} />
        <h4>保存持仓</h4>
        <SavedTable rows={item.positions} label="持仓"><table className="compact" aria-label="案例保存持仓"><thead><tr><th>证券</th><th>持仓（股）</th><th>来源日可卖（股）</th></tr></thead><tbody>{item.positions?.map((row, index) => <tr key={`${value(row.symbol)}-${index}`}><td>{value(row.symbol)}</td><td>{numeric(row.quantity)}</td><td>{numeric(row.sellable_on_source_day)}</td></tr>)}</tbody></table></SavedTable>
        {item.holding_review_status === 'saved_product_bound' && item.holding_review ? <section aria-label="本方案保存的持仓理由与退出记录">
          <p className="subtle">以下成交锚点、入场依据和退出记录与本方案一起保存；截至账本日 {item.holding_review.processed_day}，读取时没有改用新账本或重评风险。</p>
          {item.holding_review.positions.map(position => <SavedLeaderPositionDetail key={position.symbol} position={position} day={item.holding_review!.processed_day} />)}
          <details className="offline-details"><summary>本方案账本日模拟委托受阻 · {item.holding_review.latest_order_blocks.length} 条</summary>
            {item.holding_review.latest_order_blocks.length ? item.holding_review.latest_order_blocks.map((row, index) => <p key={index} className="opening-daily-wrapped">{value(row.symbol)} · {originalReason(row.reason)} · 原记录 {value(row)}</p>)
              : <p className="subtle">该日未保存受阻记录，不代表后续委托可以成交。</p>}
          </details>
        </section> : ['eight_type', 'dated_catchup'].includes(view.route.family)
          ? <p className="window-stale">原方案未保存持仓理由与退出复盘，不能读取当前账本补到旧方案中。</p> : null}
        <h4>计划目标</h4>
        <p className="subtle opening-daily-case-note">预算来自保存的模拟权益与目标权重，不能当作可用现金。参考金额按来源日收盘价计算，不含费用与滑点，也不是下一开盘报价。保持原数量、退出与范围保留不计算新买入预算；旧表缺字段时保留待核。</p>
        <SavedTable rows={item.targets} label="目标表"><table className="compact opening-sizing-table" aria-label="案例计划目标"><thead><tr><th>证券</th><th>原持仓（股）</th><th>目标（股）</th><th>变化（股）</th><th>目标权重 / 预算（元）</th><th>收盘参考价（元）</th><th>最低买量 / 增量（股）<br />最低参考金额（元）</th><th>动作 / 原原因</th><th>数量形成说明</th></tr></thead><tbody>{item.targets?.map((row, index) => <tr key={`${value(row.symbol)}-${index}`} data-target-sizing-status={value(row.sizing_status)}>
          <td>{value(row.symbol)}</td><td>{numeric(row.current_qty)}</td><td>{numeric(row.target_qty)}</td><td>{numeric(row.delta_qty)}</td>
          <td>{percent(row.target_weight)}<br />{sizingNumber(row, 'target_budget_rmb', 2)}</td>
          <td>{numeric(row.close_rmb, 2)}</td>
          <td>{sizingNumber(row, 'minimum_buy_quantity')} / {sizingNumber(row, 'quantity_step')}<br />{sizingNumber(row, 'minimum_buy_notional_rmb', 2)}</td>
          <td className="opening-daily-wrapped">{actionNames[value(row.target_action)] || value(row.target_action ?? row.reason ?? row.status)}</td>
          <td className="opening-daily-wrapped"><strong>{row.sizing_status == null || row.sizing_status === '' ? '数量状态待核' : sizingStates[value(row.sizing_status)] || value(row.sizing_status)}</strong><br />{row.sizing_note == null || row.sizing_note === '' ? '未保存数量形成说明，待核。' : sizingNotes[value(row.sizing_note)] || value(row.sizing_note)}
            <details><summary>原数量说明与口径</summary><p className="subtle">{value({ sizing_status: row.sizing_status, sizing_note: row.sizing_note, sizing_reference_basis: row.sizing_reference_basis, budget_below_minimum_buy_notional: row.budget_below_minimum_buy_notional })}</p></details>
          </td>
        </tr>)}</tbody></table></SavedTable>
        <h4>候选委托与原预检结果</h4>
        <p className="subtle opening-daily-case-note">价格为研究收盘参考价。预检是在复制账本上完成的检查，接受记录不代表账户已报单或成交。</p>
        <SavedTable rows={item.orders} label="候选委托"><table className="compact" aria-label="案例候选委托与拒绝原因"><thead><tr><th>证券 / 方向</th><th>数量（股）</th><th>参考价（元）</th><th>原预检</th><th>拒绝原因</th><th>生成依据</th></tr></thead><tbody>{item.orders?.map((row, index) => <tr key={`${value(row.symbol)}-${value(row.side)}-${index}`} data-preflight-status={value(row.preflight?.preflight_status)}><td>{value(row.symbol)}<br />{row.side === 'buy' ? '买入' : row.side === 'sell' ? '卖出' : value(row.side)}</td><td>{numeric(row.qty)}</td><td>{numeric(row.price_rmb, 2)}</td><td>{preflightNames[value(row.preflight?.preflight_status)] || value(row.preflight?.preflight_status)}</td><td className="opening-daily-wrapped">{originalReason(row.preflight?.preflight_reason)}</td><td className="opening-daily-wrapped">{actionNames[value(row.reason)] || value(row.reason)}</td></tr>)}</tbody></table></SavedTable>
        {(item.status === 'not_ready' || hasMissing(item.missing)) && <p className="window-stale">保存缺项：{value(item.missing)}</p>}
        <details className="offline-details" aria-label="案例原保存记录"><summary>原始决策、限制与引用</summary><dl className="opening-daily-case-audit">
          <dt>原组合意图</dt><dd>{value(item.intent)}</dd><dt>原预检摘要</dt><dd>{value(item.preflight)}</dd>
          <dt>未满足的执行资料</dt><dd>{value(item.release_blockers)}</dd><dt>案例文件引用</dt><dd>{value(item.refs)}</dd>
          <dt>页面读取时间</dt><dd>{view.read_at}</dd>
        </dl></details>
      </div>}
      {!view.cases.length && <p className="window-stale">此路线没有可读取的保存案例；不按其他路线补齐。</p>}
    </>}
  </section>;
}
