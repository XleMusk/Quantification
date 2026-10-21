import { useState } from 'react';
import { Check, RefreshCw, X, ArrowUpRight } from 'lucide-react';
import { post } from '../api';
import { type Dataset, type Job, timeText } from '../types';
import { Status, ErrorMessage } from '../components/Common';

export function Tasks({ jobs, datasets, active, select, reload, openResult }: { jobs: Job[]; datasets: Dataset[]; active: string; select: (id: string) => void; reload: () => Promise<void>; openResult: (id: string) => void }) {
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const item = jobs.find(j => j.id === active) || jobs[0];
  async function cancel(id: string) { setBusy(true); setError(''); try { await post(`/jobs/${id}/cancel`); await reload(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  async function retry(job: Job) { setBusy(true); setError(''); try { const next = await post<Job>('/jobs', { dataset_id: job.dataset_id, start: job.config.start, end: job.config.end, min_pairs: job.config.min_pairs }, crypto.randomUUID()); await reload(); select(next.id); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  return <>
    <header className="page-header"><h1>任务管理</h1><button onClick={reload}><RefreshCw size={16} />刷新</button></header>
    {error && <ErrorMessage message={error} />}
    <div className="table-scroll"><table><thead><tr><th>任务编号</th><th>数据集</th><th>状态</th><th>进度</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{jobs.map(j => <tr key={j.id} className={item?.id === j.id ? 'selected' : ''}><td><button className="text-button mono" onClick={() => select(j.id)}>{j.id.slice(0, 8)}</button></td><td>{datasets.find(d => d.id === j.dataset_id)?.name || j.dataset_id.slice(0, 8)}</td><td><Status value={j.status} /></td><td><div className="progress"><progress max={100} value={j.progress} /><span>{j.progress}%</span></div></td><td className="numeric">{timeText(j.created_at)}</td><td>{j.status === 'completed' ? <button className="icon-button" title="查看结果" aria-label={`查看结果 ${j.id.slice(0, 8)}`} onClick={() => openResult(j.id)}><ArrowUpRight size={17} /></button> : ['queued', 'running'].includes(j.status) ? <button className="icon-button" aria-label="取消任务" title="取消任务" disabled={busy || !!j.cancel_requested} onClick={() => cancel(j.id)}><X size={17} /></button> : <button className="icon-button" aria-label="重新提交" title="重新提交" disabled={busy} onClick={() => retry(j)}><RefreshCw size={17} /></button>}</td></tr>)}</tbody></table></div>
    {!jobs.length && <div className="empty">暂无任务</div>}
    {item && <section className="task-detail"><h2>运行记录 <span className="mono">{item.id.slice(0, 8)}</span></h2><div className="task-info"><Status value={item.status} /><span>{item.config.start} 至 {item.config.end}</span><span>最小样本 {item.config.min_pairs}</span>{item.cancel_requested === 1 && <span>已请求取消</span>}</div>
      {item.error && <ErrorMessage message={item.error} />}
      <ol className="logs">{item.logs.map((log, index) => <li key={index}><Check size={14} /><time>{new Date(log.time).toLocaleTimeString('zh-CN', { hour12: false })}</time><span>{log.message}</span></li>)}</ol>
      {!item.logs.length && <p className="subtle">等待计算进程领取任务</p>}
    </section>}
  </>;
}
