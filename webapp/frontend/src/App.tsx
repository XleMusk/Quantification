import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowLeftRight, BarChart3, ClipboardList, Database, FlaskConical, GitCompareArrows, Radio, Target } from 'lucide-react';
import { api } from './api';
import type { Dataset, Job } from './types';
import { DataCenter } from './pages/DataCenter';
import { Research } from './pages/Research';
import { Tasks } from './pages/Tasks';
import { Results } from './pages/Results';
import { ErrorMessage, Loading } from './components/Common';
import { Trading } from './pages/Trading';

const tabs = [{ id: 'overview', label: '运行总览', Icon: Activity }, { id: 'strategy', label: '策略与目标', Icon: Target }, { id: 'orders', label: '订单与成交', Icon: ArrowLeftRight }, { id: 'comparison', label: '研究比较', Icon: GitCompareArrows }, { id: 'data', label: '数据中心', Icon: Database }, { id: 'research', label: '因子研究', Icon: FlaskConical }, { id: 'tasks', label: '因子任务', Icon: ClipboardList }, { id: 'results', label: '因子结果', Icon: BarChart3 }];
export default function App() {
  const [page, setPage] = useState(() => location.hash.slice(1) || 'overview');
  const [datasets, setDatasets] = useState<Dataset[]>([]); const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState(''); const [active, setActive] = useState('');
  const [online, setOnline] = useState(false); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  const reloadData = useCallback(async () => { const value = await api<Dataset[]>('/datasets'); setDatasets(value); setSelected(previous => value.some(d => d.id === previous) ? previous : value.find(d => d.kind === 'historical')?.id || value[0]?.id || ''); }, []);
  const reloadJobs = useCallback(async () => {
    try {
      const [value, health] = await Promise.all([api<Job[]>('/jobs'), api<{ worker: { online: boolean } }>('/health')]);
      setJobs(value); setOnline(health.worker.online);
    } catch (error) { setOnline(false); throw error; }
  }, []);
  useEffect(() => { Promise.all([reloadData(), reloadJobs()]).catch(e => setError(e.message)).finally(() => setLoading(false)); }, [reloadData, reloadJobs]);
  useEffect(() => { let current = true; const interval = setInterval(() => { reloadJobs().then(() => { if (current) setError(''); }).catch(e => { if (current) setError(`后端连接失败：${e.message}`); }); }, jobs.some(j => ['running', 'queued'].includes(j.status)) ? 1200 : 5000); return () => { current = false; clearInterval(interval); }; }, [reloadJobs, jobs.some(j => ['running', 'queued'].includes(j.status))]);
  useEffect(() => { const changed = () => setPage(location.hash.slice(1) || 'overview'); window.addEventListener('hashchange', changed); return () => window.removeEventListener('hashchange', changed); }, []);
  useEffect(() => { window.scrollTo({ top: 0, left: 0 }); }, [page]);
  function navigate(next: string) { location.hash = next; setPage(next); }
  function openResult(id: string) { setActive(id); navigate('results'); }
  return <div className="app-shell"><aside className="sidebar"><a className="brand" href="#overview"><BarChart3 size={27} strokeWidth={3} /><span>本地量化</span></a><nav aria-label="主导航">{tabs.map(({ id, label, Icon }) => <button className={page === id ? 'active' : ''} onClick={() => navigate(id)} key={id}><Icon size={20} /><span>{label}</span>{id === 'tasks' && jobs.some(j => ['queued', 'running'].includes(j.status)) && <i className="nav-dot" />}</button>)}</nav><div className="local-status"><Radio size={17} /><span>本地计算进程</span><i className={online ? 'online' : ''} /><small>{online ? '在线' : '离线'}</small></div></aside>
    <main>{error && <ErrorMessage message={error} />}{['overview', 'strategy', 'orders', 'comparison'].includes(page) ? <Trading page={page} /> : loading ? <Loading /> : page === 'research' ? <Research datasets={datasets} selected={selected} select={setSelected} workerOnline={online} onCreated={job => { setActive(job.id); reloadJobs(); navigate('tasks'); }} /> : page === 'tasks' ? <Tasks jobs={jobs} datasets={datasets} active={active} select={setActive} reload={reloadJobs} openResult={openResult} /> : page === 'results' ? <Results jobs={jobs} active={active} select={setActive} /> : <DataCenter datasets={datasets} selected={selected} select={setSelected} reload={reloadData} />}</main>
  </div>;
}
