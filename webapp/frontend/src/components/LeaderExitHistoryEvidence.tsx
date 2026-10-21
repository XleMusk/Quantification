import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';

type ExitRow = {
  family: string; old_status: string; new_status: string; new_error: string | null;
  old_main_return: number | null; new_main_return: number | null;
  old_tail_return: number | null; new_tail_return: number | null;
  old_overdue_days: number | null; new_overdue_days: number | null;
  old_open_positions: number | null; new_open_positions: number | null;
  old_orders: number | null; new_orders: number | null; old_fills: number | null; new_fills: number | null;
  old_blocked_exits: number | null; new_blocked_exits: number | null;
  old_last_session: string | null; new_last_session: string | null;
};
type ExitView = {
  status: 'completed' | 'diagnostic_failed' | 'unavailable'; read_at: string; created_at?: string;
  source_file: string | null; method_id?: string; start?: string; end?: string;
  execution_tail_sessions: string[]; frozen_cohort_size: number | null;
  new_engine_cases: number | null; retained_unavailable_original_cases: number | null; rows: ExitRow[];
  protocol_file?: string | null; recovery_evidence_file?: string | null;
};
const names: Record<string, string> = {
  stage1_amount_baseline: '首板接力', stage2_amount_baseline: '二板接力', stage3_amount_baseline: '三板接力',
  trend_leader_20_60: '趋势龙头代理', daily_rebound_proxy: '收盘反包代理',
};
const count = (value: number | null) => value == null ? '—' : fmt(value, 0);
const percent = (value: number | null) => value == null ? '不可用' : `${fmt(value * 100, 2)}%`;

export function LeaderExitHistoryEvidence({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<ExitView | null>(null);
  const [error, setError] = useState('');
  const [window, setWindow] = useState<'main' | 'tail'>('main');
  useEffect(() => {
    const controller = new AbortController();
    setView(null); setError('');
    api<ExitView>('/trading/research/leader-exit-hook', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [refreshVersion]);
  const result = (row: ExitRow, path: 'old' | 'new') => {
    if (path === 'new' && row.new_error) return '回放停止';
    if (row[`${path}_fills`] === 0) return '未成交，不评收益';
    return percent(row[`${path}_${window}_return`]);
  };
  return <div className="offline-subsection exit-history" aria-label="退出路径历史对照">
    <h3>退出路径 · 历史对照</h3>
    {error && <p className="subtle" role="status">退出对照读取失败：{error}。暂不显示旧结果替代。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取已保存的历史诊断，不启动回放或交易。</p>}
    {view?.status === 'unavailable' && <p className="subtle" role="status">尚无保存的独立退出历史对照。</p>}
    {view && view.status !== 'unavailable' && <>
      <p className="subtle">固定 {count(view.frozen_cohort_size)} 只股票，主窗口 {view.start} 至 {view.end}；已保存 {count(view.new_engine_cases)} 套对照。原信号批次、费用与样本保持相同，比较新增独立到期退出后的历史模拟结果。</p>
      {view.status === 'diagnostic_failed' && <p role="status">部分回放停止，原失败和剩余持仓保留；停止项不展示收益。</p>}
      <div className="exit-history-controls" role="group" aria-label="历史收益窗口">
        <button type="button" aria-pressed={window === 'main'} onClick={() => setWindow('main')}>主窗口收益</button>
        <button type="button" aria-pressed={window === 'tail'} onClick={() => setWindow('tail')}>含执行尾部收益</button>
      </div>
      <p className="subtle" aria-live="polite">当前收益：{window === 'main' ? `主窗口截至 ${view.end}` : `含执行尾部 ${view.execution_tail_sessions.join('、') || '日期未知'}`}。超期天数只计主窗口；残仓读取各保存账户的最后日期，切换收益窗口不改变这两个口径。</p>
      <div className="table-scroll"><table className="compact exit-history-table" aria-label="独立退出历史收益与残仓对照">
        <thead><tr><th>策略</th><th>原路径收益</th><th>独立退出收益</th><th>超5日持仓天数<br />原 → 新</th><th>期末未退出证券<br />原 → 新</th><th>受阻卖出次数<br />原 → 新</th></tr></thead>
        <tbody>{view.rows.map(row => <tr key={row.family}>
          <td>{names[row.family] || row.family}</td><td>{result(row, 'old')}</td><td>{result(row, 'new')}</td>
          <td>{count(row.old_overdue_days)} → {count(row.new_overdue_days)}</td>
          <td title={`原账户截至 ${row.old_last_session || '未知'}；新账户截至 ${row.new_last_session || '未知'}`}>{count(row.old_open_positions)} → {count(row.new_open_positions)}</td>
          <td>{count(row.old_blocked_exits)} → {count(row.new_blocked_exits)}</td>
        </tr>)}</tbody>
      </table></div>
      <p className="subtle">超期和残仓减少不代表策略更赚钱。亏损、未成交和不利变化均保留；主窗口与尾部须一起看。固定小样本已被检查过，尚不能据此认定龙头战法有效或生成当前买卖名单。</p>
      <details className="offline-details"><summary>模拟委托、成交与回放状态</summary>
        <div className="table-scroll"><table className="compact" aria-label="独立退出模拟委托和成交">
          <thead><tr><th>策略</th><th>原委托 / 成交片段</th><th>新委托 / 成交片段</th><th>原 / 新账户截至</th><th>新路径状态</th></tr></thead>
          <tbody>{view.rows.map(row => <tr key={row.family}><td>{names[row.family] || row.family}</td>
            <td>{count(row.old_orders)} / {count(row.old_fills)}</td><td>{count(row.new_orders)} / {count(row.new_fills)}</td>
            <td>{row.old_last_session || '未知'} / {row.new_last_session || '未知'}</td>
            <td>{row.new_error || (row.new_status === 'completed_already_inspected_engine_diagnostic' ? '完成历史诊断，未验证策略' : row.new_status)}</td>
          </tr>)}</tbody>
        </table></div>
        <p className="subtle">另外 {count(view.retained_unavailable_original_cases)} 项原比较仍因证据不足不可用，没有作为本次新增回放或零收益计入。三板没有成交时仍保留其买入受阻记录。</p>
      </details>
      <p className="subtle">这是历史模拟，不是券商成交。历史资格、公司行动及价格口径仍有未核实项；新退出轨迹单独保存，原失败和残仓没有被改写。</p>
    </>}
    {view && <details className="offline-details"><summary>退出对照来源与读取时间</summary><dl>
      <dt>完成回执</dt><dd>{view.source_file || '尚未形成'}</dd><dt>实际完成时间</dt><dd>{view.created_at || '尚未形成'}</dd>
      <dt>读取时间</dt><dd>{view.read_at}</dd><dt>原输入与固定规则</dt><dd>{view.protocol_file || '尚未形成'}</dd>
      <dt>保存结果回读</dt><dd>{view.recovery_evidence_file || '尚未形成'}</dd>
    </dl></details>}
  </div>;
}
