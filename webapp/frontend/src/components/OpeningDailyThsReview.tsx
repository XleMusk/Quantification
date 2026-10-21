import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';

type Ref = { path?: string; file?: string; sha256: string };
type MissingItem = string | { code?: string; message?: string; reason?: string; label?: string };
type SavedIntent = {
  kind?: string | null; weights?: Record<string, number> | null;
  reason?: string | null; published_at?: string | null; source?: string | null;
};
type Review = {
  schema: string; status: string; route_id: string; case_id: string;
  source_day: string | null; execution_day: string | null; read_at?: string;
  source_head?: Ref | null; product_source_head?: Ref | null;
  saved_intent: SavedIntent | null; missing: MissingItem[]; diagnostic: null;
  execution_admitted?: boolean; selection_enabled?: boolean; prefill_allowed?: boolean;
  flags?: { execution_admitted?: boolean; selection_enabled?: boolean; prefill_allowed?: boolean };
};
const decisions: Record<string, string> = { FULL_TARGET: '完整研究目标', NO_CHANGE: '保持原目标', EXIT_ALL: '退出目标' };
const missingNames: Record<string, string> = {
  exact_execution_day_account_and_broker_quotes: '需在计划执行日取得同一账户的现金、持仓、可卖数量和券商报价。',
  account_observation: '缺少计划执行日的账户快照，包括现金、持仓和可卖数量。',
  saved_committed_opening_product: '尚无可对应的已保存纸面方案。',
  published_research_plan_account_not_created: '此方案尚未创建独立研究账户，目前只保存了研究目标。',
};
function missingText(item: MissingItem) {
  if (typeof item === 'string') return missingNames[item] || item;
  const detail = item.message || item.reason || item.label;
  return detail ? `${detail}${item.code ? `（${item.code}）` : ''}` : item.code || '保存缺项尚未说明';
}

export function OpeningDailyThsReview({ routeId, caseId, sourceDay, executionDay, sourceSha, productSha }: {
  routeId: string; caseId: string; sourceDay: string | null; executionDay: string | null;
  sourceSha: string | null; productSha: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setBusy(true); setReview(null); setError('');
    api<Review>(`/trading/research/opening-daily/${encodeURIComponent(routeId)}/cases/${encodeURIComponent(caseId)}/ths-review`, { signal: controller.signal })
      .then(next => {
        if (controller.signal.aborted) return;
        if (next.schema !== 'ths_saved_opening_review_v1' || next.route_id !== routeId || next.case_id !== caseId
          || next.source_day !== sourceDay || next.execution_day !== executionDay) throw new Error('复核资料与当前案例或方案日期不一致，请刷新上方方案。');
        if ((next.source_head && next.source_head.sha256 !== sourceSha)
          || (next.product_source_head && next.product_source_head.sha256 !== productSha)) throw new Error('方案来源已变化，请先刷新上方日更状态。');
        if ((next.execution_admitted ?? next.flags?.execution_admitted) !== false
          || (next.prefill_allowed ?? next.flags?.prefill_allowed) !== false || next.diagnostic !== null
          || !Array.isArray(next.missing)) throw new Error('复核响应的只读范围或缺项格式不符合预期。');
        setReview(next);
      })
      .catch((problem: Error) => { if (!controller.signal.aborted) { setReview(null); setError(problem.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [expanded, routeId, caseId, sourceDay, executionDay, sourceSha, productSha, version]);
  const weights = review?.saved_intent?.weights;
  return <section className="opening-ledger" aria-label="案例账户复核" aria-busy={busy}>
    <button className="opening-daily-case-button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起账户复核' : '账户复核'}</button>
    {expanded && <div className="opening-ledger-content">
      <div className="opening-daily-heading"><h4>账户复核 · 保存方案</h4><button className="icon-button" aria-label="刷新本案例账户复核" title="只读刷新复核资料" disabled={busy} onClick={() => setVersion(value => value + 1)}><RefreshCw size={16} /></button></div>
      <p className="window-stale" role="status">暂不可预填。此处只核对已保存研究目标和缺项，尚未接入账户或报价。</p>
      {busy && <p className="subtle" role="status">正在读取本案例保存的复核资料…</p>}
      {error && <p className="window-stale" role="status">复核读取失败：{error}</p>}
      {review && <div data-ths-review-case={caseId} data-ths-review-status={review.status}>
        <dl className="opening-daily-facts">
          <div><dt>研究来源日</dt><dd>{review.source_day || '—'}</dd></div>
          <div><dt>计划执行日</dt><dd>{review.execution_day || '—'}</dd></div>
          <div><dt>原保存决策</dt><dd data-ths-review-field="decision">{review.saved_intent?.kind ? decisions[review.saved_intent.kind] || review.saved_intent.kind : '—'}</dd></div>
          <div><dt>原方案发布时间</dt><dd>{review.saved_intent?.published_at || '未保存'}</dd></div>
        </dl>
        <h4>尚缺资料</h4>
        {review.missing.length ? <ul data-ths-review-field="missing">{review.missing.map((item, index) => <li key={index}>{missingText(item)}</li>)}</ul>
          : <p className="subtle">此记录未列缺项；账户复核和预填仍未开放。</p>}
        <h4>原研究目标权重</h4>
        {weights == null ? <p className="subtle">原记录未提供目标权重，不能按空仓理解。</p>
          : Object.keys(weights).length === 0 ? <p className="subtle">原保存目标权重为空；尚未换算账户操作。</p>
            : <div className="table-scroll"><table className="compact" aria-label="账户复核原研究目标权重"><thead><tr><th>证券</th><th>原目标权重</th></tr></thead><tbody>{Object.entries(weights).map(([symbol, weight]) => <tr key={symbol}><td>{symbol}</td><td>{typeof weight === 'number' && Number.isFinite(weight) ? `${fmt(weight * 100, 2)}%` : '待核'}</td></tr>)}</tbody></table></div>}
        {review.saved_intent?.reason && <p className="subtle opening-daily-case-note">原保存原因：{review.saved_intent.reason}</p>}
        {review.read_at && <p className="subtle">页面读取 {review.read_at}</p>}
      </div>}
    </div>}
  </section>;
}
