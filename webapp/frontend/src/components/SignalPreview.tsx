import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, Eye, RefreshCw } from 'lucide-react';
import { post } from '../api';
import { ErrorMessage } from './Common';
import { fmt } from '../types';
import type { Config } from '../tradingTypes';

type Preview = {
  status: string; reason?: string; account_alias: string;
  account_observed_at?: string; execution_config?: Config;
  strategy_signal: { execution_day: string; signal_frame: { day: string; source: string } };
  weight_sizing?: { equity_cents: number; targets: { symbol: string; target_weight: number; target_qty: number }[] };
  adjustments?: { symbol: string; held_qty: number; target_qty: number; delta_qty: number; side: string | null; status: string; validation_reason?: string }[];
  next_plan: { id: string; symbol: string; side: string; qty: number; price_cents: number; expires_at: string } | null;
};
const states: Record<string, string> = { awaiting_review: '待审核预览', blocked: '受限', no_change: '无调仓', next_plan: '下一笔', waiting_for_prior_step: '等待前序', waiting_for_sales: '等待卖出回读' };
const reasons: Record<string, string> = { not_rebalance_session: '非调仓日', insufficient_cash: '可用资金不足（含费用）', insufficient_sellable_T1: '可卖数量不足 / T+1', buy_lot_must_be_100: '买入非整手', odd_lot_must_be_full_exit: '零股须完整卖出', quantity_above_board_order_limit: '超过单笔申报上限' };
const localTime = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '--';

export function SignalPreview({ runId, observedAt, configIdentity }: { runId: string; observedAt: string | null | undefined; configIdentity: string }) {
  const [payload, setPayload] = useState<Record<string, unknown> | null>(null);
  const [result, setResult] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [currentTime, setCurrentTime] = useState(Date.now());
  const version = useRef(0);
  useEffect(() => { version.current++; setResult(null); setError(''); setBusy(false); }, [runId, observedAt, configIdentity]);
  useEffect(() => () => { version.current++; }, []);
  useEffect(() => { const timer = setInterval(() => setCurrentTime(Date.now()), 1000); return () => clearInterval(timer); }, []);
  async function upload(file?: File) {
    const token = ++version.current;
    setPayload(null); setResult(null); setError(''); setBusy(false);
    if (!file) return;
    try {
      if (file.size > 2_000_000) throw new Error('信号文件超过 2 MB');
      const value: unknown = JSON.parse(await file.text());
      if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('信号文件须为 JSON 对象');
      if (token === version.current) setPayload(value as Record<string, unknown>);
    } catch (e) { if (token === version.current) setError((e as Error).message); }
  }
  async function preview() {
    if (!payload || busy) return;
    const token = ++version.current;
    setBusy(true); setResult(null); setError('');
    try {
      const value = await post<Preview>(`/trading/runs/${runId}/broker/signal-preview`, { signal_request: payload });
      if (token === version.current) { setCurrentTime(Date.now()); setResult(value); }
    } catch (e) { if (token === version.current) setError((e as Error).message); }
    finally { if (token === version.current) setBusy(false); }
  }
  const expires = result?.next_plan ? Date.parse(result.next_plan.expires_at) : null;
  const expired = expires !== null && currentTime > expires;
  const weights = new Map(result?.weight_sizing?.targets.map(item => [item.symbol, item.target_weight]));
  function download() {
    if (!result || expired) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `signal-preview-${runId}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section className="trade-section signal-preview" aria-label="当前信号预览">
    <h2>当前信号预览</h2>
    <div className="signal-preview-toolbar">
      <label>信号与报价 JSON<input type="file" accept=".json,application/json" aria-label="信号与报价文件" onChange={e => void upload(e.target.files?.[0])} /></label>
      <button onClick={preview} disabled={!payload || busy}><Eye size={16} />{busy ? '生成中' : '生成预览'}</button>
      {result && <button className="icon-button" aria-label="下载信号预览" title="下载信号预览" onClick={download} disabled={expired}><ArrowDownToLine size={17} /></button>}
    </div>
    {error && <ErrorMessage message={error} />}
    {result && <>
      <div className="window-summary" role="status"><strong>{expired ? '预览已过期' : states[result.status] || result.status}</strong><span>账户 {result.account_alias}</span><span>信号 {result.strategy_signal.signal_frame.day}</span><span>执行日 {result.strategy_signal.execution_day}</span><span>未保存 · 未提交 · 客户端未操作</span></div>
      {result.reason && (result.status === 'blocked' ? <ErrorMessage message={reasons[result.reason] || result.reason} /> : <div className="window-summary">{reasons[result.reason] || result.reason}</div>)}
      {result.execution_config && <div className="window-summary"><span>单只 / 总仓位 {fmt(result.execution_config.single_limit * 100, 2)}% / {fmt(result.execution_config.gross_limit * 100, 2)}%</span><span>最低佣金 {fmt(result.execution_config.minimum_commission)} 元</span><span>账户观察（UTC+8）{localTime(result.account_observed_at)}</span></div>}
      {result.adjustments?.length ? <div className="table-scroll"><table><thead><tr><th>证券</th><th>目标权重</th><th>现有股数</th><th>目标股数</th><th>变动</th><th>状态</th></tr></thead><tbody>{result.adjustments.map(item => <tr key={item.symbol}><td>{item.symbol}</td><td>{weights.has(item.symbol) ? `${fmt(weights.get(item.symbol)! * 100, 2)}%` : '--'}</td><td>{fmt(item.held_qty, 0)}</td><td>{fmt(item.target_qty, 0)}</td><td>{item.delta_qty > 0 ? '+' : ''}{fmt(item.delta_qty, 0)}</td><td>{states[item.status] || item.status}{item.validation_reason && <span className="window-case-reason">{reasons[item.validation_reason] || item.validation_reason}</span>}</td></tr>)}</tbody></table></div> : <div className="empty small-empty">无调仓清单</div>}
      {result.next_plan && <dl><dt>下一笔预览</dt><dd>{result.next_plan.symbol} · {result.next_plan.side === 'buy' ? '买入' : '卖出'} {fmt(result.next_plan.qty, 0)} 股 · {fmt(result.next_plan.price_cents / 100)} 元</dd><dt>有效至（UTC+8）</dt><dd>{localTime(result.next_plan.expires_at)}</dd></dl>}
      {expired && <button onClick={preview} disabled={busy}><RefreshCw size={16} />重新预览</button>}
    </>}
  </section>;
}
