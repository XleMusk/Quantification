import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, ChevronRight, RefreshCw, Search } from 'lucide-react';
import { api } from '../api';
import { Chart } from '../components/Chart';
import { ErrorMessage } from '../components/Common';
import { adjustmentNames, factorNames, fmt, type Dataset, type Job, type Result } from '../types';
import { runStatuses, type TradeDetail, type TradeOverview } from '../tradingTypes';

type SnapshotRow = { symbol: string; date: string; open: number; high: number; low: number; close: number; volume: number; previous_date: string | null; previous_close: number | null; change_pct: number | null };
type Snapshot = { dataset: Dataset; rows: SnapshotRow[]; status: string; realtime: false; price_change_basis: 'previous_observed_row' };
type Props = { datasets: Dataset[]; selectedDatasetId: string; onDatasetSelect: (id: string) => void; jobs: Job[]; onNavigate: (page: string) => void; onOpenResult: (id: string) => void };
const number = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const percent = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${number(value * 100, digits)}%`;
const tone = (value: number | null | undefined) => value == null || value === 0 ? '' : value > 0 ? 'market-up' : 'market-down';
const shortDate = (date: string | null | undefined) => date?.slice(0, 10) || '—';
function savedRun() { try { return localStorage.getItem('quant.activeRun.v1') || ''; } catch { return ''; } }
function PaneHeading({ title, children }: { title: string; children?: React.ReactNode }) { return <div className="terminal-pane-heading"><h2>{title}</h2><div className="terminal-pane-actions">{children}</div></div>; }
function PaneEmpty({ children, action, onAction }: { children: React.ReactNode; action?: string; onAction?: () => void }) { return <div className="terminal-pane-empty"><p>{children}</p>{action && <button onClick={onAction}>{action}<ChevronRight size={12} /></button>}</div>; }

export function TerminalOverview({ datasets, selectedDatasetId, onDatasetSelect, jobs, onNavigate, onOpenResult }: Props) {
  const [overview, setOverview] = useState<TradeOverview | null>(null);
  const [detail, setDetail] = useState<TradeDetail | null>(null);
  const [activeRun, setActiveRun] = useState(savedRun);
  const [accountError, setAccountError] = useState('');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState('');
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [watchTab, setWatchTab] = useState('watch');
  const [search, setSearch] = useState('');
  const [range, setRange] = useState('all');
  const [orderTab, setOrderTab] = useState('orders');
  const [orderSide, setOrderSide] = useState('all');
  const [orderState, setOrderState] = useState('all');
  const [result, setResult] = useState<Result | null>(null);
  const [resultError, setResultError] = useState('');
  const accountVersion = useRef(0);
  const dataset = datasets.find(value => value.id === selectedDatasetId);
  const completedJobId = jobs.find(job => job.status === 'completed' && job.dataset_id === selectedDatasetId)?.id;
  const refreshAccount = useCallback(async () => {
    const version = ++accountVersion.current;
    try {
      const value = await api<TradeOverview>('/trading/overview');
      const id = value.runs.some(run => run.id === activeRun) ? activeRun : value.runs[0]?.id || '';
      const account = id ? await api<TradeDetail>(`/trading/runs/${id}`) : null;
      if (version !== accountVersion.current) return;
      setOverview(value); setDetail(account); setAccountError('');
      if (id !== activeRun) setActiveRun(id);
    } catch (error) { if (version === accountVersion.current) setAccountError((error as Error).message); }
  }, [activeRun]);
  useEffect(() => {
    let current = true; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await refreshAccount(); if (current) timer = setTimeout(poll, 5000); };
    void poll();
    return () => { current = false; accountVersion.current++; clearTimeout(timer); };
  }, [refreshAccount]);
  useEffect(() => { try { if (activeRun) localStorage.setItem('quant.activeRun.v1', activeRun); } catch { /* Selection works without storage. */ } }, [activeRun]);
  useEffect(() => {
    let current = true; setSnapshot(null); setSnapshotError(''); setSelectedSymbol('');
    if (selectedDatasetId) api<Snapshot>(`/datasets/${selectedDatasetId}/snapshot`)
      .then(value => { if (current) { setSnapshot(value); setSelectedSymbol(value.rows[0]?.symbol || ''); } })
      .catch(error => { if (current) setSnapshotError(error.message); });
    return () => { current = false; };
  }, [selectedDatasetId]);
  useEffect(() => {
    let current = true; setResult(null); setResultError('');
    if (completedJobId) api<Result>(`/jobs/${completedJobId}/result`)
      .then(value => { if (current) setResult(value); }).catch(error => { if (current) setResultError(error.message); });
    return () => { current = false; };
  }, [completedJobId, selectedDatasetId]);

  const holdings = detail?.holdings || [];
  const holding = holdings.find(value => value.symbol === selectedSymbol);
  const quote = snapshot?.rows.find(value => value.symbol === selectedSymbol);
  const filteredRows = (snapshot?.rows || []).filter(row => row.symbol.toLowerCase().includes(search.toLowerCase()) && (watchTab !== 'holdings' || holdings.some(value => value.symbol === row.symbol)));
  const equityRows = useMemo(() => {
    if (!detail) return [];
    let peak = detail.config.initial_cash;
    const rows = detail.daily.map(row => { peak = Math.max(peak, row.equity); return { ...row, nav: row.equity / detail.config.initial_cash, drawdown: row.equity / peak - 1 }; });
    if (range === 'all' || !rows.length) return rows;
    const last = new Date(`${rows[rows.length - 1].date.slice(0, 10)}T00:00:00Z`);
    if (range === 'year') last.setUTCMonth(0, 1); else last.setUTCMonth(last.getUTCMonth() - (range === '1m' ? 1 : 3));
    return rows.filter(row => row.date.slice(0, 10) >= last.toISOString().slice(0, 10));
  }, [detail, range]);
  const lastDays = detail?.daily.slice(-2) || [];
  const lastChange = lastDays.length === 2 ? lastDays[1].equity - lastDays[0].equity : null;
  const positionValue = detail ? holdings.reduce((sum, value) => sum + value.value, 0) : null;
  const sourceKind = dataset?.kind === 'synthetic' ? '合成数据' : dataset ? '历史数据' : '未选择快照';
  const orders = detail?.orders.filter(order => (orderSide === 'all' || order.side === orderSide) && (orderState === 'all' || order.status === orderState)) || [];
  const fills = detail?.fills.filter(fill => orderSide === 'all' || fill.side === orderSide) || [];
  const accountMode = detail?.execution_source === 'broker_observation' ? '券商模拟观察' : detail?.mode === 'paper' ? '日终纸面模拟' : detail ? '历史回放账户' : '未建立账户';
  function openReview() {
    try { localStorage.setItem('quant.review.dataset.v1', selectedDatasetId); localStorage.setItem('quant.review.symbol.v1', selectedSymbol); } catch { /* Review can select a dataset itself. */ }
    onNavigate('chart-review');
  }

  return <div className="terminal-desktop">
    <div className="terminal-account-strip">
      <label>账户<select aria-label="总览策略账户" value={detail?.id || activeRun} onChange={event => { setDetail(null); setActiveRun(event.target.value); }} disabled={!overview?.runs.length}>{!overview?.runs.length && <option value="">未建立账户</option>}{overview?.runs.map(run => <option value={run.id} key={run.id}>{run.name}</option>)}</select></label>
      <div><span>总资产</span><strong>{number(detail?.equity)}</strong></div><div><span>持仓市值</span><strong>{number(positionValue)}</strong></div><div><span>现金余额</span><strong>{number(detail?.cash)}</strong></div><div><span>末日变动</span><strong className={tone(lastChange)}>{lastChange == null ? '—' : `${lastChange > 0 ? '+' : ''}${number(lastChange)}`}</strong></div><span className="terminal-account-date">{accountMode}<span>账户日期 {detail?.day || '—'}</span></span>
    </div>
    <div className="terminal-workgrid">
      <section className="terminal-pane terminal-watchlist">
        <PaneHeading title="行情监控"><button className="icon-button" title="管理数据快照" aria-label="管理数据快照" onClick={() => onNavigate('data')}><ArrowUpRight size={14} /></button></PaneHeading>
        <div className="terminal-watch-toolbar"><label className="terminal-search"><Search size={13} /><input aria-label="筛选证券代码" placeholder="筛选证券代码" value={search} onChange={event => setSearch(event.target.value)} /></label><div className="terminal-segments">{[['watch', '数据池'], ['holdings', '持仓']].map(([value, label]) => <button key={value} aria-pressed={watchTab === value} onClick={() => setWatchTab(value)}>{label}</button>)}</div></div>
        <label className="terminal-dataset-picker"><span>快照</span><select aria-label="总览行情快照" value={selectedDatasetId} onChange={event => onDatasetSelect(event.target.value)} disabled={!datasets.length}>{!datasets.length && <option value="">暂无数据集</option>}{datasets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <div className="terminal-source-caption"><span>{sourceKind} · 非实时</span><span>截至 {dataset?.end || '—'}</span></div>
        {snapshotError && <ErrorMessage message={snapshotError} />}
        <div className="table-scroll terminal-watch-table"><table><thead><tr><th>代码</th><th>收盘价</th><th title="相邻已观测记录的收盘价变动，不保证相邻交易日">观测涨跌</th><th>成交量</th></tr></thead><tbody>{filteredRows.slice(0, 50).map(row => <tr key={row.symbol} className={selectedSymbol === row.symbol ? 'selected' : ''}><td><button className="text-button" aria-pressed={selectedSymbol === row.symbol} onClick={() => setSelectedSymbol(row.symbol)} title={`观测日期 ${shortDate(row.date)}`}>{row.symbol}</button></td><td className={`numeric ${tone(row.change_pct)}`}>{number(row.close)}</td><td className={`numeric ${tone(row.change_pct)}`}>{percent(row.change_pct)}</td><td className="numeric">{row.volume >= 10000 ? `${number(row.volume / 10000, 1)}万` : fmt(row.volume, 0)}</td></tr>)}</tbody></table>{!filteredRows.length && <PaneEmpty action={!datasets.length ? '导入数据' : undefined} onAction={() => onNavigate('data')}>{!datasets.length ? '导入行情后显示证券与观测价格' : snapshot ? watchTab === 'holdings' ? '所选快照没有当前持仓证券' : '没有匹配的证券' : '读取行情快照…'}</PaneEmpty>}</div>
        <div className="terminal-data-status"><h3>数据状态</h3><dl><dt>价格口径</dt><dd>{dataset ? adjustmentNames[dataset.adjustment] || dataset.adjustment : '—'}</dd><dt>交易日历</dt><dd>{dataset ? dataset.quality.calendar_kind === 'provided' ? '随数据提供' : '观测日期并集' : '—'}</dd><dt>基础字段</dt><dd className={dataset?.quality.invalid_rows ? 'market-down' : ''}>{dataset ? dataset.quality.invalid_rows ? `${dataset.quality.invalid_rows} 条异常` : '校验通过' : '待导入'}</dd></dl><p title={dataset?.source}>{dataset?.source || '尚未接入数据源'}</p></div>
      </section>
      <div className="terminal-center-column">
        <section className="terminal-pane terminal-equity-pane">
          <PaneHeading title="组合表现"><span className="terminal-caption">{detail?.name || '账户权益'}</span><button className="icon-button" title="刷新账户" aria-label="刷新总览账户" onClick={() => void refreshAccount()}><RefreshCw size={13} /></button></PaneHeading>
          <div className="terminal-chart-toolbar"><div className="terminal-segments">{[['1m', '1月'], ['3m', '3月'], ['year', '年内'], ['all', '全部']].map(([value, label]) => <button key={value} aria-pressed={range === value} onClick={() => setRange(value)}>{label}</button>)}</div><span className="terminal-chart-legend"><i />账户净值 <small>基准序列未接入</small></span></div>
          {accountError && <ErrorMessage message={accountError} />}
          {equityRows.length ? <div className="terminal-equity-content"><div className="terminal-equity-chart"><Chart x={equityRows.map(row => row.date)} values={equityRows.map(row => row.nav)} label="账户归一化净值" color="#5b85f0" /></div><div className="terminal-drawdown"><div><span>最大回撤</span><strong className="market-down">{percent(detail?.comparison.metrics?.max_drawdown)}</strong><small>{detail?.mode === 'paper' ? '日终纸面模拟' : '固定快照历史回放'}</small></div><Chart x={equityRows.map(row => row.date)} values={equityRows.map(row => row.drawdown)} label="账户回撤" percentage compact color="#d56b67" /></div></div> : <PaneEmpty action="配置模拟账户" onAction={() => onNavigate('strategy')}>{detail ? '运行完成首个交易日后显示真实权益序列' : '选择或建立账户后查看组合净值与回撤'}</PaneEmpty>}
        </section>
        <section className="terminal-pane terminal-factor-pane"><PaneHeading title="因子表现"><span className="terminal-caption">{result ? result.dataset.kind === 'synthetic' ? '合成数据研究' : '所选快照最新研究' : '所选数据快照'}</span><button className="text-button" onClick={() => completedJobId ? onOpenResult(completedJobId) : onNavigate('research')}>{completedJobId ? '完整研究' : '新建研究'}<ArrowUpRight size={12} /></button></PaneHeading>
          {resultError && <ErrorMessage message={resultError} />}
          <div className="table-scroll"><table><thead><tr><th>因子</th><th>Rank IC</th><th title="日度 Rank IC 均值 / 标准差，未年化">ICIR（日）</th><th>有效日</th><th>样本</th></tr></thead><tbody>{result?.summary.map(row => <tr key={row.factor}><td><button className="text-button" onClick={() => completedJobId && onOpenResult(completedJobId)}>{factorNames[row.factor] || row.factor}</button></td><td className="numeric">{number(row.mean_rank_ic, 3)}</td><td className="numeric">{row.mean_rank_ic != null && row.std_rank_ic != null && row.std_rank_ic > 0 ? number(row.mean_rank_ic / row.std_rank_ic, 2) : '—'}</td><td className="numeric">{fmt(row.valid_ic_days, 0)}</td><td><span className={row.valid_ic_days ? 'terminal-data-ready' : 'terminal-data-pending'}>{row.valid_ic_days ? '有样本' : '不足'}</span></td></tr>)}</tbody></table>{!result && <PaneEmpty action="因子研究" onAction={() => onNavigate('research')}>{completedJobId ? '读取研究结果…' : '该快照尚无已完成研究'}</PaneEmpty>}</div>
        </section>
      </div>
      <section className="terminal-pane terminal-preview-pane"><PaneHeading title="调仓预览"><button className="icon-button" title="打开图表复盘" aria-label="打开选中证券图表复盘" disabled={!selectedSymbol} onClick={openReview}><ArrowUpRight size={14} /></button></PaneHeading>
        <div className="terminal-security"><strong>{selectedSymbol || '未选择证券'}</strong><span>{quote ? `${sourceKind}收盘 · ${shortDate(quote.date)}` : '选择左侧证券查看价格'}</span><div className={tone(quote?.change_pct)}>{number(quote?.close)}<small>{percent(quote?.change_pct)}</small></div></div>
        <div className="terminal-price-grid"><dl><dt>开盘</dt><dd>{number(quote?.open)}</dd><dt>最高</dt><dd>{number(quote?.high)}</dd><dt>最低</dt><dd>{number(quote?.low)}</dd><dt>上次观测</dt><dd>{number(quote?.previous_close)}</dd><dt>成交量</dt><dd>{quote ? fmt(quote.volume, 0) : '—'}</dd></dl></div>
        <div className="terminal-position-fields"><div><span>当前持仓</span><strong>{detail ? fmt(holding?.quantity || 0, 0) : '—'}<small>股</small></strong></div><div><span>T+1 可卖</span><strong>{detail ? fmt(holding?.sellable || 0, 0) : '—'}<small>股</small></strong></div><div><span>目标权重</span><strong>{detail && selectedSymbol in detail.targets ? percent(detail.targets[selectedSymbol]) : '—'}</strong></div><div><span>可用资金</span><strong>{number(detail?.available_cash)}</strong></div><button className="terminal-primary-action" onClick={() => onNavigate('strategy')}>{detail ? '预览调仓' : '配置模拟账户'}</button><p className="terminal-caption">复用策略目标与账户记录，历史收盘价仅作观察。</p></div>
        <div className="terminal-execution-status"><h3>运行状态</h3><dl><dt>模拟引擎</dt><dd className={overview?.worker.online ? 'terminal-data-ready' : 'terminal-data-pending'}><i />{overview?.worker.online ? '在线' : '离线'}</dd><dt>账户模式</dt><dd>{accountMode}</dd><dt>运行进度</dt><dd>{detail ? `${detail.cursor} 个交易日` : '—'}</dd><dt>真实提交</dt><dd>关闭</dd></dl></div>
      </section>
    </div>
    <section className="terminal-pane terminal-blotter">
      <div className="terminal-blotter-toolbar"><div className="terminal-blotter-tabs" role="tablist" aria-label="账户记录">{[['orders', '委托'], ['fills', '成交'], ['holdings', '持仓'], ['events', '运行日志']].map(([id, label]) => <button role="tab" aria-selected={orderTab === id} key={id} className={orderTab === id ? 'active' : ''} onClick={() => setOrderTab(id)}>{label}</button>)}</div><div className="terminal-blotter-filters">{orderTab === 'orders' && <select aria-label="筛选委托状态" value={orderState} onChange={event => setOrderState(event.target.value)}><option value="all">全部状态</option>{Array.from(new Set(detail?.orders.map(order => order.status))).map(status => <option value={status} key={status}>{runStatuses[status] || status}</option>)}</select>}{['orders', 'fills'].includes(orderTab) && <select aria-label="筛选委托方向" value={orderSide} onChange={event => setOrderSide(event.target.value)}><option value="all">全部方向</option><option value="buy">买入</option><option value="sell">卖出</option></select>}<button className="icon-button" title="完整订单与成交" aria-label="查看完整订单与成交" onClick={() => onNavigate('orders')}><ArrowUpRight size={13} /></button></div></div>
      <div className="table-scroll terminal-blotter-content">
        {orderTab === 'orders' ? <table><thead><tr><th>日期</th><th>代码</th><th>方向</th><th>委托价</th><th>委托数量</th><th>已成交</th><th>状态</th><th>来源</th></tr></thead><tbody>{orders.map(order => <tr key={order.id}><td>{order.day}</td><td><button className="text-button" onClick={() => setSelectedSymbol(order.symbol)}>{order.symbol}</button></td><td className={order.side === 'buy' ? 'market-up' : 'market-down'}>{order.side === 'buy' ? '买入' : '卖出'}</td><td className="numeric">{number(order.limit_price_rmb, order.price_unit === 'mill' ? 3 : 2)}</td><td className="numeric">{fmt(order.qty, 0)}</td><td className="numeric">{fmt(order.filled, 0)}</td><td><span className={`status ${order.status}`}><i />{runStatuses[order.status] || order.status}</span></td><td>{accountMode}</td></tr>)}</tbody></table>
          : orderTab === 'fills' ? <table><thead><tr><th>日期</th><th>代码</th><th>方向</th><th>成交价</th><th>成交数量</th><th>费用增量</th><th>来源</th></tr></thead><tbody>{fills.map((fill, index) => <tr key={`${fill.order_id}-${index}`}><td>{fill.day}</td><td>{fill.symbol}</td><td className={fill.side === 'buy' ? 'market-up' : 'market-down'}>{fill.side === 'buy' ? '买入' : '卖出'}</td><td className="numeric">{number(fill.price_rmb, fill.price_unit === 'mill' ? 3 : 2)}</td><td className="numeric">{fmt(fill.qty, 0)}</td><td className="numeric">{fill.fees == null ? '未分摊' : number(fill.fees / 100)}</td><td>{accountMode}</td></tr>)}</tbody></table>
          : orderTab === 'holdings' ? <table><thead><tr><th>代码</th><th>总持仓</th><th>当日可卖</th><th>标记价</th><th>市值</th></tr></thead><tbody>{holdings.map(value => <tr key={value.symbol}><td><button className="text-button" onClick={() => setSelectedSymbol(value.symbol)}>{value.symbol}</button></td><td className="numeric">{fmt(value.quantity, 0)}</td><td className="numeric">{fmt(value.sellable, 0)}</td><td className="numeric">{number(value.price, value.price_unit === 'mill' ? 3 : 2)}</td><td className="numeric">{number(value.value)}</td></tr>)}</tbody></table>
          : <table><thead><tr><th>序号</th><th>日期</th><th>事件</th><th>代码</th><th>数量</th><th>说明</th></tr></thead><tbody>{detail?.events.map(event => <tr key={event.sequence}><td>{event.sequence}</td><td>{event.day}</td><td>{runStatuses[event.kind] || ({ targets_generated: '生成次日目标', corporate_action: '公司行动', dividend_paid: '现金分红到账', order_blocked: '交易受限' } as Record<string, string>)[event.kind] || event.kind}</td><td>{event.symbol || '—'}</td><td>{event.qty ?? event.remaining ?? '—'}</td><td>{event.reason || '—'}</td></tr>)}</tbody></table>}
        {!(orderTab === 'orders' ? orders.length : orderTab === 'fills' ? fills.length : orderTab === 'holdings' ? holdings.length : detail?.events.length) && <PaneEmpty>{detail ? '当前筛选下暂无记录' : '建立或选择模拟账户后显示委托、成交与持仓'}</PaneEmpty>}
      </div>
    </section>
  </div>;
}
