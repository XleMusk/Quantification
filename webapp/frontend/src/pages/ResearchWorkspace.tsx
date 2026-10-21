import { useEffect, useMemo, useState } from 'react';
import { BarChart3, CheckCircle2, Database, Layers3, RefreshCw, Search, ShieldCheck } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage, Loading } from '../components/Common';
import './ResearchWorkspace.css';

type Metrics = {
  net_return: number | null; max_drawdown: number | null; sharpe_zero_rf_252: number | null;
  fees: number | null; turnover_over_initial: number | null; completed_sessions: number | null;
};
type Basis = {
  data_fingerprint: string | null; universe: string | null; start: string | null; end: string | null;
  costs: Record<string, number> | null;
};
type Capacity = {
  initial_cash: number | null; gross_limit: number | null; single_limit: number | null;
  participation: number | null; holdings: number | null; min_amount: number | null;
};
type Experiment = {
  id: string; name: string; study: string; kind: 'saved_experiment' | 'candidate_reference'; status: string;
  start: string | null; end: string | null; complete: boolean; metrics: Metrics | null;
  source: { path: string; sha256: string | null } | null; basis: Basis;
  comparison_key: string | null; missing_comparison_fields: string[]; capacity: Capacity | null; caveats: string[];
  observed_costs?: { fees: number | null; turnover_value: number | null; completed_sessions: number | null; basis: 'completed_prefix_only' | 'full_period' };
};
type Position = {
  symbol: string; quantity: number | null; sellable: number | null; value: number | null;
  weight: number | null; liquidity_participation: number | null;
};
type Portfolio = {
  id: string; name: string; mode: string; status: string; day: string | null; equity: number | null;
  cash: number | null; available_cash: number | null; gross_exposure: number | null; largest_weight: number | null;
  top5_weight: number | null; hhi: number | null; effective_positions: number | null; fees: number | null;
  position_count: number | null; positions: Position[]; warnings: string[]; source: 'shared_ledger';
};
type Workspace = {
  schema: string; status: string; served_at: string; experiments: Experiment[]; portfolios: Portfolio[]; warnings: string[];
};

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const number = (value: unknown, digits = 2) => finite(value)
  ? value.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits }) : '—';
const percent = (value: unknown) => finite(value) ? `${number(value * 100)}%` : '—';
const money = (value: unknown) => finite(value) ? `¥${number(value)}` : '—';
const clock = (value: string | null | undefined) => value ? value.replace('T', ' ').slice(0, 19) : '—';
const span = (start: string | null, end: string | null) => start || end ? `${start || '未提供'} — ${end || '未提供'}` : '区间未提供';
const statusNames: Record<string, string> = {
  completed: '已完成', complete: '已完成', finished: '已完成', partial: '部分完成', source_stopped: '来源中断',
  failed: '运行失败', failed_replay: '回放未完成', running: '运行中', queued: '等待计算', pending: '待研究',
  reference: '研究条目', unavailable: '结果不可用', unknown: '状态待核', saved: '已保存',
};
const modeNames: Record<string, string> = { historical: '历史回放', replay: '历史回放', paper: '纸面账户', synthetic: '合成验收' };
const missingNames: Record<string, string> = {
  data_fingerprint: '行情版本', universe: '股票池', start: '开始日期', end: '结束日期', costs: '费用与执行参数',
};
const labelFor = (item: Experiment) => `${item.name} · ${item.start || '日期未提供'}`;
const savedMetric = (item: Experiment | undefined, field: keyof Metrics) => item?.complete ? item.metrics?.[field] : null;
const tone = (value: unknown) => finite(value) ? value > 0 ? 'rw-up' : value < 0 ? 'rw-down' : '' : '';

function Status({ item }: { item: Experiment }) {
  const value = item.complete ? 'complete' : item.kind === 'candidate_reference' ? 'unavailable' : item.status;
  const label = item.complete ? '完整结果' : item.kind === 'candidate_reference' ? '研究条目' : statusNames[item.status] || item.status;
  return <span className={`rw-tag ${value}`}><i className="rw-status-dot" />{label}</span>;
}

function SourceDetails({ item, slot }: { item: Experiment; slot?: string }) {
  return <details className="rw-source-detail"><summary>{slot ? `${slot} · ` : ''}数据、费用与容量设置</summary>
    <dl className="rw-facts">
      <dt>原始结果</dt><dd><code>{item.source?.path || '来源未提供'}</code></dd>
      <dt>文件版本</dt><dd><code>{item.source?.sha256 || '未提供'}</code></dd>
      <dt>数据版本</dt><dd><code>{item.basis.data_fingerprint || '未提供'}</code></dd>
      <dt>证券范围</dt><dd>{item.basis.universe || '未提供'}</dd>
      <dt>研究区间</dt><dd>{span(item.basis.start, item.basis.end)}</dd>
      <dt>费用与执行参数</dt><dd><code>{item.basis.costs ? JSON.stringify(item.basis.costs) : '未保存完整设置'}</code></dd>
      {item.observed_costs && <><dt>{item.observed_costs.basis === 'completed_prefix_only' ? '已完成前缀费用' : '完整区间费用'}</dt><dd>{money(item.observed_costs.fees)}</dd><dt>已保存成交金额</dt><dd>{money(item.observed_costs.turnover_value)}</dd><dt>已保存会话</dt><dd>{number(item.observed_costs.completed_sessions, 0)}</dd></>}
      <dt>初始资金</dt><dd>{money(item.capacity?.initial_cash)}</dd>
      <dt>总仓位 / 单只上限</dt><dd>{percent(item.capacity?.gross_limit)} / {percent(item.capacity?.single_limit)}</dd>
      <dt>成交量参与率上限</dt><dd>{percent(item.capacity?.participation)}</dd>
      <dt>目标证券数</dt><dd>{number(item.capacity?.holdings, 0)}</dd>
      <dt>最低日成交额</dt><dd>{money(item.capacity?.min_amount)}</dd>
    </dl>
    {item.caveats.map((text, index) => <p key={`${index}:${text}`} className="rw-warning">{text}</p>)}
  </details>;
}

export function ResearchWorkspace() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [leftId, setLeftId] = useState('');
  const [rightId, setRightId] = useState('');
  const [portfolioId, setPortfolioId] = useState('');

  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError('');
    api<Workspace>('/research-workspace', { signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setWorkspace(value); })
      .catch(error => { if (!abort.signal.aborted) setError((error as Error).message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [refresh]);

  const experiments = workspace?.experiments || [];
  const portfolios = workspace?.portfolios || [];
  const left = experiments.find(item => item.id === leftId);
  const right = experiments.find(item => item.id === rightId);
  const portfolio = portfolios.find(item => item.id === portfolioId) || portfolios[0];
  const filtered = useMemo(() => {
    const search = query.trim().toLowerCase();
    return experiments.filter(item => (!search || [item.name, item.study, item.status, item.source?.path].join(' ').toLowerCase().includes(search))
      && (filter === 'all' || (filter === 'complete' ? item.complete : filter === 'reference' ? item.kind === 'candidate_reference' : !item.complete && item.kind === 'saved_experiment')));
  }, [experiments, query, filter]);
  const complete = experiments.filter(item => item.complete).length;
  const comparableGroups = useMemo(() => {
    const groups = new Map<string, number>();
    for (const item of experiments) if (item.complete && item.comparison_key) groups.set(item.comparison_key, (groups.get(item.comparison_key) || 0) + 1);
    return [...groups.values()].filter(count => count >= 2).length;
  }, [experiments]);
  const sameBasis = !!left && !!right && left.id !== right.id && !!left.comparison_key && left.comparison_key === right.comparison_key;
  const comparable = sameBasis && left!.complete && right!.complete;
  const deltaReturn = comparable && finite(left!.metrics?.net_return) && finite(right!.metrics?.net_return)
    ? left!.metrics!.net_return! - right!.metrics!.net_return! : null;
  const reasons = left && right ? [...new Set([...left.missing_comparison_fields, ...right.missing_comparison_fields])].map(key => missingNames[key] || key) : [];

  function choose(slot: 'left' | 'right', id: string) {
    if (slot === 'left') { setLeftId(id); if (id && id === rightId) setRightId(''); }
    else { setRightId(id); if (id && id === leftId) setLeftId(''); }
  }

  return <div className="research-workspace">
    <header className="page-header"><div><h1>研究比较与组合风险</h1><p>读取已保存的研究结果、费用假设与共享账本；各账户资金独立展示。</p></div>
      <div className="actions"><span className="subtle">{workspace ? `读取于 ${clock(workspace.served_at)}` : '等待保存结果'}</span><button onClick={() => setRefresh(value => value + 1)} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新结果</button></div>
    </header>
    {error && <ErrorMessage message={error} />}
    {loading && !workspace ? <Loading /> : <>
      <div className="rw-metrics"><div><span>保存研究 / 候选条目</span><strong>{experiments.length}</strong></div><div><span>完整账户结果</span><strong>{complete}</strong></div><div><span>同口径可比较组</span><strong>{comparableGroups}</strong></div><div><span>共享账本账户</span><strong>{portfolios.length}</strong></div></div>
      <section className="rw-section" aria-label="保存研究结果">
        <div className="rw-section-heading"><h2><Database size={15} />保存研究</h2><span>完整净收益按原结果读取，来源中断的记录保留原状态</span></div>
        <div className="rw-section-body">
          <div className="rw-toolbar"><label className="rw-search"><Search size={14} /><input aria-label="搜索保存研究" value={query} onChange={event => setQuery(event.target.value)} placeholder="方法、研究名称或来源" /></label>
            <label>结果<select aria-label="筛选研究状态" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部记录</option><option value="complete">完整账户结果</option><option value="partial">未完成结果</option><option value="reference">候选研究条目</option></select></label><span className="subtle">{filtered.length} / {experiments.length} 条</span></div>
          {filtered.length ? <div className="table-scroll"><table className="compact rw-table" aria-label="保存研究比较表"><thead><tr><th>研究 / 方法</th><th>状态</th><th>研究区间</th><th>净收益</th><th>最大回撤</th><th>已保存区间费用（元）</th><th>资金 / 参与率</th><th>对照</th></tr></thead><tbody>{filtered.map(item => <tr key={item.id} className={item.id === leftId || item.id === rightId ? 'rw-selected' : ''}>
            <td><strong>{item.name}</strong><small>{item.study}</small></td><td><Status item={item} /></td><td>{span(item.start, item.end)}<small>已完成会话 {number(savedMetric(item, 'completed_sessions'), 0)}</small></td>
            <td className={`numeric ${tone(savedMetric(item, 'net_return'))}`}>{percent(savedMetric(item, 'net_return'))}</td><td className="numeric">{percent(savedMetric(item, 'max_drawdown'))}</td><td className="numeric">{number(item.metrics?.fees ?? item.observed_costs?.fees)}{item.observed_costs?.basis === 'completed_prefix_only' && <small>仅已完成前缀</small>}</td>
            <td className="numeric">{money(item.capacity?.initial_cash)}<small>{percent(item.capacity?.participation)}</small></td><td><div className="rw-select-controls"><button aria-label={`选为对照 A：${item.name}`} aria-pressed={item.id === leftId} onClick={() => choose('left', item.id)}>A</button><button aria-label={`选为对照 B：${item.name}`} aria-pressed={item.id === rightId} onClick={() => choose('right', item.id)}>B</button></div></td>
          </tr>)}</tbody></table></div> : <div className="rw-empty"><Layers3 size={18} />{experiments.length ? '没有匹配的研究记录。' : '尚无可读取的保存研究。完成研究或导入原结果后，可在这里比较。'}</div>}
        </div>
      </section>

      <section className="rw-section" aria-label="两组研究对照">
        <div className="rw-section-heading"><h2><BarChart3 size={15} />两组研究对照</h2><span>行情版本、股票池、日期、费用与执行设置一致后显示差异</span></div>
        <div className="rw-section-body"><div className="rw-pair-controls">
          <label>对照 A<select aria-label="研究对照 A" value={left?.id || ''} onChange={event => choose('left', event.target.value)}><option value="">选择研究</option>{experiments.map(item => <option key={item.id} value={item.id}>{labelFor(item)}</option>)}</select></label>
          <label>对照 B<select aria-label="研究对照 B" value={right?.id || ''} onChange={event => choose('right', event.target.value)}><option value="">选择研究</option>{experiments.map(item => <option key={item.id} value={item.id}>{labelFor(item)}</option>)}</select></label>
        </div>
        {left && right ? <>
          <div className={`rw-comparison-state ${comparable ? '' : 'partial'}`} role="status">{comparable ? <CheckCircle2 size={16} /> : <Layers3 size={16} />}<div>
            <p>{comparable ? '同口径完整结果，可以比较净收益与风险。' : sameBasis ? '数据与配置相同，等待两组完整账户结果。' : '两组尚未具备一致且完整的比较条件。'}</p>
            {!sameBasis && <p>查看下方原始数据版本、证券范围、日期与费用设置，核对差异。</p>}
            {reasons.length > 0 && <p>缺少比较信息：{reasons.join('、')}。</p>}
          </div></div>
          <div className="rw-pair-grid"><div><span>A · 净收益</span><strong className={tone(savedMetric(left, 'net_return'))}>{percent(savedMetric(left, 'net_return'))}</strong><small>{left.name}</small></div><div><span>B · 净收益</span><strong className={tone(savedMetric(right, 'net_return'))}>{percent(savedMetric(right, 'net_return'))}</strong><small>{right.name}</small></div><div><span>配对净收益差 A − B</span><strong className={tone(deltaReturn)}>{finite(deltaReturn) ? `${number(deltaReturn * 100)} 个百分点` : '—'}</strong><small>{comparable ? '使用各自完整净收益' : '比较条件未齐'}</small></div>
            <div><span>最大回撤 A / B</span><strong>{percent(savedMetric(left, 'max_drawdown'))} / {percent(savedMetric(right, 'max_drawdown'))}</strong></div><div><span>Sharpe A / B</span><strong>{number(savedMetric(left, 'sharpe_zero_rf_252'))} / {number(savedMetric(right, 'sharpe_zero_rf_252'))}</strong><small>原结果零无风险利率、252 日口径</small></div><div><span>累计费用 A / B</span><strong>{money(savedMetric(left, 'fees'))} / {money(savedMetric(right, 'fees'))}</strong></div></div>
          <SourceDetails item={left} slot="A" /><SourceDetails item={right} slot="B" />
        </> : <p className="rw-note">从保存研究中选择 A 和 B。未完成账户、候选条目和不同数据口径仍可查看，完整差异在条件齐备时显示。</p>}
        </div>
      </section>

      <section className="rw-section" aria-label="组合风险与资金容量">
        <div className="rw-section-heading"><h2><ShieldCheck size={15} />组合风险与资金容量</h2><span>共享 Ledger 保存状态 · 逐账户查看</span></div>
        <div className="rw-section-body">{portfolio ? <>
          <div className="rw-portfolio-controls"><label>账户<select aria-label="组合风险账户" value={portfolio.id} onChange={event => setPortfolioId(event.target.value)}>{portfolios.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><div className="rw-portfolio-status"><span>{modeNames[portfolio.mode] || portfolio.mode}</span><span>账本日期 {portfolio.day || '未推进'}</span><span>{statusNames[portfolio.status] || portfolio.status}</span></div></div>
          <div className="rw-risk-grid"><div><span>账户权益</span><strong>{money(portfolio.equity)}</strong></div><div><span>现金 / 可用现金</span><strong>{money(portfolio.cash)}</strong><small>可用 {money(portfolio.available_cash)}</small></div><div><span>总持仓 / 最大单只</span><strong>{percent(portfolio.gross_exposure)}</strong><small>最大单只 {percent(portfolio.largest_weight)}</small></div><div><span>前五大持仓</span><strong>{percent(portfolio.top5_weight)}</strong><small>{number(portfolio.position_count, 0)} 只实际持仓</small></div>
            <div><span>持仓集中度 HHI</span><strong>{number(portfolio.hhi, 4)}</strong><small>按持仓市值比例计算</small></div><div><span>等效持仓数量</span><strong>{number(portfolio.effective_positions)}</strong><small>集中度倒数，反映持仓分散程度</small></div><div><span>累计费用</span><strong>{money(portfolio.fees)}</strong><small>保存的账本成交费用</small></div><div><span>行情容量依据</span><strong style={{ fontSize: 12 }}>以逐只实际数据为准</strong><small>缺少匹配日成交量时显示为空</small></div></div>
          {portfolio.positions.length ? <div className="table-scroll"><table className="compact rw-table" aria-label="持仓集中度与成交量占比"><thead><tr><th>证券</th><th>持仓股数</th><th>可卖股数</th><th>市值（元）</th><th>权益权重</th><th>持仓 / 当日成交量</th></tr></thead><tbody>{portfolio.positions.map(position => <tr key={position.symbol}><td><strong>{position.symbol}</strong></td><td className="numeric">{number(position.quantity, 0)}</td><td className="numeric">{number(position.sellable, 0)}</td><td className="numeric">{number(position.value)}</td><td className="numeric">{percent(position.weight)}</td><td className="numeric">{percent(position.liquidity_participation)}</td></tr>)}</tbody></table></div> : <p className="rw-note">该账本当前没有持仓。</p>}
          {portfolio.warnings.map((text, index) => <p key={`${index}:${text}`} className="rw-warning">{text}</p>)}
        </> : <div className="rw-empty"><ShieldCheck size={18} />没有可读取的共享账本账户。创建历史回放或纸面账户后显示资金、持仓与费用。</div>}</div>
      </section>
      {workspace?.warnings.map((text, index) => <p key={`${index}:${text}`} className="rw-warning">{text}</p>)}
    </>}
  </div>;
}
