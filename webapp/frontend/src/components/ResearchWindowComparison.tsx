import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, GitCompareArrows, RotateCcw } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage, Warning } from './Common';
import { fmt } from '../types';
import { researchFamilyNames, type ResearchReplay, type ResearchWindowComparison as WindowResult, type ResearchWindowCase } from '../tradingTypes';

const pct = (value?: number | null) => value == null ? '--' : `${fmt(value * 100, 2)}%`;
const states = { comparable: '已覆盖', failed_source_case: '原回放失败', window_not_covered: '期间缺失', calendar_mismatch: '日历不一致' };
const costs: Record<string, string> = { initial_cash: '原始初始资金', commission: '佣金率', minimum_commission: '最低佣金', sell_tax: '卖出税率', transfer_fee: '过户费率', slippage_bps: '滑点 bps', participation: '参与率', dividend_tax_reserve: '分红税准备金率' };
type Sort = 'source' | 'net_return' | 'max_drawdown' | 'sharpe_zero_rf_252';

function Source({ row }: { row: ResearchWindowCase }) {
  return <details className="window-case-source"><summary>来源与口径</summary><dl>
    <dt>源账户期间</dt><dd>{row.source_period.start} 至 {row.source_period.end}</dd>
    <dt>数据范围</dt><dd>{row.scope} · {row.symbols} 个证券标识</dd>
    <dt>价格快照</dt><dd>{row.dataset}</dd>
    {row.fund_dataset && <><dt>基金快照</dt><dd>{row.fund_dataset}</dd></>}
    <dt>期初权益口径</dt><dd>{row.opening_basis === 'preceding_saved_session_equity' ? '上一个已保存交易日权益' : row.opening_basis === 'original_account_initial_cash' ? '原账户初始资金' : '--'}</dd>
    {row.portfolio_settings && <><dt>持仓 / 保留名次</dt><dd>{row.portfolio_settings.holdings} / {row.portfolio_settings.retain_rank}</dd><dt>调仓间隔</dt><dd>{row.portfolio_settings.rebalance_sessions} 个交易日</dd><dt>总仓位 / 单只上限</dt><dd>{pct(row.portfolio_settings.gross_limit)} / {pct(row.portfolio_settings.single_limit)}</dd></>}
    {Object.entries(row.cost_settings || {}).map(([key, value]) => <div className="window-cost-line" key={key}><dt>{costs[key] || key}</dt><dd>{value == null ? '--' : ['initial_cash', 'minimum_commission'].includes(key) ? `${fmt(value, 2)} 元` : key === 'slippage_bps' ? value : `${fmt(value * 100, 4)}%`}</dd></div>)}
  </dl>{row.reason && <p className="window-case-reason">{row.reason}</p>}</details>;
}

export function ResearchWindowComparison({ runs, initialRun, openCase }: { runs: ResearchReplay[]; initialRun: string; openCase: (run: string, signal: string) => void }) {
  const initial = runs.find(run => run.id === initialRun) || runs[0];
  const [chosen, setChosen] = useState<string[]>(initial ? [initial.id] : []);
  const [start, setStart] = useState(initial?.start || '');
  const [end, setEnd] = useState(initial?.end || '');
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState<Sort>('source');
  const [result, setResult] = useState<WindowResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const validIds = chosen.filter(id => runs.some(run => run.id === id));
  const filtered = runs.filter(run => `${run.id} ${researchFamilyNames[run.research_family] || run.research_family} ${run.start} ${run.end}`.toLowerCase().includes(filter.toLowerCase()));
  const dirty = () => { setResult(null); setError(''); };
  function toggle(id: string) {
    dirty();
    setChosen(previous => previous.includes(id) ? previous.filter(value => value !== id) : [...previous, id]);
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !validIds.length) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setResult(null); setError('');
    try {
      const query = new URLSearchParams({ runs: validIds.join(','), start, end });
      const value = await api<WindowResult>(`/trading/research-comparison?${query}`, { signal: controller.signal });
      if (!controller.signal.aborted) setResult(value);
    } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const rows = [...(result?.cases || [])].sort((a, b) => {
    if (sort === 'source') return 0;
    const first = a.metrics?.[sort], second = b.metrics?.[sort];
    return first == null ? second == null ? 0 : 1 : second == null ? -1 : second - first;
  });
  function download() {
    if (!result) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url;
    link.download = `ledger-comparison_${result.start}_${result.end}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <div className="window-comparison" aria-label="同日期账户比较">
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <details className="window-run-picker" open><summary>比较批次 · {validIds.length} / 16</summary>
          <div className="window-filter"><label>筛选批次<input aria-label="筛选比较批次" value={filter} onChange={e => setFilter(e.target.value)} /></label><button type="button" className="icon-button" title="清除选择" aria-label="清除比较批次" onClick={() => { setChosen([]); dirty(); }}><RotateCcw size={17} /></button></div>
          <div className="window-run-list">{filtered.map(run => <label key={run.id}>
            <input type="checkbox" aria-label={`比较批次 ${run.id}`} checked={validIds.includes(run.id)} disabled={!validIds.includes(run.id) && validIds.length >= 16} onChange={() => toggle(run.id)} />
            <span><strong>{researchFamilyNames[run.research_family] || run.research_family} · {run.results.length} 条</strong><span className="mono">{run.id}</span><small>{run.start} 至 {run.end} · {run.symbols} 个证券标识</small></span>
          </label>)}{!filtered.length && <div className="empty small-empty">无匹配批次</div>}</div>
        </details>
        <div className="window-controls"><label>比较开始<input type="date" aria-label="比较开始日期" value={start} max={end || undefined} onChange={e => { setStart(e.target.value); dirty(); }} required /></label><label>比较结束<input type="date" aria-label="比较结束日期" value={end} min={start || undefined} onChange={e => { setEnd(e.target.value); dirty(); }} required /></label><button type="submit" className="primary" disabled={!validIds.length || validIds.length > 16}><GitCompareArrows size={16} />{busy ? '读取账户' : '比较账户'}</button></div>
      </fieldset>
    </form>
    <Warning>已观察的历史诊断，未获交易准入。日期相同不代表数据快照、证券范围和资金路径相同；不同样本不能归因于单一信号。缺失期间不补零，陈旧估值不构成可靠风险证据。</Warning>
    {error && <ErrorMessage message={error} />}
    {result && <>
      <div className="window-summary" role="status"><strong>{result.comparable_cases} / {result.requested_cases} 条已覆盖</strong><span>{result.sessions} 个交易日</span><span>{result.start} 至 {result.end}</span><span>缺失或不可比 {result.unavailable_cases} 条</span><span>配置现金 / 成本{result.same_cost_settings ? '相同' : '不同或未知'}</span></div>
      <div className="window-result-toolbar"><label className="inline-field">排序<select aria-label="账户比较排序" value={sort} onChange={e => setSort(e.target.value as Sort)}><option value="source">原始批次顺序</option><option value="net_return">净收益降序</option><option value="max_drawdown">回撤幅度从小到大</option><option value="sharpe_zero_rf_252">夏普降序</option></select></label><button type="button" className="icon-button" aria-label="下载账户比较 JSON" title="下载账户比较 JSON" onClick={download}><ArrowDownToLine size={18} /></button></div>
      <div className="table-scroll"><table aria-label="同日期账户比较结果"><thead><tr><th>策略 / 源账户</th><th>覆盖状态</th><th>期初权益（元）</th><th>净收益</th><th>最大回撤</th><th>夏普（零利率）</th><th>最差单日</th><th>费用（元）</th><th>陈旧估值日</th></tr></thead><tbody>{rows.map(row => <tr key={row.case_id}>
        <td className="window-case"><button type="button" className="text-button" onClick={() => openCase(row.run_id, row.signal)}>{row.label}</button><span>{researchFamilyNames[row.family] || row.family}</span><small className="mono">{row.run_id}</small><Source row={row} /></td>
        <td>{states[row.status]}{row.metrics && row.metrics.stale_valuation_days > 0 && <span className="window-stale">陈旧估值</span>}</td>
        <td>{row.opening_equity == null ? '--' : fmt(row.opening_equity, 2)}</td><td>{pct(row.metrics?.net_return)}</td><td>{pct(row.metrics?.max_drawdown)}</td><td>{row.metrics?.sharpe_zero_rf_252 == null ? '--' : fmt(row.metrics.sharpe_zero_rf_252, 3)}</td><td>{pct(row.metrics?.worst_day)}</td><td>{row.metrics == null ? '--' : fmt(row.metrics.fees, 2)}</td><td>{row.metrics?.stale_valuation_days ?? '--'}</td>
      </tr>)}</tbody></table></div>
    </>}
  </div>;
}
