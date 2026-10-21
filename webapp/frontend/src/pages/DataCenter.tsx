import { useEffect, useState } from 'react';
import { CheckCircle2, Database, RefreshCw, Upload } from 'lucide-react';
import { api, post } from '../api';
import { adjustmentNames, dateText, fmt, type Dataset, type Preview } from '../types';
import { Chart } from '../components/Chart';
import { ErrorMessage, Loading, Warning } from '../components/Common';
import { ImportDialog } from '../components/ImportDialog';

export function DataCenter({ datasets, selected, select, reload }: { datasets: Dataset[]; selected: string; select: (id: string) => void; reload: () => Promise<void> }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [symbol, setSymbol] = useState('');
  const [tab, setTab] = useState('preview');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const dataset = datasets.find(d => d.id === selected);
  useEffect(() => { setSymbol(''); }, [selected]);
  useEffect(() => {
    if (!selected || (symbol && !dataset?.symbols.includes(symbol))) return;
    let current = true; setPreview(null); setError('');
    api<Preview>(`/datasets/${selected}/preview${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`)
      .then(value => { if (current) setPreview(value); }).catch(error => { if (current) setError(error.message); });
    return () => { current = false; };
  }, [selected, symbol, dataset]);
  async function sync() { setBusy(true); setError(''); try { const value = await post<Dataset>('/datasets/sync-cache'); await reload(); select(value.id); } catch (error) { setError((error as Error).message); } finally { setBusy(false); } }
  return <>
    <header className="page-header"><h1>数据中心</h1><div className="actions"><button onClick={sync} disabled={busy}><RefreshCw size={16} className={busy ? 'spin' : ''} />同步本地缓存</button><button className="primary" onClick={() => setImporting(true)}><Upload size={16} />导入数据</button></div></header>
    {error && <ErrorMessage message={error} />}
    <div className="metrics"><div><span>数据集</span><strong>{datasets.length}</strong></div><div><span>所选证券</span><strong>{dataset?.symbols.length ?? '—'}</strong></div><div><span>价格口径</span><strong className="metric-text">{dataset ? adjustmentNames[dataset.adjustment] : '—'}</strong></div><div><span>数据截止</span><strong className="metric-text">{dataset?.end ?? '—'}</strong></div></div>
    <div className="table-scroll"><table className="dataset-table"><thead><tr><th>数据集名称</th><th>行数</th><th>证券数量</th><th>数据类型</th><th>数据来源</th><th>截止日期</th></tr></thead><tbody>
      {datasets.map(d => <tr key={d.id} className={d.id === selected ? 'selected' : ''}><td><button className="row-select" onClick={() => select(d.id)} aria-pressed={d.id === selected}><span className="radio-dot" />{d.name}</button></td><td className="numeric">{fmt(d.rows, 0)}</td><td>{d.symbols.length}</td><td><span className={`source-tag ${d.kind}`}>{d.kind === 'historical' ? '历史数据' : '合成数据'}</span></td><td>{d.source}</td><td className="numeric">{d.end}</td></tr>)}
    </tbody></table></div>
    {!datasets.length && <div className="empty"><Database size={26} />暂无数据集</div>}
    {dataset && <section className="dataset-detail"><h2>数据集详情 <span>{dataset.name}</span></h2>
      <div className="tabs"><button onClick={() => setTab('preview')} className={tab === 'preview' ? 'active' : ''}>数据预览</button><button onClick={() => setTab('quality')} className={tab === 'quality' ? 'active' : ''}>质量检查</button></div>
      {tab === 'preview' ? <>
        <label className="inline-field">证券代码<select aria-label="证券代码" value={symbol || preview?.symbol || dataset.symbols[0]} onChange={e => setSymbol(e.target.value)}>{dataset.symbols.map(s => <option key={s}>{s}</option>)}</select></label>
        {!preview ? <Loading /> : <div className="data-split"><div className="chart-section"><h3>收盘价 <small>{adjustmentNames[dataset.adjustment]}</small></h3><Chart x={preview.prices.map(p => dateText(p.date))} values={preview.prices.map(p => p.close)} label="收盘价" /></div>
          <div className="table-scroll"><table className="compact"><thead><tr><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th><th>成交量（股）</th></tr></thead><tbody>{preview.recent.map(p => <tr key={p.date}><td>{dateText(p.date)}</td><td>{fmt(p.open)}</td><td>{fmt(p.high)}</td><td>{fmt(p.low)}</td><td>{fmt(p.close)}</td><td>{fmt(p.volume, 0)}</td></tr>)}</tbody></table></div></div>}
      </> : <div className="quality"><div className="quality-summary"><CheckCircle2 size={20} /><strong>{dataset.quality.invalid_rows ? '存在数据异常' : '基础字段校验通过'}</strong></div><dl><dt>OHLC / 成交量异常</dt><dd>{dataset.quality.invalid_rows}</dd><dt>零成交量记录</dt><dd>{dataset.quality.zero_volume_rows}</dd><dt>日期 × 证券缺失格点</dt><dd>{dataset.quality.missing_grid_rows}</dd><dt>交易日数量</dt><dd>{dataset.quality.sessions}</dd><dt>日历来源</dt><dd>{dataset.quality.calendar_kind === 'provided' ? '随数据提供，未独立验证' : '观测日期并集'}</dd><dt>覆盖区间</dt><dd>{dataset.start} 至 {dataset.end}</dd></dl></div>}
      <div className="warnings">{dataset.warnings.map(text => <Warning key={text}>{text}</Warning>)}</div>
    </section>}
    {importing && <ImportDialog onClose={() => setImporting(false)} onImported={async d => { setImporting(false); await reload(); select(d.id); }} />}
  </>;
}
