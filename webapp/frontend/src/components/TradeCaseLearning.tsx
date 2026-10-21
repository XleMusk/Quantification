import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';

type Saved = Record<string, unknown>;
type CaseRow = { case_id: string; symbol: string | null; day: string | null; kind: string;
  name: string | null; scope: string; interpretation_status: string; price_path_status: string };
type Summary = { archive_records: number; seat_source_interpretations: number;
  records_by_kind: Record<string, number>; incremental_price_status_counts: Record<string, number> };
type CasePage = { summary: Summary; cases: CaseRow[]; total_filtered: number; offset: number; limit: number };
type CaseDetail = { case: CaseRow; interpretation: Saved | null; source_facts: Saved;
  source_metadata: Saved; boundaries: string[] };
const kindNames: Record<string, string> = { seat_disclosure: '既有席位案', seat_source_snapshot: '新增来源案',
  leader_price_path: '龙头价格案例', public_source: '公开原文资料' };
const priceNames: Record<string, string> = { embedded_pair_present_not_full_trade_path: '有两日原价，非完整成交路径',
  embedded_price_slots_missing: '前日价格缺项', independent_price_path_pending: '独立日线待补',
  embedded_price_slots_include_nonactive_or_unknown: '含停牌或未知状态',
  not_aggregated_from_legacy_manifest: '保留原案状态，未重新汇总' };
const scopeNames: Record<string, string> = { supported_sh_sz_code_proxy: '沪深代码范围，资格另核',
  bse_research_only: '北交所独立研究范围', outside_ordinary_equity_scope: '非普通股票范围',
  legacy_saved_scope: '原案保存范围' };
const text = (value: unknown) => value == null ? '未保存' : typeof value === 'string' ? value : JSON.stringify(value);
function Paragraphs({ value }: { value: unknown }) {
  if (value == null) return null;
  const values = Array.isArray(value) ? value : typeof value === 'object' ? Object.values(value) : [value];
  return <>{values.map((item, i) => <p key={i}>{text(item)}</p>)}</>;
}

export function TradeCaseLearning() {
  const [symbol, setSymbol] = useState(''), [day, setDay] = useState('');
  const [kind, setKind] = useState('seat_source_snapshot'), [scope, setScope] = useState('');
  const [filters, setFilters] = useState({ symbol: '', day: '', kind: 'seat_source_snapshot', scope: '' });
  const [offset, setOffset] = useState(0), [version, setVersion] = useState(0);
  const [page, setPage] = useState<CasePage | null>(null), [error, setError] = useState('');
  const [selected, setSelected] = useState(''), [detail, setDetail] = useState<CaseDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const reading = useRef<HTMLElement>(null);
  useEffect(() => {
    const controller = new AbortController(); setPage(null); setError(''); setSelected('');
    const query = new URLSearchParams({ ...filters, offset: String(offset), limit: '30' });
    api<CasePage>(`/trading/research/trade-case-learning?${query}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setPage(result); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [filters, offset, version]);
  useEffect(() => {
    setDetail(null); setDetailError('');
    if (!selected) return;
    const controller = new AbortController();
    api<CaseDetail>(`/trading/research/trade-case-learning/${encodeURIComponent(selected)}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setDetail(result); })
      .catch(cause => { if (!controller.signal.aborted) setDetailError(cause.message); });
    return () => controller.abort();
  }, [selected]);
  useEffect(() => { if (detail) reading.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, [detail]);
  const summary = page?.summary, interpretation = detail?.interpretation;
  return <section className="trade-section" id="trade-case-learning" aria-label="逐案学习档案">
    <div className="dialog-heading"><h2>逐案学习 · 原事实与解读</h2>
      <button className="icon-button" aria-label="只读刷新逐案档案" title="只读刷新" onClick={() => setVersion(v => v + 1)}><RefreshCw size={16} /></button></div>
    <p className="subtle">查股票、看原件、对照失败。这里保存来源解读，不是游资私人交割单，也不产生当前买卖信号。</p>
    {summary && <>
      <p aria-label="逐案档案统计">全部档案 {summary.archive_records} 条；席位来源AI解读 {summary.seat_source_interpretations} 份。
        龙头价格案例 {summary.records_by_kind.leader_price_path || 0}、公开原文 {summary.records_by_kind.public_source || 0} 单列，不能相加为交易笔数。</p>
      <p className="subtle">新增来源案 {summary.records_by_kind.seat_source_snapshot || 0} 份：
        {summary.incremental_price_status_counts.embedded_pair_present_not_full_trade_path || 0} 份有两日原价，
        {summary.incremental_price_status_counts.embedded_price_slots_missing || 0} 份前日缺价，
        {summary.incremental_price_status_counts.independent_price_path_pending || 0} 份缺独立日线。私人完整交割单与人工深审均未取得。</p>
    </>}
    <form className="leader-paper-controls" onSubmit={event => { event.preventDefault(); setOffset(0); setFilters({ symbol: symbol.trim(), day, kind, scope }); }}>
      <label>证券代码<input aria-label="逐案证券代码" value={symbol} onChange={event => setSymbol(event.target.value)} placeholder="例 002343 或 002343.SZSE" /></label>
      <label>事件日期<input aria-label="逐案事件日期" type="date" value={day} onChange={event => setDay(event.target.value)} /></label>
      <label>资料类型<select aria-label="逐案资料类型" value={kind} onChange={event => setKind(event.target.value)}><option value="">全部档案</option>{Object.entries(kindNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      <label>保存范围<select aria-label="逐案保存范围" value={scope} onChange={event => setScope(event.target.value)}><option value="">全部范围</option>{Object.entries(scopeNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <button type="submit">筛选已存资料</button>
    </form>
    {error && <p role="status" className="window-stale">逐案资料读取失败：{error}。不以旧结果或空列表代替。</p>}
    {!page && !error && <p role="status">正在读取已保存索引。</p>}
    {page && <>
      <p aria-live="polite">符合条件 {page.total_filtered} 条{page.total_filtered ? `，显示 ${offset + 1}—${offset + page.cases.length}` : '；不表示该股票没有交易'}。</p>
      <div className="table-scroll"><table className="compact" aria-label="逐案学习列表"><thead><tr><th>证券 / 事件日</th><th>资料范围</th><th>已存价格</th><th>原事实与解读</th></tr></thead><tbody>{page.cases.map(row => <tr key={row.case_id}>
        <td>{row.symbol || row.case_id}<small style={{ display: 'block' }}>{row.name || ''} · {row.day || '原记录未存精确日期'}</small></td>
        <td>{kindNames[row.kind] || row.kind}<small style={{ display: 'block' }}>{scopeNames[row.scope] || row.scope}</small></td>
        <td style={{ whiteSpace: 'normal' }}>{priceNames[row.price_path_status] || row.price_path_status}</td>
        <td><button aria-label={`阅读案例 ${row.case_id}`} aria-expanded={selected === row.case_id} onClick={() => setSelected(value => value === row.case_id ? '' : row.case_id)}>阅读本案</button></td>
      </tr>)}</tbody></table></div>
      <div className="actions" style={{ marginTop: 12 }}><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button><button disabled={offset + page.cases.length >= page.total_filtered} onClick={() => setOffset(offset + 30)}>下一页</button></div>
    </>}
    {selected && <article ref={reading} aria-label="当前逐案详情" style={{ marginTop: 18, overflowWrap: 'anywhere' }}>
      {!detail && !detailError && <p role="status">正在读取本案已存事实与解读。</p>}
      {detailError && <p role="status" className="window-stale">本案资料读取失败：{detailError}。</p>}
      {detail && <>
        <h3>{detail.case.name || detail.case.symbol || detail.case.case_id} · {detail.case.day || '日期见原件'}</h3>
        <button onClick={() => { setSelected(''); document.getElementById('trade-case-learning')?.scrollIntoView({ block: 'start' }); }}>收起本案</button>
        <Paragraphs value={interpretation?.summary ?? interpretation?.title} />
        <Paragraphs value={interpretation?.observed_facts ?? interpretation?.facts ?? interpretation?.learning_notes} />
        <Paragraphs value={interpretation?.interpretation} />
        {interpretation?.question_to_test != null && <><strong>待检验问题</strong><Paragraphs value={interpretation.question_to_test} /></>}
        {(interpretation?.invalidation_and_untradeable_conditions != null || interpretation?.next_step_and_failure_boundary != null) && <><strong>失效观察与成交缺项</strong><Paragraphs value={interpretation.invalidation_and_untradeable_conditions ?? interpretation.next_step_and_failure_boundary} /></>}
        <Paragraphs value={interpretation?.lesson_to_keep} />
        {interpretation == null && <p>当前档案没有另存解读字段；下方保留原事实。</p>}
        <details className="offline-details"><summary>原摘要、席位、价格与空值</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 440, overflow: 'auto' }}>{JSON.stringify(detail.source_facts, null, 2)}</pre></details>
        <details className="offline-details"><summary>原件路径、取得时间与解读时间</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto' }}>{JSON.stringify(detail.source_metadata, null, 2)}</pre></details>
        <p className="subtle">{detail.boundaries.join(' ')}</p>
      </>}
    </article>}
  </section>;
}
