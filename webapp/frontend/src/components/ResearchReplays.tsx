import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { Chart } from './Chart';
import { ResearchWindowComparison } from './ResearchWindowComparison';
import { SavedLedgerComparisons } from './SavedLedgerComparisons';
import { ValuationGaps } from './ValuationGaps';
import { ErrorMessage, Warning } from './Common';
import { fmt } from '../types';
import { signalNames, runStatuses, researchFamilyNames, type ResearchReplay, type ResearchReplayDetail, type ResearchDiagnostic, type ResearchCandidateBoard } from '../tradingTypes';

const names = { ...signalNames, trend: '趋势基线', lag1_trend_any_lhb: '任意龙虎榜 +1日', lag2_trend_any_lhb: '任意龙虎榜 +2日', lag1_trend_h1: '席位买入确认 +1日', lag2_trend_h1: '席位买入确认 +2日', lag1_trend_h4: '席位卖出过滤 +1日', lag2_trend_h4: '席位卖出过滤 +2日', lag1_trend_h1_h4: '席位买入确认 / 卖出过滤 +1日', lag2_trend_h1_h4: '席位买入确认 / 卖出过滤 +2日', donchian_trend: 'Donchian 趋势突破', qarp_only: '盈利与估值', risk_momentum_qarp: '风险 / 动量 / 盈利估值', profit_matched_low_vol: '同财务池低波', profit_matched_low_vol_half_momentum: '同财务池低波加动量', profitability_only: '纯盈利代理', risk_momentum_profitability: '风险 / 动量 / 盈利' };
const fundNames: Record<string, string> = { monthly_fixed_sleeves: 'ETF固定配置', own_total_return_10m: 'ETF十个月趋势', monthly_equal_risk: 'ETF等风险贡献', static_2023_equal_risk: 'ETF固定2023风险权重', sovereign_bond_sleeve: '国债ETF80%对照' };
const pct = (value?: number | null) => value == null ? '--' : `${fmt(value * 100, 2)}%`;

export function ResearchReplays() {
  const [runs, setRuns] = useState<ResearchReplay[]>([]);
  const [diagnostics, setDiagnostics] = useState<ResearchDiagnostic[]>([]);
  const [candidateBoard, setCandidateBoard] = useState<ResearchCandidateBoard | null>(null);
  const [selected, setSelected] = useState('');
  const [signal, setSignal] = useState('');
  const [detail, setDetail] = useState<ResearchReplayDetail | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [view, setView] = useState('single');
  const run = runs.find(item => item.id === selected);
  const selectedSignal = run?.results.some(row => row.signal === signal) ? signal : run?.results[0]?.signal || '';
  const label = (value: string) => fundNames[value] || run?.results.find(row => row.signal === value)?.label || names[value as keyof typeof names] || value;
  useEffect(() => {
    let live = true;
    api<ResearchDiagnostic[]>('/trading/research-diagnostics').then(items => {
      if (live) setDiagnostics(items);
    }).catch(e => { if (live) setError(e.message); });
    api<ResearchCandidateBoard>('/trading/research-candidate-board').then(value => {
      if (live) setCandidateBoard(value);
    }).catch(e => { if (live) setError(e.message); });
    api<ResearchReplay[]>('/trading/research-replays').then(items => {
      if (!live) return;
      setRuns(items);
      setSelected(previous => items.some(item => item.id === previous) ? previous : items[0]?.id || '');
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [revision]);
  useEffect(() => {
    let live = true; setDetail(null);
    if (view === 'single' && selected && selectedSignal) api<ResearchReplayDetail>(`/trading/research-replays/${selected}/${selectedSignal}`).then(value => {
      if (live) { setDetail(value); setError(''); }
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [selected, selectedSignal, revision, view]);
  const shown = detail?.id === selected && detail.signal === selectedSignal ? detail : null;
  return <section className="trade-section research-replays" aria-label="扩大样本研究回放">
    <div className="research-heading"><h2>扩大样本研究回放</h2><button className="icon-button" aria-label="刷新研究回放" title="刷新研究回放" onClick={() => setRevision(value => value + 1)}><RefreshCw size={17} /></button></div>
    {error && <ErrorMessage message={error} />}
    {!runs.length ? <div className="empty small-empty">{error ? '研究记录不可读取' : '暂无已保存的研究回放'}</div> : <>
      <div className="tabs" role="tablist" aria-label="研究账户视图"><button role="tab" aria-selected={view === 'single'} className={view === 'single' ? 'active' : ''} onClick={() => setView('single')}>单批次回放</button><button role="tab" aria-selected={view === 'window'} className={view === 'window' ? 'active' : ''} onClick={() => setView('window')}>同日期比较</button></div>
      <div hidden={view !== 'window'}><ResearchWindowComparison runs={runs} initialRun={selected} openCase={(id, value) => { setSelected(id); setSignal(value); setView('single'); }} /></div>
      <div hidden={view !== 'single'}>
      <label className="inline-field research-run-select">研究批次<select aria-label="研究回放批次" value={selected} onChange={e => setSelected(e.target.value)}>{runs.map(item => <option key={item.id} value={item.id}>{item.id} · {researchFamilyNames[item.research_family] || item.research_family} · {item.symbols} 个证券标识 · {item.start} 至 {item.end}</option>)}</select></label>
      {run && <>
        <div className="trade-source"><span>{run.symbols} 个证券标识 · {run.sessions} 个交易日</span><span>{run.start} 至 {run.end}</span><span>研究初始资金 {fmt(run.protocol.portfolio.initial_cash, 0)} 元</span><span>只读历史研究 · 未获交易准入</span></div>
        <Warning>{run.research_family === 'full_source_public_seat_donchian_v1' ? '公开龙虎榜席位披露事件诊断；不等于任何个人交割单、账户或持仓，固定披露滞后假设仅用于历史比较，不构成当前信号或交易准入。' : run.research_family === 'fund_allocation' ? '固定ETF组合历史诊断，不代表全ETF市场或账户准入。' : run.scope === 'full_source_sh_sz_research_only' ? '已获取沪深股票范围，历史时点覆盖仍未独立核验，不代表完整A股市场。' : !run.protocol.selection_is_point_in_time ? '部分下载样本，仅用于数据接入与账本回放，不是全市场策略验证。' : '固定历史股票子集，不代表全市场或个人账户准入。'}{run.research_family !== 'full_source_public_seat_donchian_v1' && !run.protocol.fresh_holdout_claimed && '2026 年已被观察，不是全新独立测试。'}{run.results.some(row => row.signal === 'equal_weight') && '等权固定排序是代码排序对照，不是市场指数。'}</Warning>
        <div className="table-scroll"><table><thead><tr><th>策略</th><th>状态</th><th>净收益</th><th>最大回撤</th><th>夏普（零利率）</th><th>最差单日</th><th>陈旧估值日</th><th>调仓间隔</th></tr></thead><tbody>{run.results.map(row => <tr key={row.signal} className={selectedSignal === row.signal ? 'selected' : ''}><td><button className="text-button" aria-pressed={selectedSignal === row.signal} onClick={() => setSignal(row.signal)}>{label(row.signal)}</button></td><td>{runStatuses[row.status] || row.status}</td><td>{row.status === 'completed' ? pct(row.net_return) : '--'}</td><td>{row.status === 'completed' ? pct(row.max_drawdown) : '--'}</td><td>{row.status === 'completed' && row.sharpe_zero_rf_252 != null ? fmt(row.sharpe_zero_rf_252, 3) : '--'}</td><td>{row.status === 'completed' ? pct(row.worst_day) : '--'}</td><td>{row.status === 'completed' ? row.stale_valuation_days : '--'}</td><td>{run.protocol.decision_cadence === 'completed_month_end' ? '月末' : `${row.signal === 'donchian_trend' ? run.protocol.donchian_portfolio_override?.rebalance_sessions ?? run.protocol.portfolio.rebalance_sessions : run.protocol.portfolio.rebalance_sessions} 日`}</td></tr>)}</tbody></table></div>
        {shown && <>
          {shown.error && <ErrorMessage message={`${shown.error}；已完成 ${shown.receipt.completed_sessions} 个交易日，最后完成 ${shown.receipt.last_completed_session || '--'}，失败尝试 ${shown.receipt.attempted_session || '--'}。无完整期间收益。`} />}
          <div className="trade-source"><span>快照 {run.dataset.split(/[\\/]/).pop()}</span><span>佣金 {pct(shown.config.commission)} · 最低 {fmt(shown.config.minimum_commission)} 元</span><span>卖出税 {pct(run.research_family === 'fund_allocation' ? 0 : shown.config.sell_tax)} · 过户费 {fmt((run.research_family === 'fund_allocation' ? 0 : shown.config.transfer_fee) * 100, 3)}%</span><span>滑点 {shown.config.slippage_bps} bps · 参与率 {pct(shown.config.participation)}</span><span>分红税准备金 {pct(run.research_family === 'fund_allocation' ? 0 : shown.config.dividend_tax_reserve)}</span></div>
          <h3>{label(selectedSignal)} · {shown.status === 'completed' ? '账户权益' : '截至最后成功交易日的账户权益'}</h3>
          {shown.daily.length > 0 && <Chart x={shown.daily.map(row => row.date)} values={shown.daily.map(row => row.equity)} label="研究回放账户权益" />}
          {shown.segments.length > 0 && <div className="table-scroll"><table><thead><tr><th>已观察年份</th><th>净收益</th><th>最大回撤</th><th>夏普</th><th>最差单日</th></tr></thead><tbody>{shown.segments.map(row => <tr key={row.year}><td>{row.year}</td><td>{pct(row.net_return)}</td><td>{pct(row.max_drawdown)}</td><td>{row.sharpe_zero_rf_252 == null ? '--' : fmt(row.sharpe_zero_rf_252, 3)}</td><td>{pct(row.worst_day)}</td></tr>)}</tbody></table></div>}
          <ValuationGaps runId={selected} signal={selectedSignal} revision={revision} />
        </>}
      </>}
      </div>
    </>}
    <SavedLedgerComparisons revision={revision} />
    {candidateBoard?.rows.length ? <div className="research-candidate-board">
      <h3>近期候选复核板（只读）</h3>
      <Warning>按固定研究族、信号和日期窗口去重，仅用于人工复核；不按收益自动排名、不自动晋级，也不改变统一账本或交易准入。</Warning>
      <div className="table-scroll"><table aria-label="近期候选复核板"><thead><tr><th>研究族 / 候选</th><th>期间</th><th>收益 / 回撤</th><th>夏普 / 费用</th><th>验证边界</th><th>样本窗口</th><th>阻塞</th></tr></thead><tbody>{candidateBoard.rows.map(row => <tr key={`${row.family}:${row.signal}:${row.start}:${row.end}`}><td><strong>{row.label}</strong><small className="mono">{row.family} · {row.id}</small></td><td>{row.start} 至 {row.end}<br />{row.sessions ?? '--'} 个交易日 / {row.symbols ?? '--'} 个标识</td><td>{pct(row.net_return)} / {pct(row.max_drawdown)}</td><td>{row.sharpe_zero_rf_252 == null ? '--' : fmt(row.sharpe_zero_rf_252, 3)}<br />费用 {row.fees == null ? '--' : fmt(row.fees, 2)} 元</td><td>PIT {row.selection_is_point_in_time === true ? '是' : '否'}<br />独立留出 {row.fresh_holdout_claimed === true ? '是' : '否'}<br />准入 {row.execution_admitted ? '开' : '关'}</td><td>{row.non_overlapping == null ? '--' : row.non_overlapping ? '窗口不重叠' : '存在重叠'}<br />连续性 {row.calendar_continuity_verified == null ? '未提供' : row.calendar_continuity_verified ? '已验证' : '未验证'}</td><td>{row.blockers.join('；')}</td></tr>)}</tbody></table></div>
      <small className="muted">共 {candidateBoard.total_groups} 个固定复核组{candidateBoard.truncated ? '，页面显示最新窗口子集' : ''}。所有行仍属于研究结果，不是当前交易信号。</small>
    </div> : null}
    {diagnostics.length > 0 && <div className="research-diagnostics">
      <h3>非共享账本研究诊断</h3>
      <Warning>这些结果来自 Qlib / StockDB 的独立研究格式，只用于发现数据质量和策略方向；不进入统一账本，不是当前信号，也不具备交易准入。</Warning>
      <div className="table-scroll"><table aria-label="非共享账本研究诊断"><thead><tr><th>来源 / 结果</th><th>期间</th><th>收益</th><th>基准 / 相对</th><th>最大回撤</th><th>数据门控</th><th>阻塞</th></tr></thead><tbody>{diagnostics.map(row => <tr key={`${row.id}:${row.label}`}><td><strong>{row.label}</strong><small className="mono">{row.source} · {row.id}</small></td><td>{row.start} 至 {row.end}<br />{row.sessions} 个交易日 / {row.symbols ?? '--'} 个标识</td><td>{pct(row.net_return)}<br /><small>{row.status}</small></td><td>{row.benchmark_return == null ? '--' : pct(row.benchmark_return)} / {pct(row.relative_wealth_return)}</td><td>{pct(row.max_drawdown)}<br />夏普 {row.sharpe_zero_rf_252 == null ? '--' : fmt(row.sharpe_zero_rf_252, 3)}</td><td>{row.data_quality_gate}{row.excluded_symbols != null && ` · 排除 ${row.excluded_symbols}`}</td><td>{row.blockers.join('；')}</td></tr>)}</tbody></table></div>
    </div>}
  </section>;
}
