import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, CirclePause, CirclePlay, FlaskConical, Plus, RefreshCw, ShieldCheck, X } from 'lucide-react';
import { api, post } from '../api';
import { Chart } from '../components/Chart';
import { ResearchReplays } from '../components/ResearchReplays';
import { AnnualQualityResearch } from '../components/AnnualQualityResearch';
import { SignalPreview } from '../components/SignalPreview';
import { BrokerHistory } from '../components/BrokerHistory';
import { OfflineOperatorBoard } from '../components/OfflineOperatorBoard';
import { CurrentResearchEvidence } from '../components/CurrentResearchEvidence';
import { LeaderEnvironmentWorkbench } from '../components/LeaderEnvironmentWorkbench';
import { TradeCaseLearning } from '../components/TradeCaseLearning';
import { OpeningDailyStatus } from '../components/OpeningDailyStatus';
import { PaperAccountContinuity } from '../components/PaperAccountContinuity';
import { ResearchCandidateEvidence } from '../components/ResearchCandidateEvidence';
import { PortfolioRealization } from '../components/PortfolioRealization';
import { ErrorMessage, Warning } from '../components/Common';
import { fmt } from '../types';
import { runStatuses, signalNames, type Comparison, type Config, type Run, type TradeDetail, type TradeOverview } from '../tradingTypes';

const titles: Record<string, string> = { overview: '运行总览', strategy: '策略与目标', orders: '订单与成交', comparison: '研究比较' };
const events: Record<string, string> = { submitted: '已报', partial: '部分成交', filled: '全部成交', cancel_pending: '撤单请求', cancelled: '撤单确认', targets_generated: '生成次日目标', corporate_action: '除权除息', dividend_paid: '现金分红到账', order_blocked: '交易受限', broker_opening_balance: '券商期初余额', broker_fee_adjustment: '券商累计费用', broker_receipt_applied: '券商回读入账', broker_order_terminal: '跨日终态回读', broker_cash_movement: '券商资金流水', broker_session_reconciled: '交易日余额核对' };
const pct = (value: number | null | undefined) => value == null ? '--' : `${fmt(value * 100, 2)}%`;
const ratio = (value: number | null | undefined) => value == null ? '--' : fmt(value, 3);

function NewRun({ overview, close, created }: { overview: TradeOverview; close: () => void; created: (run: Run) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef<{ body: string; key: string } | null>(null);
  const [config, setConfig] = useState<Config>(overview.default_config);
  const [mode, setMode] = useState('replay');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => { dialog.current?.showModal(); }, []);
  function number(name: keyof Config, value: string) { setConfig(c => ({ ...c, [name]: Number(value) })); }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return; setBusy(true); setError('');
    const fields = new FormData(event.currentTarget);
    const body = { name: fields.get('name'), mode, start: fields.get('start'), end: fields.get('end'), config };
    const signature = JSON.stringify(body);
    if (key.current?.body !== signature) key.current = { body: signature, key: crypto.randomUUID() };
    try { created(await post<Run>('/trading/runs', body, key.current.key)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <dialog ref={dialog} className="import-dialog trade-dialog" onCancel={close} aria-label="新建策略运行"><div className="dialog-heading"><h2>新建策略运行</h2><button aria-label="关闭运行配置" title="关闭" className="icon-button" onClick={close} disabled={busy}><X size={18} /></button></div>
    <form onSubmit={submit}>
      <label>运行名称<input name="name" defaultValue="中期强势组合 V1" required maxLength={80} /></label>
      <div className="form-columns"><label>运行模式<select aria-label="运行模式" value={mode} onChange={e => setMode(e.target.value)}><option value="replay">历史回放</option><option value="paper">日终纸面模拟</option></select></label><label>信号<select aria-label="信号类型" value={config.signal} onChange={e => setConfig(c => ({ ...c, signal: e.target.value }))}>{Object.entries(signalNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label></div>
      <div className="form-columns" key={mode}><label>开始日期<input type="date" name="start" defaultValue={mode === 'replay' ? overview.dataset.start : overview.server_date} required /></label><label>结束日期<input type="date" name="end" defaultValue={mode === 'replay' ? overview.dataset.end : `${Number(overview.server_date.slice(0, 4)) + 1}${overview.server_date.slice(4)}`} required /></label></div>
      <div className="form-columns"><label>模拟初始资金<input type="number" value={config.initial_cash} min={1000} max={10000000} step={1000} onChange={e => number('initial_cash', e.target.value)} required /></label><label>持仓数量<input type="number" value={config.holdings} min={1} max={20} onChange={e => number('holdings', e.target.value)} required /></label><label>保留名次上限<input type="number" value={config.retain_rank} min={config.holdings} max={100} onChange={e => number('retain_rank', e.target.value)} required /></label><label>调仓间隔（交易日）<input type="number" value={config.rebalance_sessions} min={1} max={60} onChange={e => number('rebalance_sessions', e.target.value)} required /></label><label>总仓位上限<input type="number" value={config.gross_limit} min={0.01} max={1} step={0.01} onChange={e => number('gross_limit', e.target.value)} required /></label><label>单只上限<input type="number" value={config.single_limit} min={0.01} max={1} step={0.01} onChange={e => number('single_limit', e.target.value)} required /></label></div>
      <Warning>{mode === 'paper' ? '日终纸面模拟：工作日 16:15 后更新日线并回放已结束交易日；不是盘中实时模拟。' : '历史十股验证池；收益不代表全市场，不构成策略有效性结论。'}</Warning>
      {error && <ErrorMessage message={error} />}<div className="dialog-actions"><span className="subtle">真实券商连接与下单关闭</span><button className="primary" disabled={busy} type="submit"><CirclePlay size={16} />{busy ? '提交中' : '创建并运行'}</button></div>
    </form></dialog>;
}

function Holdings({ detail }: { detail: TradeDetail }) {
  return <div className="table-scroll"><table><thead><tr><th>证券</th><th>总持仓</th><th>当日可卖</th><th>标记价</th><th>市值</th></tr></thead><tbody>{detail.holdings.map(p => <tr key={p.symbol}><td>{p.symbol}</td><td>{fmt(p.quantity, 0)}</td><td>{fmt(p.sellable, 0)}</td><td>{fmt(p.price, p.price_unit === 'mill' ? 3 : 2)}</td><td>{fmt(p.value, 2)}</td></tr>)}</tbody></table>{!detail.holdings.length && <div className="empty small-empty">空仓</div>}</div>;
}

export function Trading({ page }: { page: string }) {
  const [overview, setOverview] = useState<TradeOverview | null>(null);
  const [active, setActive] = useState(() => localStorage.getItem('quant.activeRun.v1') || '');
  const [detail, setDetail] = useState<TradeDetail | null>(null);
  const [comparisons, setComparisons] = useState<Comparison[]>([]);
  const [error, setError] = useState(''); const [modal, setModal] = useState(false); const [busy, setBusy] = useState(false);
  const [orderTab, setOrderTab] = useState('orders');
  const requestVersion = useRef(0);
  const brokerBound = detail?.execution_source === 'broker_observation';
  const refresh = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      const state = await api<TradeOverview>('/trading/overview');
      if (version !== requestVersion.current) return;
      const id = state.runs.some(r => r.id === active) ? active : state.runs[0]?.id;
      const [value, compare] = await Promise.all([id ? api<TradeDetail>(`/trading/runs/${id}`) : Promise.resolve(null), page === 'comparison' ? api<Comparison[]>('/trading/comparison') : Promise.resolve([])]);
      if (version !== requestVersion.current) return;
      setOverview(state); if (id && id !== active) setActive(id);
      setDetail(value); if (page === 'comparison') setComparisons(compare); setError('');
    } catch (e) { if (version === requestVersion.current) throw e; }
  }, [active, page]);
  useEffect(() => { let live = true; let timer: ReturnType<typeof setTimeout>; const poll = async () => { try { if (live) await refresh(); } catch (e) { if (live) setError((e as Error).message); } finally { if (live) timer = setTimeout(poll, 2000); } }; poll(); return () => { live = false; requestVersion.current++; clearTimeout(timer); }; }, [refresh]);
  useEffect(() => { if (active) localStorage.setItem('quant.activeRun.v1', active); }, [active]);
  async function control() { if (!detail) return; setBusy(true); try { await post(`/trading/runs/${detail.id}/${detail.status === 'paused' ? 'resume' : 'pause'}`); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  async function baselines() {
    if (!overview || !detail) return; setBusy(true); setError('');
    try { for (const signal of ['momentum', 'equal_weight']) await post<Run>('/trading/runs', { name: `${signalNames[signal]} · 同参数`, mode: 'replay', reference_run: detail.id, start: detail.start, end: detail.end, config: { ...detail.config, signal } }, `baseline-${detail.id}-${signal}`); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <><header className="page-header"><h1>{titles[page]}</h1><div className="actions">{page === 'comparison' && <button onClick={() => document.getElementById('trade-case-learning')?.scrollIntoView({ block: 'start', behavior: 'smooth' })}>逐案档案</button>}<button className="icon-button" aria-label="刷新交易状态" title="刷新" onClick={() => refresh().catch(e => setError(e.message))}><RefreshCw size={17} /></button><button className="primary" onClick={() => setModal(true)} disabled={!overview || overview.dataset.status !== 'ready'}><Plus size={16} />新建运行</button></div></header>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>模拟账户</strong><span>真实下单关闭</span><span className={overview?.worker.online && !error ? 'engine-online' : ''}>交易进程 {overview?.worker.online && !error ? '在线' : '离线'}</span></div>
    {error && <ErrorMessage message={error} />}
    {overview?.worker.refresh?.status === 'updating' && <Warning>正在获取日终行情，账户暂按最后有效日期显示。</Warning>}
    {overview?.worker.refresh?.status === 'failed' && <ErrorMessage message={`日终更新失败，保留上次数据：${overview.worker.refresh.error}`} />}
    {overview && <div className="trade-source">{page === 'comparison' && <strong>默认运行样本（各项研究范围见对应结果）</strong>}历史成分日期 {overview.dataset.selection_date || '--'}<span>数据截止 {overview.dataset.end || '--'}</span><span>{overview.dataset.symbols?.length || 0} 只证券</span><span>{fmt(overview.dataset.rows || 0, 0)} 条日线</span></div>}
    {overview?.dataset.status !== 'ready' && overview && <Warning>行情未就绪：{overview.dataset.error || overview.dataset.status}</Warning>}
    {page === 'comparison' && <><ResearchCandidateEvidence /><PortfolioRealization /><OpeningDailyStatus /><LeaderEnvironmentWorkbench /><TradeCaseLearning /><CurrentResearchEvidence /><OfflineOperatorBoard /><AnnualQualityResearch /><ResearchReplays /></>}
    {page === 'overview' && <PaperAccountContinuity />}
    <label className="inline-field trade-run-select">运行<select aria-label="当前策略运行" value={detail?.id || active} onChange={e => { setDetail(null); setActive(e.target.value); }}><option value="" disabled>选择运行</option>{overview?.runs.map(run => <option key={run.id} value={run.id}>{run.name} · {runStatuses[run.status]} · {run.id.slice(0, 6)}</option>)}</select></label>
    {!detail ? <div className="empty">{overview?.runs.length ? '读取账户状态' : '暂无策略运行'}</div> : <>
      <div className="trade-run-state"><span className={`status ${detail.status}`}><i />{runStatuses[detail.status]}</span><span>{brokerBound ? '券商模拟账户观察' : detail.mode === 'paper' ? '日终纸面模拟' : '历史回放'}</span><span>{brokerBound ? `估值观察 ${detail.valuation_observed_at || '--'}` : `已处理 ${detail.cursor} 个交易日`}</span><span>账户日期 {detail.day || '尚无成交日'}</span>{!brokerBound && ['running', 'paused'].includes(detail.status) && <button className="icon-button" aria-label={detail.status === 'paused' ? '恢复运行' : '暂停运行'} title={detail.status === 'paused' ? '恢复运行' : '暂停运行'} onClick={control} disabled={busy}>{detail.status === 'paused' ? <CirclePlay size={19} /> : <CirclePause size={19} />}</button>}</div>
      {detail.error && <ErrorMessage message={detail.error} />}
      {detail.mode === 'paper' && !detail.day && <Warning>等待新交易日。工作日 16:15 后获取日线；历史数据不会自动算作本账户的过去成交。</Warning>}
      {page === 'overview' && <>
        <div className="metrics trade-metrics"><div><span>模拟总权益</span><strong>{fmt(detail.equity)}</strong></div><div><span>可用现金</span><strong>{fmt(detail.available_cash)}</strong></div><div><span>累计费用</span><strong>{fmt(brokerBound ? detail.accounted_fees : detail.comparison.metrics?.fees || 0)}</strong></div><div><span>{brokerBound ? '已核验净收益序列' : detail.mode === 'paper' ? '纸面模拟净收益' : '历史回放净收益'}</span><strong>{pct(detail.comparison.metrics?.net_return)}</strong></div></div>
        <section className="trade-section"><h2>账户权益 <span>{detail.name}</span></h2>{detail.daily.length ? <Chart x={detail.daily.map(d => d.date)} values={detail.daily.map(d => d.equity)} label="模拟账户权益" /> : <div className="empty small-empty">暂无权益序列</div>}</section>
        <section className="trade-section"><h2>当前持仓</h2><Holdings detail={detail} /></section>
        <section className="trade-section"><h2>连接与账户</h2><dl><dt>券商连接</dt><dd>{brokerBound ? '导出回读；本机未核验' : '未授权、未连接'}</dd><dt>订单入口</dt><dd>{brokerBound ? '人工提交；自动提交关闭' : '统一模拟订单管理'}</dd><dt>现金分红应收</dt><dd>{fmt(detail.receivables)}</dd><dt>委托 / 成交回报</dt><dd>{detail.order_count} / {detail.fill_count}</dd></dl></section>
      </>}
      {page === 'strategy' && <>
        {brokerBound && <SignalPreview key={detail.id} runId={detail.id} observedAt={detail.valuation_observed_at} configIdentity={JSON.stringify(detail.config)} />}
        <div className="trade-source">最近信号 {detail.last_signal_date || '--'}<span>下一次信号：{detail.status === 'completed' ? '本次回放已结束' : detail.next_rebalance_in_sessions ? `再处理 ${detail.next_rebalance_in_sessions} 个交易日后` : '首个交易日收盘'}</span></div>
        <section className="trade-section"><h2>{signalNames[detail.config.signal]}</h2><dl><dt>中期区间 / 跳过近期</dt><dd>{detail.config.lookback} / {detail.config.skip} 个交易日</dd><dt>波动率窗口</dt><dd>{detail.config.volatility_window} 个交易日</dd><dt>调仓 / 保留名次</dt><dd>每 {detail.config.rebalance_sessions} 日 / 前 {detail.config.retain_rank} 名</dd><dt>持仓 / 单只 / 总仓位</dt><dd>{detail.config.holdings} 只 / {pct(detail.config.single_limit)} / {pct(detail.config.gross_limit)}</dd><dt>20 日成交额中位数</dt><dd>至少 {fmt(detail.config.min_amount, 0)} 元</dd><dt>买入 / 持有 / 退出</dt><dd>合格信号前列 / 名次缓冲 / 跌出保留范围或失去资格</dd><dt>执行时点</dt><dd>收盘生成目标，下一交易日开盘模拟成交</dd></dl></section>
        <section className="trade-section"><h2>最近候选与目标</h2><div className="table-scroll"><table><thead><tr><th>名次</th><th>证券</th><th>分数</th><th>中期收益</th><th>波动率</th><th>目标权重</th></tr></thead><tbody>{detail.candidates.map((c, i) => <tr key={c.symbol}><td>{i + 1}</td><td>{c.symbol}</td><td>{fmt(c.score, 4)}</td><td>{pct(c.momentum)}</td><td>{pct(c.volatility)}</td><td>{pct(detail.targets[c.symbol] || 0)}</td></tr>)}</tbody></table></div>{!detail.candidates.length && <div className="empty small-empty">无合格候选，保留现金</div>}</section><section className="trade-section"><h2>实际持仓</h2><Holdings detail={detail} /></section>
      </>}
      {page === 'orders' && <>
        {brokerBound && <BrokerHistory key={detail.id} runId={detail.id} />}
        <div className="tabs">{[['orders', '委托'], ['fills', '成交'], ['events', '回报事件']].map(([id, label]) => <button key={id} onClick={() => setOrderTab(id)} className={orderTab === id ? 'active' : ''}>{label}</button>)}</div>
        <div className="table-scroll">{orderTab === 'orders' ? <table><thead><tr><th>日期</th><th>证券</th><th>方向</th><th>申报数量</th><th>成交数量</th><th>限价</th><th>状态</th><th>原因</th></tr></thead><tbody>{detail.orders.map(o => <tr key={o.id}><td>{o.day}</td><td>{o.symbol}</td><td>{o.side === 'buy' ? '买入' : '卖出'}</td><td>{o.qty}</td><td>{o.filled}</td><td>{fmt(o.limit_price_rmb, o.price_unit === 'mill' ? 3 : 2)}</td><td>{runStatuses[o.status]}</td><td>{o.reason || '--'}</td></tr>)}</tbody></table> : orderTab === 'fills' ? <table><thead><tr><th>日期</th><th>证券</th><th>方向</th><th>成交股数</th><th>价格</th><th>费用增量</th></tr></thead><tbody>{detail.fills.map((f, i) => <tr key={`${f.order_id}-${i}`}><td>{f.day}</td><td>{f.symbol}</td><td>{f.side === 'buy' ? '买入' : '卖出'}</td><td>{f.qty}</td><td>{fmt(f.price_rmb, f.price_unit === 'mill' ? 3 : 2)}</td><td>{f.fees == null ? '未分摊' : fmt(f.fees / 100, 2)}</td></tr>)}</tbody></table> : <table><thead><tr><th>序号</th><th>日期</th><th>事件</th><th>证券</th><th>数量</th><th>原因</th></tr></thead><tbody>{detail.events.map(e => <tr key={e.sequence}><td>{e.sequence}</td><td>{e.day}</td><td>{events[e.kind] || e.kind}</td><td>{e.symbol || '--'}</td><td>{e.qty ?? e.remaining ?? '--'}</td><td>{e.reason || '--'}</td></tr>)}</tbody></table>}</div>
      </>}
      {page === 'comparison' && <>
        <div className="comparison-actions"><button onClick={baselines} disabled={busy || detail.mode !== 'replay'}><FlaskConical size={16} />运行同参数对照</button><span className="subtle">已完成运行 · 相同数据快照、日期、资金和成本</span></div>
        <div className="table-scroll"><table><thead><tr><th>运行</th><th>状态</th><th>净收益</th><th>最大回撤</th><th title="净值日收益，年化252个交易日，无风险收益为0">夏普</th><th>年化波动</th><th>最差单日</th><th>费用</th><th>交易日</th></tr></thead><tbody>{comparisons.filter(c => c.status === 'completed' && c.metrics && c.dataset === detail.dataset && c.start === detail.start && c.end === detail.end && Object.entries(c.config).every(([key, value]) => key === 'signal' || detail.config[key as keyof Config] === value)).map(c => <tr key={c.id}><td><button className="text-button" onClick={() => { setDetail(null); setActive(c.id); }}>{c.name}</button></td><td>{runStatuses[c.status]}</td><td>{pct(c.metrics?.net_return)}</td><td>{pct(c.metrics?.max_drawdown)}</td><td>{ratio(c.metrics?.sharpe_zero_rf_252)}</td><td>{pct(c.metrics?.annualized_volatility)}</td><td>{pct(c.metrics?.worst_day)}</td><td>{fmt(c.metrics?.fees || 0)}</td><td>{c.metrics?.sessions}</td></tr>)}</tbody></table></div>
        <section className="trade-section"><h2>当前运行分年记录</h2><div className="table-scroll"><table><thead><tr><th>年份</th><th>用途</th><th>净收益</th><th>最大回撤</th><th title="净值日收益，年化252个交易日，无风险收益为0">夏普</th><th>年化波动</th><th>最差单日</th><th>费用</th></tr></thead><tbody>{detail.comparison.segments.map(s => <tr key={s.year}><td>{s.year}</td><td>{s.segment === 'development' ? '开发段' : s.segment === 'validation' ? '验证段' : '未调参测试段'}</td><td>{pct(s.net_return)}</td><td>{pct(s.max_drawdown)}</td><td>{ratio(s.sharpe_zero_rf_252)}</td><td>{pct(s.annualized_volatility)}</td><td>{pct(s.worst_day)}</td><td>{fmt(s.fees)}</td></tr>)}</tbody></table></div></section><Warning>此表仅比较所选回放账户。Alpha158 / LightGBM 独立实验不等于账户准入，历史指标不保证未来收益。</Warning>
      </>}
      {detail.status === 'completed' && <div className="downloads">{[['daily.csv', '每日权益'], ['orders.csv', '全部委托'], ['fills.csv', '全部成交'], ['summary.json', '研究汇总'], ['account.json', '完整账户']].map(([file, label]) => <a className="button" download key={file} href={`/api/trading/runs/${detail.id}/files/${file}`}><ArrowDownToLine size={15} />{label}</a>)}</div>}
      <div className="warnings">{brokerBound ? <Warning>券商模拟账户导出观察，尚未完成本机字段联调。费用按委托累计，逐笔未分摊；股份变动尚未验收，自动提交关闭。</Warning> : <><Warning>历史十股工程验证池，不是全市场策略验证。日线开盘撮合、成交量上限和固定滑点不能复制真实排队成交。</Warning><Warning>使用不复权成交价，分红按登记、除息、到账日期处理；税费和分红税准备金为研究假设，未核验为个人账户实际费用。</Warning></>}</div>
    </>}
    {modal && overview && <NewRun overview={overview} close={() => setModal(false)} created={run => { requestVersion.current++; setDetail(null); setActive(run.id); setModal(false); }} />}
  </>;
}
