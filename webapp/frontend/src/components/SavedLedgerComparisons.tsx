import { useEffect, useState } from 'react';
import { api } from '../api';
import { ErrorMessage, Warning } from './Common';
import { fmt } from '../types';
import { researchFamilyNames, type SavedLedgerComparisonCatalog } from '../tradingTypes';

const pct = (value?: number | null) => value == null ? '--' : `${fmt(value * 100, 2)}%`;

export function SavedLedgerComparisons({ revision = 0 }: { revision?: number }) {
  const [catalog, setCatalog] = useState<SavedLedgerComparisonCatalog | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    api<SavedLedgerComparisonCatalog>('/trading/research-comparison-snapshots?limit=12')
      .then(value => { if (live) { setCatalog(value); setSelectedId(previous => value.rows.some(row => row.id === previous) ? previous : value.rows[0]?.id || ''); setError(''); } })
      .catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [revision]);

  const snapshot = catalog?.rows.find(row => row.id === selectedId) || catalog?.rows[0];
  if (error) return <div className="research-saved-comparison"><ErrorMessage message={error} /></div>;
  if (!snapshot) return null;

  return <div className="research-saved-comparison">
    <h3>保存的同账本比较（只读）</h3>
    <Warning>以下是已经完成的固定日期、固定成本、统一 Ledger 横向比较。它只是研究证据，不是当前信号，也不会自动晋级或提交订单。</Warning>
    <label className="inline-field research-comparison-picker">保存批次<select aria-label="保存的同账本比较批次" value={snapshot.id} onChange={event => setSelectedId(event.target.value)}>{catalog?.rows.map(row => <option key={row.id} value={row.id}>{row.id} · {row.start} 至 {row.end} · {row.comparable_cases}/{row.requested_cases} 案</option>)}</select></label>
    <div className="trade-source"><span>{snapshot.start} 至 {snapshot.end}</span><span>{snapshot.sessions} 个交易日</span><span>{snapshot.comparable_cases}/{snapshot.requested_cases} 案可比</span><span>成本一致 {snapshot.same_cost_settings ? '是' : '否'}</span><span>保存批次 {snapshot.id}</span></div>
    <div className="table-scroll"><table aria-label="保存的同账本比较"><thead><tr><th>研究族 / 方案</th><th>收益</th><th>回撤</th><th>Sharpe</th><th>费用</th><th>描述性前沿</th><th>晋级状态</th></tr></thead><tbody>{snapshot.cases.map(row => <tr key={row.case_id}><td><strong>{row.label}</strong><small className="mono">{researchFamilyNames[row.family] || row.family}</small></td><td>{row.status === 'comparable' ? pct(row.net_return) : '--'}</td><td>{row.status === 'comparable' ? pct(row.max_drawdown) : '--'}</td><td>{row.sharpe_zero_rf_252 == null ? '--' : fmt(row.sharpe_zero_rf_252, 3)}</td><td>{row.fees == null ? '--' : fmt(row.fees, 2)} 元</td><td>{row.descriptive_nondominated ? '是' : '否'}</td><td>{row.execution_admitted ? '已准入' : '研究待审'}</td></tr>)}</tbody></table></div>
    <small className="muted">描述性前沿只按已保存收益、回撤和 Sharpe 计算；PIT、独立留出、账户观察、行情/回读和人工决策仍未满足时，统一保持不可交易。</small>
  </div>;
}
