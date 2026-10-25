import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage } from './Common';
import { fmt } from '../types';

type ContinuityView = {
  status: string;
  source_file?: string;
  last_completed_day?: string | null;
  generation?: number | null;
  daily_rows: number;
  replay_completed_sessions: number;
  order_count: number;
  fill_count: number;
  pending_targets: boolean;
  cash_cny?: number | null;
  position_count: number;
  refresh_gate?: string | null;
  limitations: string[];
  execution_admitted: false;
  client_interacted: false;
  ledger_mutated: false;
  next_action: string;
};

const statusNames: Record<string, string> = {
  VERIFIED_READONLY_CURRENT_CUTOFF: '已保存纸面连续记录',
  not_observed: '尚无连续性回执',
  invalid_receipt: '连续性回执不可读取',
};

export function PaperAccountContinuity() {
  const [view, setView] = useState<ContinuityView | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setBusy(true);
    try {
      setView(await api<ContinuityView>('/trading/paper-account-continuity'));
      setError('');
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { refresh(); }, []);

  return <section className="trade-section" aria-label="纸面账户连续性" aria-busy={busy}>
    <div className="opening-daily-heading">
      <div><h2>纸面账户连续性</h2><p className="subtle">只读读取已保存的日切结果；刷新不会推进账户、生成订单或接触客户端。</p></div>
      <button className="icon-button" aria-label="刷新纸面账户连续性" title="只读刷新" onClick={refresh} disabled={busy}><RefreshCw size={16} /></button>
    </div>
    {error && <ErrorMessage message={error} />}
    {view && <>
      <p className="subtle" role="status">{statusNames[view.status] || view.status} · 最后完成日 {view.last_completed_day || '--'} · generation {view.generation ?? '--'}</p>
      <div className="metrics trade-metrics">
        <div><span>日记录</span><strong>{fmt(view.daily_rows, 0)}</strong></div>
        <div><span>订单 / 成交</span><strong>{fmt(view.order_count, 0)} / {fmt(view.fill_count, 0)}</strong></div>
        <div><span>现金</span><strong>{view.cash_cny == null ? '--' : fmt(view.cash_cny, 2)}</strong></div>
        <div><span>持仓</span><strong>{fmt(view.position_count, 0)}</strong></div>
      </div>
      {view.refresh_gate && <p className="subtle">刷新状态：{view.refresh_gate}</p>}
      {view.pending_targets && <p className="subtle">当前存在待处理目标，需沿同一 paper 账户继续核对。</p>}
      <p className="subtle">{view.next_action}</p>
    </>}
  </section>;
}
