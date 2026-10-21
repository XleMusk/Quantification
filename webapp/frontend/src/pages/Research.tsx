import { useEffect, useRef, useState } from 'react';
import { Play, FlaskConical } from 'lucide-react';
import { post } from '../api';
import { factorNames, type Dataset, type Job } from '../types';
import { ErrorMessage, Warning } from '../components/Common';

export function Research({ datasets, selected, select, onCreated, workerOnline }: { datasets: Dataset[]; selected: string; select: (id: string) => void; onCreated: (job: Job) => void; workerOnline: boolean }) {
  const dataset = datasets.find(d => d.id === selected);
  const [start, setStart] = useState(''); const [end, setEnd] = useState('');
  const [pairs, setPairs] = useState(5); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const request = useRef<{ signature: string; key: string } | null>(null);
  useEffect(() => { if (dataset) { setStart(dataset.start); setEnd(dataset.end); } }, [dataset]);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (busy || !dataset) return; setBusy(true); setError('');
    const body = { dataset_id: dataset.id, start, end, min_pairs: pairs };
    const signature = JSON.stringify(body);
    if (request.current?.signature !== signature) request.current = { signature, key: crypto.randomUUID() };
    try { const job = await post<Job>('/jobs', body, request.current.key); request.current = null; onCreated(job); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <>
    <header className="page-header"><h1>因子研究</h1><span className="subtle">日频 · 横截面</span></header>
    {!dataset ? <div className="empty"><FlaskConical size={24} />暂无可用数据集</div> : <form onSubmit={submit} className="research-form">
      <section><h2>研究配置</h2><div className="form-grid"><label>数据集<select aria-label="研究数据集" value={selected} onChange={e => select(e.target.value)}>{datasets.map(d => <option key={d.id} value={d.id}>{d.name} · {d.symbols.length} 个证券</option>)}</select></label>
        <label>最小截面样本数<input type="number" min={5} max={100} value={pairs} onChange={e => setPairs(Number(e.target.value))} required /></label>
        <label>开始日期<input type="date" value={start} min={dataset.start} max={end} onChange={e => setStart(e.target.value)} required /></label>
        <label>结束日期<input type="date" value={end} min={start} max={dataset.end} onChange={e => setEnd(e.target.value)} required /></label></div></section>
      <section><h2>因子集</h2><div className="table-scroll"><table><thead><tr><th>因子</th><th>定义</th><th>信息时点</th></tr></thead><tbody>{Object.entries(factorNames).map(([key, title]) => <tr key={key}><td>{title}</td><td className="formula">{key === 'reversal_5' ? '−(close[t] / close[t−5] − 1)' : key === 'momentum_20' ? 'close[t] / close[t−20] − 1' : 'volume[t] / mean(volume[t−20:t−1])'}</td><td>t 日收盘后</td></tr>)}</tbody></table></div></section>
      <section className="label-definition"><h2>评价口径</h2><dl><dt>收益标签</dt><dd>t+1 开盘至 t+6 开盘</dd><dt>分组</dt><dd>5 组，按信号时点划分</dd><dt>缺失价格</dt><dd>不填充，不压缩交易日历</dd><dt>结束边界</dt><dd>剔除跨越研究结束日的收益标签</dd></dl></section>
      {dataset.warnings.map(w => <Warning key={w}>{w}</Warning>)}
      {!workerOnline && <Warning>计算进程离线。提交后任务将保留在队列中。</Warning>}
      {error && <ErrorMessage message={error} />}
      <div className="form-submit"><button className="primary" type="submit" disabled={busy || dataset.quality.invalid_rows > 0}><Play size={16} />{busy ? '提交中' : '提交因子任务'}</button></div>
    </form>}
  </>;
}
