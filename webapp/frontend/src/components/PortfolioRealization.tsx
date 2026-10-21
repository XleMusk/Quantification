import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';

type CaseIndex = {
  case_id: string; label: string; candidate_id: string; candidate_label: string; status: string;
  expected_sessions: number | null; committed_sessions: number | null; last_saved_day: string | null;
  source_available: boolean; error?: string | null;
};
type RealizationSummary = {
  configured_gross_limit: number | null; committed_sessions: number | null;
  trace_sessions: number | null; matched_sessions: number | null;
  mean_adopted_target_gross: number | null; mean_lot_target_gross: number | null;
  mean_realized_reference_gross: number | null; mean_close_gross_on_matched_sessions: number | null;
  mean_close_gross_all_saved_sessions: number | null; last_saved_day: string | null;
  stale_valuation_sessions: number | null; close_gross_known_sessions?: number | null;
};
type RealizationSession = {
  date: string; diagnostic_rows: number | null; target_adopted_gross: number | null;
  lot_target_gross: number | null; realized_reference_gross: number | null; close_gross: number | null;
  matched: boolean; stale_valuation: boolean; warnings: string[] | null;
  mechanical_reasons: Record<string, number> | null; constraint_reasons: Record<string, number> | null;
  target_to_lot_gap: number | null; lot_to_realized_gap: number | null; target_to_realized_gap: number | null;
};
type RealizationDetail = {
  case_id: string; label: string; source_status: string; expected_sessions: number | null;
  full_window_saved: boolean; summary: RealizationSummary; sessions: RealizationSession[];
  reason_counts: { reason: string; category: string; count: number | null; day_count: number | null; days: string[] | null }[];
  notes: string[]; source_files: unknown; source_stop: unknown;
};
type RealizationResponse = {
  schema: string; cases: CaseIndex[]; selected_case_id: string | null; detail: RealizationDetail | null;
  read_at: string; readonly: boolean; producer_called: boolean; engine_advanced: boolean;
  execution_admitted: boolean; detail_status?: string; error?: string | null;
};

const number = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? fmt(value, 0) : '—';
const pct = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? `${fmt(value * 100, 2)}%` : '—';
const points = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? `${fmt(value * 100, 2)} 个百分点` : '—';
const original = (value: unknown) => value == null ? '未提供' : typeof value === 'string' ? value : JSON.stringify(value);
const reasonNames: Record<string, string> = {
  below_minimum_no_entry: '整手取整后未形成新买入', target_below_minimum_exit: '持仓目标取整为零',
  new_entry: '新建持仓目标', unchanged_quantity: '目标数量未变化', internal_reweight: '内部调仓',
  target_removed_exit: '目标移除退出', forced_risk_exit: '风险规则退出', residual_full_exit: '继续退出残仓',
  independent_risk_only_hold_quantity: '独立退出规则下保留原数量',
  independent_replay_risk_exit: '独立研究退出', exposure_cap_reduction: '持仓上限减仓',
  legal_order_quantity: '申报数量取整', cash_fee_reservation: '现金及费用预留约束',
  insufficient_cash: '可用现金不足', missing_quote: '缺少执行报价', sellable_T1: 'T+1可卖数量约束',
  exposure_caps: '持仓上限约束', prior_volume_participation: '前日成交量参与限制',
  minimum_commission_internal_reweight: '小额内部调仓因最低佣金暂缓',
};
const reasonLabel = (reason: string) => reasonNames[reason] ? `${reasonNames[reason]}（${reason}）` : reason;
const reasonList = (values: Record<string, number> | null | undefined) => values == null ? '原记录未提供'
  : Object.keys(values).length === 0 ? '无原记录' : Object.entries(values).map(([reason, count]) => `${reasonLabel(reason)} × ${number(count)}`).join('；');

function Detail({ value }: { value: RealizationDetail }) {
  const [allSessions, setAllSessions] = useState(false);
  const summary = value.summary;
  const sessions = allSessions ? value.sessions : value.sessions.slice(-20);
  return <div data-testid="portfolio-realization-detail" data-realization-case={value.case_id}>
    <p className={value.full_window_saved ? 'subtle' : 'window-stale'} data-testid="portfolio-realization-coverage">
      {value.full_window_saved ? '原研究窗口记录已保存完整' : '仅有部分研究记录，不能当作完整窗口'}：已保存 {number(summary.committed_sessions)} / {number(value.expected_sessions)} 个交易日，最后保存日 {summary.last_saved_day ?? '—'}。
    </p>
    <div className="metrics trade-metrics">
      <div><span>配置股票仓位上限</span><strong>{pct(summary.configured_gross_limit)}</strong></div>
      <div><span>同日完整对照</span><strong>{number(summary.matched_sessions)} 日</strong></div>
      <div><span>有数量计算记录</span><strong>{number(summary.trace_sessions)} 日</strong></div>
      <div><span>旧价估值</span><strong>{number(summary.stale_valuation_sessions)} 日</strong></div>
    </div>
    <p className="subtle opening-daily-case-note">配置上限不等于每天实际目标。以下四阶段均值只比较同一批记录完整的交易日；前三项共用当时开盘权益与执行参考价，收盘仓位采用收盘价格及收盘权益。价差和分母变化不等于成交受阻的因果影响。</p>
    <div className="table-scroll"><table className="compact" aria-label="同日完整交集的仓位实现均值"><thead><tr>
      <th>执行采用的目标权重</th><th>整手后的目标仓位</th><th>成交后参考价仓位</th><th>同日收盘仓位</th>
    </tr></thead><tbody><tr><td>{pct(summary.mean_adopted_target_gross)}</td><td>{pct(summary.mean_lot_target_gross)}</td>
      <td>{pct(summary.mean_realized_reference_gross)}</td><td>{pct(summary.mean_close_gross_on_matched_sessions)}</td></tr></tbody></table></div>
    <p className="subtle" data-testid="portfolio-realization-prefix">全部已保存期间的平均收盘仓位：<strong>{pct(summary.mean_close_gross_all_saved_sessions)}</strong>
      {summary.close_gross_known_sessions != null ? `，有值 ${number(summary.close_gross_known_sessions)} 日` : ''}。这与上方完整交集不是同一分母；未知不补零，缺数量计算记录也不等于零目标。</p>
    {typeof summary.stale_valuation_sessions === 'number' && summary.stale_valuation_sessions > 0 ? <p className="window-stale">原期间包含旧价估值，不能把这些保存权重当作同期可成交价格下的可靠仓位。</p> : null}
    <h4>逐日仓位实现 · {allSessions ? '全部保存日' : '最近20个保存日'}</h4>
    {value.sessions.length === 0 ? <p className="subtle">未提供逐日记录，不能补成0仓位。</p> : <>
      <div className="table-scroll"><table className="compact" aria-label="逐日仓位如何实现"><thead><tr>
        <th>日期 / 记录</th><th>采用目标</th><th>整手目标</th><th>成交后参考价仓位</th><th>收盘仓位</th><th>同参考口径仓位差</th><th>原数量原因 / 约束</th>
      </tr></thead><tbody>{sessions.map(row => <tr key={row.date} data-realization-day={row.date} data-realization-matched={row.matched}>
        <td>{row.date}<br /><span className="subtle">{row.matched ? '同日记录完整' : '记录不全，缺项保留'}；{number(row.diagnostic_rows)} 条数量记录{row.stale_valuation ? '；旧价估值' : ''}</span></td>
        <td>{pct(row.target_adopted_gross)}</td><td>{pct(row.lot_target_gross)}</td><td>{pct(row.realized_reference_gross)}</td><td>{pct(row.close_gross)}</td>
        <td className="opening-daily-wrapped">目标减整手：{points(row.target_to_lot_gap)}<br />整手减成交：{points(row.lot_to_realized_gap)}<br />目标减成交：{points(row.target_to_realized_gap)}</td>
        <td className="opening-daily-wrapped"><details><summary>查看原记录与缺项</summary><p className="subtle">数量原因：{reasonList(row.mechanical_reasons)}</p><p className="subtle">执行约束：{reasonList(row.constraint_reasons)}</p>
          {row.warnings?.map((warning, index) => <p className="window-stale" key={index}>{warning}</p>)}</details></td>
      </tr>)}</tbody></table></div>
      {value.sessions.length > 20 ? <button type="button" className="text-button" aria-expanded={allSessions} onClick={() => setAllSessions(previous => !previous)}>{allSessions ? '只看最近20日' : `展开全部 ${number(value.sessions.length)} 个保存日`}</button> : null}
    </>}
    <details className="offline-details"><summary>原记录中的数量原因与执行约束</summary>
      <p className="subtle">同一证券日可有多个标记，条数不是独立损失笔数；不同原因不能直接相加解释全部仓位差。</p>
      {value.reason_counts.length === 0 ? <p className="subtle">未提供原因汇总，不能推断为没有约束。</p>
        : <div className="table-scroll"><table className="compact" aria-label="仓位实现原原因汇总"><thead><tr><th>原原因</th><th>类别</th><th>记录条数</th><th>涉及日数</th></tr></thead><tbody>{value.reason_counts.map((row, index) => <tr key={`${row.category}-${row.reason}-${index}`}>
          <td className="opening-daily-wrapped">{reasonLabel(row.reason)}</td><td>{row.category === 'mechanical' || row.category === 'mechanical_reason' ? '数量计算' : row.category === 'constraint' || row.category === 'constraint_flag' ? '执行约束' : row.category}</td><td>{number(row.count)}</td><td>{number(row.day_count)}{row.days != null && row.days.length > 0 ? <details><summary>原日期</summary><p className="opening-daily-wrapped">{row.days.join('、')}</p></details> : null}</td>
        </tr>)}</tbody></table></div>}
    </details>
    <details className="offline-details"><summary>保存来源、停止位置与计算口径</summary><dl className="opening-daily-case-audit">
      <dt>源状态</dt><dd>{value.source_status}</dd><dt>原停止信息</dt><dd>{original(value.source_stop)}</dd>
      <dt>保存文件</dt><dd className="opening-daily-wrapped">{original(value.source_files)}</dd>
    </dl>{value.notes.map((note, index) => <p className="subtle" key={index}>{note}</p>)}</details>
  </div>;
}

export function PortfolioRealization() {
  const [expanded, setExpanded] = useState(false);
  const [cases, setCases] = useState<CaseIndex[] | null>(null);
  const [selected, setSelected] = useState('');
  const [response, setResponse] = useState<RealizationResponse | null>(null);
  const [indexError, setIndexError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [indexBusy, setIndexBusy] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!expanded) { setIndexBusy(false); return; }
    const controller = new AbortController();
    setIndexBusy(true); setIndexError('');
    api<RealizationResponse>('/trading/research/portfolio-realization', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setCases(value.cases); })
      .catch((error: Error) => { if (!controller.signal.aborted) { setCases(null); setIndexError(error.message); } })
      .finally(() => { if (!controller.signal.aborted) setIndexBusy(false); });
    return () => controller.abort();
  }, [expanded, version]);
  useEffect(() => {
    if (!expanded || !selected) { setDetailBusy(false); return; }
    const controller = new AbortController();
    setDetailBusy(true); setDetailError(''); setResponse(null);
    api<RealizationResponse>(`/trading/research/portfolio-realization?case_id=${encodeURIComponent(selected)}`, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        if (value.selected_case_id !== selected || (value.detail != null && value.detail.case_id !== selected)) throw new Error('保存结果未能对应所选案例，请只读刷新后再核对。');
        setResponse(value);
      })
      .catch((error: Error) => { if (!controller.signal.aborted) { setResponse(null); setDetailError(error.message); } })
      .finally(() => { if (!controller.signal.aborted) setDetailBusy(false); });
    return () => controller.abort();
  }, [expanded, selected, version]);
  const chosen = cases?.find(row => row.case_id === selected);
  return <section className="trade-section" aria-label="仓位如何实现" data-testid="portfolio-realization" aria-busy={indexBusy || detailBusy}>
    <div className="opening-daily-heading"><h2>仓位如何实现</h2>
      <button type="button" className="opening-daily-case-button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起保存的仓位实现' : '查看保存的仓位实现'}</button></div>
    <p className="subtle">对照配置、原目标、整手数量与实际持仓。这里只读取已保存研究账户，不运行策略或提交委托。</p>
    {expanded ? <>
      <div className="opening-daily-heading"><label className="inline-field">独立研究账户<select aria-label="选择仓位实现研究账户" value={selected} disabled={indexBusy || cases == null} onChange={event => { setSelected(event.target.value); setResponse(null); setDetailError(''); }}>
        <option value="">请选择账户，不自动按表现选择</option>{cases?.map(row => <option key={row.case_id} value={row.case_id}>{row.candidate_label} · {row.label}{row.source_available ? '' : '（来源待核）'}</option>)}</select></label>
        <button type="button" className="icon-button" aria-label="只读刷新仓位实现" title="只读刷新保存结果" disabled={indexBusy || detailBusy} onClick={() => setVersion(value => value + 1)}><RefreshCw size={16} /></button></div>
      {indexBusy ? <p className="subtle" role="status">正在读取保存账户目录…</p> : null}
      {indexError ? <p className="window-stale" role="status">账户目录暂不可读：{indexError}</p> : null}
      {cases?.length === 0 ? <p className="subtle">尚未提供保存账户目录。</p> : null}
      {chosen ? <p className="subtle">{chosen.label}：原保存 {number(chosen.committed_sessions)} / {number(chosen.expected_sessions)} 日；最后保存日 {chosen.last_saved_day ?? '—'}。{chosen.source_available ? '' : '当前来源不可完整读取，缺项不补零。'}</p> : null}
      {detailBusy ? <p className="subtle" role="status">正在读取所选账户的保存记录…</p> : null}
      {detailError ? <p className="window-stale" role="status">仓位实现暂不可读：{detailError}</p> : null}
      {response?.detail ? <Detail key={response.detail.case_id} value={response.detail} />
        : response ? <div className="window-stale" role="status">此账户尚无可展示的仓位实现明细，不能使用其他账户或零值补齐。<details><summary>查看原缺项</summary>{response.error ?? response.detail_status ?? chosen?.error ?? '原保存记录未提供详情。'}</details></div> : null}
      {response ? <p className="subtle">页面读取时间：{response.read_at}；这不是策略运行或新行情时间。</p> : null}
    </> : null}
  </section>;
}
