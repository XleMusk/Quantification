import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';
import { ErrorMessage, Warning } from './Common';

const labels: Record<string, string> = {
  annual_matched_momentum: '同池动量', annual_matched_low_vol_half_momentum: '低波后动量',
  annual_quality_only: '年度质量', annual_risk_momentum_quality: '风险 / 动量 / 质量',
  annual_low_asset_growth: '低资产增长', annual_quality_low_asset_growth: '质量 + 低资产增长',
};
const allocationName = (value?: string | null) => value === 'open_stock_lot_tracking_v1' ? '整数股数分配' : value === 'close_stock_lot_tracking_v1' ? '收盘权重分配' : '原始目标取整';
const statuses: Record<string, string> = { not_started: '未启动', completed: '已完成', partial: '部分完成', failed: '失败', interrupted: '已中断', waiting_for_post_declaration_market_data: '等待新行情', waiting_for_complete_session_and_calendar: '等待完整交易日', prepared_annual_quality_prospective_candidates: '已准备' };
const pct = (value?: number | null) => value == null ? '--' : `${fmt(value * 100, 2)}%`;
const money = (value?: number | null) => value == null ? '--' : fmt(value / 100, 2);
type CaseResult = { signal: string; status: string; completed_sessions: number; net_return?: number; max_drawdown?: number; sharpe_zero_rf_252?: number; error?: string };
type AnnualRun = { id: string; status: string; declared_at: string; feature_asof: string; report_day: string; preview_market_day: string; preview_counts: { common_pool: number }; portfolio: { initial_cash: number }; sessions: number; lot_allocation: string | null; reason: string | null; results: CaseResult[] };
type Slot = { symbol: string; market_day: string; legal_quantity: number | null; close_cents: number | null; rounded_weight: number | null; buy_fees_cents: number | null; status: string; within_existing_ths_instrument_scope: boolean };
type Capacity = { id: string; allocation_policy: string | null; calculated_at: string | null; summary: { known_sized_names: number; target_count: number; unknown_targets: number; rounded_gross: number | null; known_subtotal_buy_fees_cents: number }; slots: Slot[] };
type IndustryExposure = { source_industry: string | null; names: number; gross_weight: number; stock_share: number };
type ExposureProfile = { effective_names: number | null; source_financial_names: number; unknown_industry_gross: number; weighted_original_diagnostics: { quality?: number | null; investment?: number | null }; industries: IndustryExposure[] };
type Exposure = { id: string; capacity_id: string; summary: { target: ExposureProfile; saved_capacity: ExposureProfile; stock_normalized_weight_distance: number }; slots: { symbol: string; source_industry: string | null; industry_status: string }[] };
type AnnualDetail = AnnualRun & { case: string; weights: Record<string, number>; rankings: { symbol: string; rank: number; score: number; momentum: number; volatility: number; quality: number; asset_growth?: number; investment?: number }[]; capacities: Capacity[]; exposures?: Exposure[] };

export function AnnualQualityResearch() {
  const [runs, setRuns] = useState<AnnualRun[]>([]);
  const [selected, setSelected] = useState('');
  const [signal, setSignal] = useState('annual_risk_momentum_quality');
  const [detail, setDetail] = useState<AnnualDetail | null>(null);
  const [capacityId, setCapacityId] = useState('');
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const run = runs.find(row => row.id === selected);
  const selectedSignal = run?.results.some(row => row.signal === signal) ? signal : run?.results[0]?.signal || '';
  useEffect(() => {
    let live = true; setLoading(true);
    api<AnnualRun[]>('/trading/annual-quality').then(value => {
      if (!live) return;
      setRuns(value); setError('');
      setSelected(previous => value.some(row => row.id === previous) ? previous : value[0]?.id || '');
    }).catch(e => { if (live) setError(e.message); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [revision]);
  useEffect(() => {
    let live = true; setDetail(null);
    if (selected && selectedSignal) api<AnnualDetail>(`/trading/annual-quality/${selected}/${selectedSignal}`).then(value => {
      if (live) { setDetail(value); setError(''); }
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [selected, selectedSignal, revision]);
  const shown = detail?.id === selected && detail.case === selectedSignal ? detail : null;
  const hasInvestment = shown?.rankings.some(row => row.asset_growth != null);
  const capacity = shown?.capacities.find(row => row.id === capacityId) || shown?.capacities[0];
  const exposure = shown?.exposures?.find(row => row.capacity_id === capacity?.id);
  const industryBySymbol = new Map(exposure?.slots.map(row => [row.symbol, row]) || []);
  const beforeIndustries = new Map(exposure?.summary.target.industries.map(row => [row.source_industry, row]) || []);
  const afterIndustries = new Map(exposure?.summary.saved_capacity.industries.map(row => [row.source_industry, row]) || []);
  const industryLabels = [...new Set([...beforeIndustries.keys(), ...afterIndustries.keys()])]
    .sort((a, b) => (afterIndustries.get(b)?.stock_share || 0) - (afterIndustries.get(a)?.stock_share || 0)
      || (a || '').localeCompare(b || ''));
  return <section className="trade-section research-replays" aria-label="年度质量与仓位容量">
    <div className="research-heading"><h2>年度质量与仓位容量</h2><button className="icon-button" aria-label="刷新年度候选" title="刷新年度候选" onClick={() => setRevision(value => value + 1)}><RefreshCw size={17} /></button></div>
    {error && <ErrorMessage message={error} />}
    {!runs.length ? <div className="empty small-empty">{loading ? '读取年度候选' : error ? '年度记录不可读取' : '暂无年度候选'}</div> : <>
      <label className="inline-field research-run-select">声明批次<select aria-label="年度声明批次" value={selected} onChange={e => setSelected(e.target.value)}>{runs.map(row => <option key={row.id} value={row.id}>{row.id} · {allocationName(row.lot_allocation)} · {statuses[row.status] || row.status}</option>)}</select></label>
      {run && <>
        <div className="trade-source"><span>共同池 {fmt(run.preview_counts.common_pool, 0)} 只</span><span>资金情景 {fmt(run.portfolio.initial_cash, 0)} 元</span><span>行情截止 {run.preview_market_day}</span><span>实际账户交易日 {run.sessions}</span></div>
        <div className="trade-source"><span>财务期间 {run.report_day}</span><span>财务可用时点 {run.feature_asof}</span><span>策略声明 {run.declared_at}</span></div>
        <Warning>未获交易准入。财务为取得时点版本，不能回填历史；保存价格下的目标与股数不是当前报价、成交或同花顺账户持仓。</Warning>
        <div className="table-scroll"><table><thead><tr><th>策略</th><th>状态</th><th>已处理交易日</th><th>净收益</th><th>最大回撤</th><th>夏普</th></tr></thead><tbody>{run.results.map(row => <tr key={row.signal} className={row.signal === selectedSignal ? 'selected' : ''}><td><button className="text-button" aria-pressed={row.signal === selectedSignal} onClick={() => setSignal(row.signal)}>{labels[row.signal] || row.signal}</button></td><td>{statuses[row.status] || row.status}</td><td>{row.completed_sessions}</td><td>{row.completed_sessions > 0 && row.status === 'completed' ? pct(row.net_return) : '--'}</td><td>{row.completed_sessions > 0 && row.status === 'completed' ? pct(row.max_drawdown) : '--'}</td><td>{row.completed_sessions > 0 && row.status === 'completed' && row.sharpe_zero_rf_252 != null ? fmt(row.sharpe_zero_rf_252, 3) : '--'}</td></tr>)}</tbody></table></div>
        {run.status.startsWith('waiting_') && <div className="empty small-empty">{statuses[run.status]} · 尚无声明后完整行情</div>}
      </>}
      {shown && <>
        <h3>{labels[selectedSignal]} · 保存的候选与目标</h3>
        <div className="table-scroll"><table><thead><tr><th>名次</th><th>证券</th><th>分数</th><th>动量</th><th>波动率</th><th>质量分数</th>{hasInvestment && <><th>资产增长率</th><th>低投资分数</th></>}<th>原始目标</th></tr></thead><tbody>{shown.rankings.map(row => <tr key={row.symbol}><td>{row.rank}</td><td>{row.symbol}</td><td>{fmt(row.score, 4)}</td><td>{pct(row.momentum)}</td><td>{pct(row.volatility)}</td><td>{fmt(row.quality, 4)}</td>{hasInvestment && <><td>{pct(row.asset_growth)}</td><td>{row.investment == null ? '--' : fmt(row.investment, 4)}</td></>}<td>{pct(shown.weights[row.symbol] || 0)}</td></tr>)}</tbody></table></div>
        {!capacity ? <div className="empty small-empty">该声明批次暂无保存的容量诊断</div> : <>
          <label className="inline-field research-run-select">容量记录<select aria-label="年度容量记录" value={capacity.id} onChange={e => setCapacityId(e.target.value)}>{shown.capacities.map(row => <option key={row.id} value={row.id}>{row.id} · {allocationName(row.allocation_policy)}</option>)}</select></label>
          <div className="trade-source"><span>可配置 {capacity.summary.known_sized_names} / {capacity.summary.target_count} 只</span><span>资金使用率 {pct(capacity.summary.rounded_gross)}</span><span>已知买入费用 {money(capacity.summary.known_subtotal_buy_fees_cents)} 元</span><span>未知目标 {capacity.summary.unknown_targets}</span></div>
          <div className="table-scroll"><table><thead><tr><th>证券</th>{exposure && <th>源行业</th>}<th>价格日期</th><th>保存价格</th><th>可配置股数</th><th>取整后仓位</th><th>买入费用</th><th>现适配范围</th></tr></thead><tbody>{capacity.slots.map(row => <tr key={row.symbol}><td>{row.symbol}</td>{exposure && <td>{industryBySymbol.get(row.symbol)?.source_industry || '--'}</td>}<td>{row.market_day}</td><td>{money(row.close_cents)}</td><td>{row.legal_quantity ?? '--'}</td><td>{pct(row.rounded_weight)}</td><td>{money(row.buy_fees_cents)}</td><td>{row.within_existing_ths_instrument_scope ? '主板范围内' : '范围外'}</td></tr>)}</tbody></table></div>
          {!exposure ? <div className="empty small-empty">该容量记录暂无保存的暴露诊断</div> : <div aria-label="年度目标与整手暴露">
            <h3>目标与整手分配偏移</h3>
            <div className="trade-source"><span>有效持仓 {exposure.summary.saved_capacity.effective_names == null ? '--' : fmt(exposure.summary.saved_capacity.effective_names, 2)} / 原目标 {exposure.summary.target.effective_names == null ? '--' : fmt(exposure.summary.target.effective_names, 2)}</span><span>股票内权重偏移 {pct(exposure.summary.stock_normalized_weight_distance)}</span><span>已知源金融类 {exposure.summary.saved_capacity.source_financial_names} 只</span><span>未知行业仓位 {pct(exposure.summary.saved_capacity.unknown_industry_gross)}</span></div>
            <div className="trade-source"><span>源行业标签未独立核实</span><span>加权原质量 {exposure.summary.saved_capacity.weighted_original_diagnostics.quality == null ? '--' : fmt(exposure.summary.saved_capacity.weighted_original_diagnostics.quality, 4)}</span>{hasInvestment && <span>加权原低投资 {exposure.summary.saved_capacity.weighted_original_diagnostics.investment == null ? '--' : fmt(exposure.summary.saved_capacity.weighted_original_diagnostics.investment, 4)}</span>}</div>
            <div className="table-scroll"><table><thead><tr><th>源行业</th><th>原目标只数</th><th>可配置只数</th><th>原目标股票占比</th><th>整手股票占比</th><th>整手总资金占比</th></tr></thead><tbody>{industryLabels.map(label => <tr key={label || '__unknown__'}><td>{label || '--'}</td><td>{beforeIndustries.get(label)?.names || 0}</td><td>{afterIndustries.get(label)?.names || 0}</td><td>{pct(beforeIndustries.get(label)?.stock_share || 0)}</td><td>{pct(afterIndustries.get(label)?.stock_share || 0)}</td><td>{pct(afterIndustries.get(label)?.gross_weight || 0)}</td></tr>)}</tbody></table></div>
          </div>}
        </>}
      </>}
    </>}
  </section>;
}
