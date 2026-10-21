import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ChevronLeft, ChevronRight, Eye, RefreshCw } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage } from './Common';
import { fmt } from '../types';

type Proof = { status: string; reason?: string; fees_cents?: number; account_observed_at?: string; cashflows_observed_at?: string };
type Plan = {
  id: string; symbol: string; side: string; qty: number; price_cents: number; status: string;
  contract_id: string | null; trading_date: string | null; created_at: string; updated_at: string; expires_at: string;
  account_observed_at: string; orders_observed_at: string | null; trades_observed_at: string | null;
  confirmed_filled_qty: number | null;
  confirmed_executions: { execution_id: string; qty: number; price_cents: number; notional_cents: number }[] | null;
  account_reconciliation: Proof | null; confirmed_account_reconciliation: Proof | null;
  ledger_imported: boolean; reason: string | null; error: string | null;
};
type History = { run_id: string; account_alias: string; total: number; limit: number; offset: number; plans: Plan[]; paused: boolean | null; run_error: string | null };
const states: Record<string, string> = {
  planned: '待审核计划', prefilling: '预填处理中', prefill_unknown: '预填结果未知',
  awaiting_manual_submission: '等待人工提交', submission_unknown: '提交结果未知',
  reconciliation_required: '待核对', observed_accepted: '已观察委托', observed_partial: '已观察部分成交',
  observed_filled: '已观察全部成交', observed_cancelled: '已观察撤单终态', observed_rejected: '已观察拒单',
  discarded: '计划已废弃', closed_unsubmitted: '已确认未提交',
  pending_evidence: '证据不足', matched_cash_and_positions: '现金与持仓匹配',
};
const time = (stamp?: string | null) => stamp ? new Date(stamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '--';
const status = (value?: string | null) => value ? states[value] || value : '未核对';

export function BrokerHistory({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<History | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [error, setError] = useState('');
  const detailRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError('');
    api<History>(`/trading/runs/${runId}/broker/history?limit=20&offset=${offset}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    return () => controller.abort();
  }, [runId, offset, revision]);
  const selected = data?.plans.find(plan => plan.id === selectedId);
  useEffect(() => { if (selected) detailRef.current?.scrollIntoView({ block: 'start' }); }, [selected?.id]);
  const busy = !data && !error;
  function download(plan: Plan) {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ run_id: runId, account_alias: data?.account_alias, source: 'saved_broker_observations', plan, client_interacted: false, submission_enabled: false }, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `broker-receipt-${plan.id}.json`; link.click(); URL.revokeObjectURL(url);
  }
  return <section className="trade-section" aria-label="保存的计划与回读">
    <div className="broker-history-heading"><h2>保存的计划与回读</h2><button className="icon-button" title="重读保存记录" aria-label="重读保存记录" disabled={busy} onClick={() => setRevision(value => value + 1)}><RefreshCw size={17} /></button></div>
    {error && <ErrorMessage message={error} />}
    {busy && <div className="empty small-empty">读取保存记录</div>}
    {data && <>
      <div className="window-summary"><span>账户 {data.account_alias}</span><span>自动提交关闭</span><span>桌面写入 {data.paused === null ? '状态未知' : data.paused ? '已暂停' : '未暂停'}</span><span>本机未核验 · 保存观察</span></div>
      {data.run_error && <ErrorMessage message={data.run_error} />}
      {data.plans.length ? <div className="table-scroll"><table><thead><tr><th>交易日</th><th>证券</th><th>方向</th><th>计划股数</th><th>已确认股数</th><th>限价</th><th>计划状态</th><th>合同号</th><th>账户核对</th><th>唯一账本</th><th>详情</th></tr></thead><tbody>{data.plans.map(plan => <tr key={plan.id}>
        <td>{plan.trading_date || '--'}</td><td>{plan.symbol}</td><td>{plan.side === 'buy' ? '买入' : '卖出'}</td><td>{fmt(plan.qty, 0)}</td><td>{plan.confirmed_filled_qty == null ? '--' : fmt(plan.confirmed_filled_qty, 0)}</td><td>{fmt(plan.price_cents / 100, 2)}</td>
        <td>{status(plan.status)}{plan.status === 'planned' && new Date(plan.expires_at).getTime() <= Date.now() && <span className="window-case-reason">已过期</span>}</td><td>{plan.contract_id || '--'}</td><td>{status(plan.account_reconciliation?.status)}</td><td>{plan.ledger_imported ? '已入账' : '未入账'}</td>
        <td><button className="icon-button" title="查看保存回读" aria-label={`查看 ${plan.id} 回读`} aria-pressed={selectedId === plan.id} onClick={() => setSelectedId(selectedId === plan.id ? '' : plan.id)}><Eye size={16} /></button></td>
      </tr>)}</tbody></table></div> : <div className="empty small-empty">没有保存的计划</div>}
      <div className="broker-history-heading"><span>{data.total ? `${offset + 1}-${Math.min(offset + data.plans.length, data.total)} / ${data.total}` : '0 条'}</span><div className="actions">
        <button className="icon-button" title="上一页回读" aria-label="上一页回读" disabled={offset === 0 || busy} onClick={() => setOffset(Math.max(0, offset - 20))}><ChevronLeft size={17} /></button>
        <button className="icon-button" title="下一页回读" aria-label="下一页回读" disabled={offset + data.plans.length >= data.total || busy} onClick={() => setOffset(offset + 20)}><ChevronRight size={17} /></button>
      </div></div>
    </>}
    {selected && <div ref={detailRef} className="broker-history-detail">
      <div className="broker-history-heading"><h3>回读 {selected.id.slice(0, 8)}</h3><button className="icon-button" title="下载保存回读" aria-label="下载保存回读" onClick={() => download(selected)}><ArrowDownToLine size={17} /></button></div>
      <dl><dt>账户期初观察（UTC+8）</dt><dd>{time(selected.account_observed_at)}</dd><dt>委托 / 成交观察（UTC+8）</dt><dd>{time(selected.orders_observed_at)} / {time(selected.trades_observed_at)}</dd><dt>计划有效至（UTC+8）</dt><dd>{time(selected.expires_at)}</dd><dt>保存更新时间（UTC+8）</dt><dd>{time(selected.updated_at)}</dd><dt>最近账户核对</dt><dd>{status(selected.account_reconciliation?.status)}</dd><dt>上次通过的累计费用证据</dt><dd>{selected.confirmed_account_reconciliation?.fees_cents == null ? '--' : `${fmt(selected.confirmed_account_reconciliation.fees_cents / 100, 2)} 元`}</dd><dt>费用证据账户 / 流水时间（UTC+8）</dt><dd>{time(selected.confirmed_account_reconciliation?.account_observed_at)} / {time(selected.confirmed_account_reconciliation?.cashflows_observed_at)}</dd><dt>唯一账本</dt><dd>{selected.ledger_imported ? '已入账' : '未入账'}</dd></dl>
      {(selected.reason || selected.error || selected.account_reconciliation?.reason) && <ErrorMessage message={[selected.reason, selected.error, selected.account_reconciliation?.reason].filter(Boolean).join(' · ')} />}
      <h3>保留的已确认成交</h3>
      {selected.confirmed_executions?.length ? <div className="table-scroll"><table><thead><tr><th>成交号</th><th>股数</th><th>价格</th><th>毛成交额</th></tr></thead><tbody>{selected.confirmed_executions.map(fill => <tr key={fill.execution_id}><td>{fill.execution_id}</td><td>{fmt(fill.qty, 0)}</td><td>{fmt(fill.price_cents / 100, 2)}</td><td>{fmt(fill.notional_cents / 100, 2)}</td></tr>)}</tbody></table></div> : <div className="empty small-empty">{selected.confirmed_executions == null ? '成交明细尚无确认记录' : '已确认明细为空'}</div>}
    </div>}
  </section>;
}
