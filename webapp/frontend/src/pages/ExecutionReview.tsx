import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, RefreshCw, Upload } from 'lucide-react';
import { api, post } from '../api';
import { ErrorMessage, Loading } from '../components/Common';
import './ExecutionReview.css';

type View = { observed: boolean; row_count: number | null; observed_at: string | null; trading_date: string | null; freshness: string; age_seconds: number | null; source: string | null };
type Review = {
  status: string; generated_at: string; selected_run_id: string | null; adapter_paused?: boolean | null;
  runs: { id: string; name: string; mode: string; status: string; account_alias: string | null; channel: string | null; error: string | null; bound: boolean }[];
  accounts: { account_alias: string; views: Record<string, View>; status: string; issues: string[]; cash_total_rmb: number | null; cash_available_rmb: number | null; cash_frozen_rmb: number | null }[];
  plans: { id: string; account_alias: string; symbol: string; side: string; qty: number; status: string; trading_date: string | null; confirmed_filled_qty: number | null; confirmed_execution_count: number | null; account_reconciliation: {status?: string; reason?: string} | null; confirmed_account_reconciliation: {status?: string} | null; ledger_imported: boolean; reason: string | null; error: string | null }[];
  issues: string[]; workflow: { label: string; command: string }[]; calibration: {status: string; reason: string};
};
type CostRow = { account_alias: string; trading_date: string; execution_id: string; symbol: string; side: string; qty: number; price_rmb: number; status: string; reference_price_rmb: number | null; reference_age_seconds: number | null; slippage_bps: number | null; fees_rmb: number | null; all_in_cost_rmb: number | null };
type CostResult = { status: string; fill_count: number; matched_count: number; unmatched_count: number; unused_reference_count: number; rows: CostRow[]; input_fingerprint: string; notes: string[]; groups: {reference_kind: string; side: string; fill_count: number; quantity: number; reference_notional_rmb: number; slippage_bps: number; all_in_cost_bps: number | null; fees_known_count: number; fees_rmb: number | null}[] };
const views: Record<string, string> = { account: '账户与持仓', orders: '当日委托', trades: '当日成交', cashflows: '累计资金流水' };
const statuses: Record<string, string> = { planned: '已保存计划', awaiting_manual_submission: '等待人工提交', prefill_unknown: '预填结果待核对', submission_unknown: '提交结果待核对', reconciliation_required: '需核对', observed_partial: '已观察部分成交', observed_filled: '已观察全部成交', observed_cancelled: '已观察撤单', observed_rejected: '已观察拒单', matched_cash_and_positions: '现金与持仓已匹配', pending_evidence: '资料待补', reference_missing: '无对应参考价', reference_after_fill: '参考价晚于成交', reference_too_old: '参考价间隔过长', matched: '已匹配', partial: '部分匹配', no_matched_fills: '暂无匹配成交' };
const referenceNames: Record<string, string> = {arrival: '到达价', decision: '决策价', midpoint: '同期中间价'};
const number = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits });
const fillHeader = 'account_alias,trading_date,execution_id,symbol,side,qty,price_rmb,executed_at,fees_rmb\n';
const quoteHeader = 'account_alias,trading_date,execution_id,symbol,reference_at,reference_price_rmb,reference_kind,source\n';
function download(name: string, content: string, type = 'text/csv;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ExecutionReview() {
  const [review, setReview] = useState<Review | null>(null);
  const [run, setRun] = useState(''); const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const [fills, setFills] = useState(''); const [quotes, setQuotes] = useState(''); const [fillName, setFillName] = useState(''); const [quoteName, setQuoteName] = useState('');
  const [age, setAge] = useState(300); const [cost, setCost] = useState<CostResult | null>(null); const [calculating, setCalculating] = useState(false); const [costError, setCostError] = useState('');
  const inputRevision = useRef(0); const calculationRevision = useRef(0);
  const fileReadRevision = useRef({fills: 0, quotes: 0});
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; calculationRevision.current++; }; }, []);
  function invalidateCost() { inputRevision.current++; setCost(null); setCostError(''); }
  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError('');
    try { const value = await api<Review>(`/execution-review${run ? `?run_id=${encodeURIComponent(run)}` : ''}`, { signal }); if (!signal?.aborted) setReview(value); }
    catch (error) { if (!signal?.aborted) { setReview(null); setError((error as Error).message); } }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [run]);
  useEffect(() => { const abort = new AbortController(); void refresh(abort.signal); return () => abort.abort(); }, [refresh]);
  async function loadFile(event: React.ChangeEvent<HTMLInputElement>, kind: 'fills' | 'quotes') {
    const file = event.target.files?.[0]; if (!file) return;
    const readRevision = ++fileReadRevision.current[kind];
    invalidateCost();
    // A newly selected file is not usable until its own read finishes. This
    // prevents submitting the previous file while a replacement is loading.
    if (kind === 'fills') { setFills(''); setFillName(''); } else { setQuotes(''); setQuoteName(''); }
    try {
      if (file.size > 2_000_000) throw Error('每张 CSV 最大 2 MB，请使用指定表头的 UTF-8 文件');
      const value = await file.text();
      if (!mounted.current || readRevision !== fileReadRevision.current[kind]) return;
      invalidateCost();
      if (kind === 'fills') { setFills(value); setFillName(file.name); } else { setQuotes(value); setQuoteName(file.name); }
    } catch (error) { if (mounted.current && readRevision === fileReadRevision.current[kind]) { invalidateCost(); setCostError((error as Error).message); } }
  }
  async function calculate(event: React.FormEvent) {
    event.preventDefault(); if (!fills || !quotes || calculating) return;
    const revision = inputRevision.current; const calculation = ++calculationRevision.current;
    setCalculating(true); setCost(null); setCostError('');
    try {
      const value = await post<CostResult>('/execution-review/calibration', {currency: 'CNY', price_basis: 'raw_rmb', quantity_unit: 'share', evidence_source: 'broker_export', fills_csv: fills, quotes_csv: quotes, max_reference_age_seconds: age});
      if (mounted.current && revision === inputRevision.current && calculation === calculationRevision.current) setCost(value);
    }
    catch (error) { if (mounted.current && revision === inputRevision.current && calculation === calculationRevision.current) setCostError((error as Error).message); }
    finally { if (mounted.current && calculation === calculationRevision.current) setCalculating(false); }
  }
  const observed = review?.accounts.reduce((count, account) => count + Object.values(account.views).filter(value => value.observed).length, 0) ?? 0;
  return <div className="execution-review">
    <header className="page-header"><div><h1>成交与账户复核</h1><p>保存观察 · 同一账本对账 · 实际执行成本</p></div><button onClick={() => void refresh()} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />重读保存记录</button></header>
    {error && <ErrorMessage message={error} />}
    <div className="execution-review-toolbar"><label>账户范围<select aria-label="成交复核运行" value={run} onChange={event => setRun(event.target.value)}><option value="">全部已保存观察</option>{review?.runs.map(item => <option value={item.id} key={item.id}>{item.name} · {item.account_alias || '未绑定观察账户'}</option>)}</select></label><span>适配器暂停：{review?.adapter_paused == null ? '未记录' : review.adapter_paused ? '是' : '否'}</span><span>读取时点：{review?.generated_at ? new Date(review.generated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '—'}</span></div>
    <div className="metrics"><div><span>保存的观察账户</span><strong>{review?.accounts.length ?? '—'}</strong></div><div><span>已观察视图</span><strong>{review ? observed : '—'}</strong></div><div><span>保存计划（最多100条）</span><strong>{review?.plans.length ?? '—'}</strong></div><div><span>真实自动提交</span><strong className="metric-text">关闭</strong></div></div>
    {loading && !review ? <Loading /> : <>
      {review?.issues.map(value => <p className="execution-note" key={value}>{value}</p>)}
      <section className="execution-section"><h2>账户、委托、成交与资金流水</h2><p className="execution-note">空表只在已保存的真实观察中计为 0；未观察显示“—”。观察时间与当前时间分别保留，历史资料可用于复盘。</p>
        {!review?.accounts.length ? <p className="execution-empty">暂无保存的四表观察。使用现有导出包流程准备资料后，在这里重读。</p> : review.accounts.map(account => <div className="execution-account" key={account.account_alias}>
          <div className="execution-account-heading"><h3>{account.account_alias}</h3><span>资金余额 {number(account.cash_total_rmb)} 元</span><span>冻结 {number(account.cash_frozen_rmb)} 元</span><span>可用 {number(account.cash_available_rmb)} 元</span></div>
          <div className="table-scroll"><table><thead><tr><th>视图</th><th>保存状态</th><th>记录数</th><th>交易日</th><th>实际观察时间</th><th>时间状态</th><th>来源</th></tr></thead><tbody>{Object.entries(views).map(([kind, label]) => { const value = account.views[kind]; return <tr key={kind}><td>{label}</td><td>{value.observed ? '已观察' : '未观察'}</td><td>{number(value.row_count, 0)}</td><td>{value.trading_date ?? '—'}</td><td>{value.observed_at ?? '—'}</td><td>{value.freshness === 'fresh' ? '5分钟内' : value.freshness === 'historical' ? '历史观察' : value.observed ? '时间待核对' : '—'}</td><td>{value.source ?? '—'}</td></tr>; })}</tbody></table></div>
          {account.issues.map(issue => <p className="execution-note" key={issue}>{issue}</p>)}
        </div>)}
      </section>
      <section className="execution-section"><h2>保存的成交与对账结果</h2><div className="table-scroll"><table><thead><tr><th>计划 / 账户</th><th>证券</th><th>方向 / 计划股数</th><th>状态</th><th>确认成交股数</th><th>当前现金持仓核对</th><th>此前通过记录</th><th>所选账本入账</th><th>待处理原因</th></tr></thead><tbody>{review?.plans.map(plan => <tr key={plan.id}><td title={plan.id}>{plan.id.slice(0, 10)}<small>{plan.account_alias}</small></td><td>{plan.symbol}</td><td>{plan.side === 'buy' ? '买入' : '卖出'} / {number(plan.qty, 0)}</td><td>{statuses[plan.status] ?? plan.status}</td><td>{number(plan.confirmed_filled_qty, 0)}</td><td>{plan.account_reconciliation ? statuses[plan.account_reconciliation.status || ''] ?? plan.account_reconciliation.status : '—'}</td><td>{plan.confirmed_account_reconciliation ? statuses[plan.confirmed_account_reconciliation.status || ''] ?? plan.confirmed_account_reconciliation.status : '—'}</td><td>{run ? plan.ledger_imported ? '已入账' : '未见该运行入账标记' : '选择运行后查看'}</td><td>{plan.error || plan.reason || plan.account_reconciliation?.reason || '—'}</td></tr>)}</tbody></table></div>{!review?.plans.length && <p className="execution-empty">暂无保存计划；没有据此认定券商账户没有委托或成交。</p>}</section>
    </>}
    <section className="execution-section"><div className="execution-section-heading"><h2>实际成交成本预览</h2>{cost && <button onClick={() => download('execution_cost_preview.json', JSON.stringify(cost, null, 2), 'application/json')}><Download size={14} />导出结果</button>}</div>
      <p className="execution-note">{review?.calibration.reason ?? '上传实际成交及对应的同期原价参考资料。'} 金额单位为元，数量为股，价格须未复权；仅计算本次预览。</p>
      <form onSubmit={calculate}><div className="execution-upload-grid"><label><span><Upload size={14} />实际逐笔成交 CSV</span><input aria-label="实际逐笔成交 CSV" type="file" accept=".csv" disabled={calculating} onChange={event => void loadFile(event, 'fills')} /><small>{fillName || 'UTF-8，最多10000行 / 2 MB；费用未知可空'}</small></label><label><span><Upload size={14} />对应参考价 CSV</span><input aria-label="对应参考价 CSV" type="file" accept=".csv" disabled={calculating} onChange={event => void loadFile(event, 'quotes')} /><small>{quoteName || '按账户、交易日、成交号、证券精确匹配'}</small></label><label>允许参考价间隔（秒）<input aria-label="允许参考价间隔秒数" type="number" min={1} max={3600} value={age} disabled={calculating} onChange={event => { setAge(Number(event.target.value)); invalidateCost(); }} required /></label></div>
        <div className="execution-calibration-actions"><button type="button" onClick={() => download('execution_fills_template.csv', fillHeader)}><Download size={13} />成交表头</button><button type="button" onClick={() => download('execution_references_template.csv', quoteHeader)}><Download size={13} />参考表头</button><button type="submit" className="primary" disabled={!fills || !quotes || calculating}>{calculating ? '计算中…' : '计算执行成本'}</button></div>
      </form>
      <details className="execution-schema"><summary>字段格式与计算口径</summary><p>成交字段：<code>{fillHeader.trim()}</code></p><p>参考字段：<code>{quoteHeader.trim()}</code></p><p>证券示例 600000.SSE；方向 buy / sell；时间带时区，例如 2026-10-19T10:00:00+08:00。reference_kind 为 arrival（到达价）、decision（决策价）或 midpoint（同期中间价），source 填实际来源。</p><p>买入成交高于参考价、卖出成交低于参考价记为正成本。每组以参考成交额加权；不同方向、参考类型分别统计。fees_rmb 仅填写有明确逐笔归属的实际费用，委托累计费用不自动平均分摊。</p></details>
      {costError && <ErrorMessage message={costError} />}
      {cost && <div className="execution-cost-results"><p className="execution-cost-heading">{statuses[cost.status] ?? cost.status} · 匹配 {cost.matched_count} / {cost.fill_count} 笔 · 待匹配 {cost.unmatched_count} 笔 · 未使用参考 {cost.unused_reference_count} 条</p>
        <div className="table-scroll"><table><thead><tr><th>参考类型</th><th>方向</th><th>匹配笔数</th><th>参考成交额（元）</th><th>不利滑点（bps）</th><th>实际费用（元）</th><th>含费用成本（bps）</th></tr></thead><tbody>{cost.groups.map(group => <tr key={`${group.reference_kind}:${group.side}`}><td>{referenceNames[group.reference_kind]}</td><td>{group.side === 'buy' ? '买入' : '卖出'}</td><td>{group.fill_count}</td><td>{number(group.reference_notional_rmb)}</td><td>{number(group.slippage_bps, 3)}</td><td>{number(group.fees_rmb)}</td><td>{number(group.all_in_cost_bps, 3)}</td></tr>)}</tbody></table></div>
        <div className="table-scroll"><table><thead><tr><th>账户 / 成交号</th><th>证券 / 方向</th><th>股数</th><th>成交价</th><th>参考价</th><th>时间差（秒）</th><th>匹配状态</th><th>滑点（bps）</th><th>费用（元）</th></tr></thead><tbody>{cost.rows.map(row => <tr key={`${row.account_alias}:${row.trading_date}:${row.execution_id}`}><td>{row.account_alias}<small>{row.execution_id}</small></td><td>{row.symbol} / {row.side === 'buy' ? '买' : '卖'}</td><td>{number(row.qty, 0)}</td><td>{number(row.price_rmb, 3)}</td><td>{number(row.reference_price_rmb, 3)}</td><td>{number(row.reference_age_seconds, 1)}</td><td>{statuses[row.status] ?? row.status}</td><td>{number(row.slippage_bps, 3)}</td><td>{number(row.fees_rmb)}</td></tr>)}</tbody></table></div>
        <p className="execution-note">输入版本 {cost.input_fingerprint.slice(0, 20)} · 未写入账户或研究费用配置</p>{cost.notes.map(note => <p className="execution-note" key={note}>{note}</p>)}
      </div>}
    </section>
    <section className="execution-section"><h2>沿用现有导出与同一账本流程</h2><p className="execution-note">以下是本机资料准备与预览命令。页面重读只使用已保存资料，原生客户端核验需在本机连接可用时完成。</p>{review?.workflow.map(step => <details className="execution-command" key={step.label}><summary>{step.label}</summary><code>{step.command}</code></details>)}</section>
  </div>;
}
