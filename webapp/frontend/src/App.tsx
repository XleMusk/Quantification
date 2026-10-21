import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { ChevronDown, RefreshCw } from 'lucide-react';
import { api } from './api';
import type { Dataset, Job } from './types';
import { DataCenter } from './pages/DataCenter';
import { Research } from './pages/Research';
import { Tasks } from './pages/Tasks';
import { Results } from './pages/Results';
import { ErrorMessage, Loading } from './components/Common';
import { TerminalOverview } from './pages/TerminalOverview';

const Trading = lazy(() => import('./pages/Trading').then(module => ({ default: module.Trading })));
const ChartReview = lazy(() => import('./pages/ChartReview').then(module => ({ default: module.ChartReview })));
const tabs = [
  { id: 'overview', label: '交易总览' }, { id: 'data', label: '数据中心' },
  { id: 'research', label: '因子研究' }, { id: 'comparison', label: '回测分析' },
  { id: 'strategy', label: '模拟账户' }, { id: 'orders', label: '执行管理' },
  { id: 'chart-review', label: '图表复盘' },
];
const knownPages = new Set([...tabs.map(tab => tab.id), 'tasks', 'results']);
const pageFromHash = () => { const hash = location.hash.slice(1); return knownPages.has(hash) ? hash : 'overview'; };

function TerminalClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const timer = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(timer); }, []);
  return <time dateTime={now.toISOString()}>{new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(now)}</time>;
}

export default function App() {
  const [page, setPage] = useState(pageFromHash);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState(() => {
    try { return localStorage.getItem('quant.workspace.dataset.v1') || ''; } catch { return ''; }
  });
  const [active, setActive] = useState('');
  const [online, setOnline] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const reloadData = useCallback(async () => {
    const value = await api<Dataset[]>('/datasets');
    setDatasets(value);
    setSelected(previous => value.some(dataset => dataset.id === previous)
      ? previous : value.find(dataset => dataset.kind === 'historical')?.id || value[0]?.id || '');
  }, []);
  const reloadJobs = useCallback(async () => {
    try {
      const [value, health] = await Promise.all([
        api<Job[]>('/jobs'), api<{ worker: { online: boolean } }>('/health'),
      ]);
      setJobs(value); setOnline(health.worker.online);
    } catch (error) { setOnline(false); throw error; }
  }, []);
  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([reloadData(), reloadJobs()]);
    const failed = results.find(result => result.status === 'rejected');
    setError(failed?.status === 'rejected' ? String(failed.reason?.message || failed.reason) : '');
  }, [reloadData, reloadJobs]);
  useEffect(() => { refresh().finally(() => setLoading(false)); }, [refresh]);
  useEffect(() => {
    try { if (selected) localStorage.setItem('quant.workspace.dataset.v1', selected); } catch { /* Selection remains usable. */ }
  }, [selected]);
  const hasPendingJobs = jobs.some(job => ['running', 'queued'].includes(job.status));
  useEffect(() => {
    let current = true;
    const interval = setInterval(() => {
      reloadJobs().then(() => { if (current) setError(''); })
        .catch(error => { if (current) setError(`后端连接失败：${error.message}`); });
    }, hasPendingJobs ? 1200 : 5000);
    return () => { current = false; clearInterval(interval); };
  }, [reloadJobs, hasPendingJobs]);
  useEffect(() => { const changed = () => setPage(pageFromHash()); window.addEventListener('hashchange', changed); return () => window.removeEventListener('hashchange', changed); }, []);
  useEffect(() => { window.scrollTo({ top: 0, left: 0 }); }, [page]);
  const navigate = useCallback((next: string) => { location.hash = next; setPage(next); }, []);
  const openResult = useCallback((id: string) => { setActive(id); navigate('results'); }, [navigate]);
  const researchPage = ['research', 'tasks', 'results'].includes(page);

  return <div className="app-shell terminal-shell">
    <header className="terminal-header">
      <a className="terminal-brand" href="#overview"><strong>QUANTIFICATION</strong><span>量化工作台</span></a>
      <span className="terminal-workspace">A股研究 <ChevronDown size={12} aria-hidden="true" /></span>
      <nav className="terminal-nav" aria-label="主导航">{tabs.map(tab => <button
        key={tab.id} className={(researchPage ? tab.id === 'research' : page === tab.id) ? 'active' : ''}
        aria-current={(researchPage ? tab.id === 'research' : page === tab.id) ? 'page' : undefined}
        onClick={() => navigate(tab.id)}>{tab.label}{tab.id === 'research' && hasPendingJobs && <i className="nav-dot" />}</button>)}</nav>
      <div className="terminal-header-status"><span className="terminal-mode"><i />研究 / 模拟</span><TerminalClock /><button className="icon-button" title="刷新本地数据与任务" aria-label="刷新本地数据与任务" onClick={() => void refresh()}><RefreshCw size={14} /></button></div>
    </header>
    {researchPage && <div className="terminal-subnav" aria-label="因子研究导航">{[['research', '研究配置'], ['tasks', '后台任务'], ['results', '研究结果']].map(([id, label]) => <button key={id} className={page === id ? 'active' : ''} onClick={() => navigate(id)}>{label}{id === 'tasks' && hasPendingJobs && <span>{jobs.filter(job => ['running', 'queued'].includes(job.status)).length}</span>}</button>)}</div>}
    <main className={page === 'overview' ? 'terminal-main terminal-main-overview' : 'terminal-main'}>
      {error && <ErrorMessage message={error} />}
      <Suspense fallback={<Loading />}>
        {page === 'overview' ? <TerminalOverview datasets={datasets} selectedDatasetId={selected} onDatasetSelect={setSelected} jobs={jobs} onNavigate={navigate} onOpenResult={openResult} />
          : page === 'chart-review' ? <ChartReview datasets={datasets} datasetId={selected} onDatasetSelect={setSelected} jobs={jobs} />
          : ['strategy', 'orders', 'comparison'].includes(page) ? <Trading page={page} />
          : loading ? <Loading />
          : page === 'research' ? <Research datasets={datasets} selected={selected} select={setSelected} workerOnline={online} onCreated={job => { setActive(job.id); void reloadJobs(); navigate('tasks'); }} />
          : page === 'tasks' ? <Tasks jobs={jobs} datasets={datasets} active={active} select={setActive} reload={reloadJobs} openResult={openResult} />
          : page === 'results' ? <Results jobs={jobs} active={active} select={setActive} />
          : <DataCenter datasets={datasets} selected={selected} select={setSelected} reload={reloadData} />}
      </Suspense>
    </main>
    <footer className="terminal-statusbar"><span>本地研究环境 · 非实时行情</span><span>价格口径与日期以所选快照为准</span><span className={online ? 'service-online' : ''}><i />因子进程 {online ? '在线' : '离线'}</span><span>真实提交 关闭</span></footer>
  </div>;
}
