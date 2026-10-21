import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';
import { ErrorMessage, Warning } from './Common';

type Episode = { symbol: string; start: string; end: string; held_stale_sessions: number; end_quantity: number; last_tradable_date: string | null; next_tradable_date: string | null; max_marked_exposure_fraction: number | null; raw_reopen_open_ratio: number | null; raw_reopen_close_ratio: number | null; company_actions_in_interval: number; };
type Gaps = { status: 'prepared' | 'not_prepared'; case: string; diagnostic_id?: string; stale_dates?: number; unknown_mark_rows?: number; max_known_stale_exposure_fraction?: number | null; source_reasons?: Record<string, number>; episodes: Episode[]; };
const pct = (value?: number | null) => value == null ? '--' : `${fmt(value * 100, 2)}%`;
const reasonNames: Record<string, string> = { provider_suspended: '提供方标记停牌', missing_raw_row: '缺失原始报价', unpriceable_raw_close: '原始收盘价不可用', saved_flag_disagrees_with_tradable_quote: '账本与报价状态冲突', unknown_raw_trade_status: '交易状态未知' };

export function ValuationGaps({ runId, signal, revision }: { runId: string; signal: string; revision: number }) {
  const [gaps, setGaps] = useState<Gaps | null>(null);
  const [error, setError] = useState('');
  const caseId = `${runId}:${signal}`;
  useEffect(() => {
    let live = true;
    setGaps(null); setError('');
    api<Gaps>(`/trading/research-replays/${runId}/${signal}/valuation-gaps`).then(value => {
      if (live) setGaps(value);
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [runId, signal, revision]);
  const shown = gaps?.case === caseId ? gaps : null;
  return <div aria-label="停牌与估值缺口">
    <h3>停牌与估值缺口</h3>
    {error ? <ErrorMessage message={error} /> : !shown ? <p className="subtle">读取中</p> : shown.status === 'not_prepared' ? <p className="subtle">尚无匹配的估值缺口诊断</p> : <>
      <div className="trade-source"><span>陈旧估值 {shown.stale_dates} 日</span><span>最大已知账面暴露 {pct(shown.max_known_stale_exposure_fraction)}</span><span>未知估值 {shown.unknown_mark_rows} 条</span></div>
      {Object.keys(shown.source_reasons || {}).length > 0 && <p className="subtle">{Object.entries(shown.source_reasons || {}).map(([reason, count]) => `${reasonNames[reason] || reason} ${count} 个证券日`).join(' · ')}</p>}
      {shown.episodes.length ? <>
        <Warning>停牌报价不是可成交价格。复牌涨跌为事后价格观察，不是持仓收益；历史回撤不能保证清仓损失。</Warning>
        <div className="table-scroll"><table><thead><tr><th>证券</th><th>冻结持仓期间</th><th>交易日</th><th>期末股数</th><th>最大账面暴露</th><th>最后交易日</th><th>复牌日</th><th>复牌开盘 / 收盘涨跌</th><th>期间公司行动</th></tr></thead><tbody>{shown.episodes.map(row => <tr key={`${row.symbol}-${row.start}`}><td className="mono">{row.symbol}</td><td>{row.start} 至 {row.end}</td><td>{row.held_stale_sessions}</td><td>{fmt(row.end_quantity, 0)}</td><td>{pct(row.max_marked_exposure_fraction)}</td><td>{row.last_tradable_date || '--'}</td><td>{row.next_tradable_date || '--'}</td><td>{pct(row.raw_reopen_open_ratio)} / {pct(row.raw_reopen_close_ratio)}</td><td>{row.company_actions_in_interval}</td></tr>)}</tbody></table></div>
      </> : <p className="subtle">已保存期间未观察到陈旧估值持仓</p>}
    </>}
  </div>;
}
