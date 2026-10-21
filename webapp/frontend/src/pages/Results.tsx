import { useEffect, useState } from 'react';
import { Download, FileText } from 'lucide-react';
import { api } from '../api';
import { factorNames, fmt, timeText, type Job, type Result, type Summary } from '../types';
import { Chart } from '../components/Chart';
import { ErrorMessage, Loading, Warning } from '../components/Common';

export function Results({ jobs, active, select }: { jobs: Job[]; active: string; select: (id: string) => void }) {
  const complete = jobs.filter(j => j.status === 'completed');
  const id = complete.find(j => j.id === active)?.id || complete[0]?.id;
  const [result, setResult] = useState<Result | null>(null); const [error, setError] = useState(''); const [factor, setFactor] = useState('reversal_5');
  useEffect(() => { if (!id) return; let current = true; setResult(null); setError(''); api<Result>(`/jobs/${id}/result`).then(value => { if (current) setResult(value); }).catch(e => { if (current) setError(e.message); }); return () => { current = false; }; }, [id]);
  const selected = result?.summary.find(s => s.factor === factor);
  const daily = result?.daily_ic.filter(d => d.factor === factor) || [];
  return <>
    <header className="page-header"><h1>研究结果</h1>{id && <a className="button" href={`/api/jobs/${id}/files/REPORT.md`} download><FileText size={16} />研究报告</a>}</header>
    {error && <ErrorMessage message={error} />}
    {!id ? <div className="empty">暂无已完成的研究</div> : !result ? <Loading /> : <>
      <div className="result-toolbar"><label className="inline-field">任务<select aria-label="结果任务" value={id} onChange={e => select(e.target.value)}>{complete.map(j => <option key={j.id} value={j.id}>{j.id.slice(0, 8)} · {timeText(j.created_at)}</option>)}</select></label><span className={`source-tag ${result.dataset.kind}`}>{result.dataset.kind === 'historical' ? '历史数据' : '合成数据'}</span><strong>{result.dataset.name}</strong></div>
      <div className="result-range">{result.config.start} 至 {result.config.end}<span>前瞻收益 5 个交易日</span><span>最小截面样本 {result.config.min_pairs}</span></div>
      <div className="table-scroll"><table><thead><tr><th>因子</th><th>有效 IC 天数</th><th>平均 Rank IC</th><th>IC 标准差</th><th>Q1 平均收益</th><th>Q5 平均收益</th></tr></thead><tbody>{result.summary.map(s => <tr key={s.factor} className={s.factor === factor ? 'selected' : ''}><td><button className="text-button" onClick={() => setFactor(s.factor)}>{factorNames[s.factor]}</button></td><td>{s.valid_ic_days}</td><td className="numeric">{fmt(s.mean_rank_ic, 4)}</td><td className="numeric">{fmt(s.std_rank_ic, 4)}</td><td>{s.q1_mean_label == null ? '—' : `${fmt(s.q1_mean_label * 100, 3)}%`}</td><td>{s.q5_mean_label == null ? '—' : `${fmt(s.q5_mean_label * 100, 3)}%`}</td></tr>)}</tbody></table></div>
      <div className="result-charts"><section><h2>{factorNames[factor]} · 日度 Rank IC</h2><Chart x={daily.map(d => d.date.slice(0, 10))} values={daily.map(d => d.rank_ic)} label="日度 Rank IC" /></section><section><h2>分组平均远期收益</h2><Chart x={['Q1', 'Q2', 'Q3', 'Q4', 'Q5']} values={[1, 2, 3, 4, 5].map(q => selected?.[`q${q}_mean_label` as keyof Summary] as number | null ?? null)} label="五组平均收益" kind="bar" percentage color="#14865d" /></section></div>
      <section className="correlation"><h2>因子截面秩相关性</h2><div className="table-scroll"><table><thead><tr><th>因子</th>{Object.values(factorNames).map(name => <th key={name}>{name}</th>)}</tr></thead><tbody>{result.correlation.map(row => <tr key={String(row.factor)}><td>{factorNames[String(row.factor)]}</td>{Object.keys(factorNames).map(name => <td className="numeric" key={name}>{fmt(row[name] as number | null, 3)}</td>)}</tr>)}</tbody></table></div></section>
      <div className="downloads">{[['summary.csv', '指标汇总'], ['daily_ic.csv', '日度 IC'], ['groups.csv', '分组数据'], ['factors.parquet', '因子明细'], ['result.json', '完整结果']].map(([file, name]) => <a className="button" href={`/api/jobs/${id}/files/${file}`} download key={file}><Download size={15} />{name}</a>)}</div>
      <div className="warnings">{result.warnings.map(w => <Warning key={w}>{w}</Warning>)}</div>
    </>}
  </>;
}
