import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { OpeningDailyCases } from './OpeningDailyCases';

type FileRef = { path: string; sha256: string };
type RouteView = {
  id: string; family: string; status: string; source_changed: boolean | null;
  stale: boolean; error: string | null; attempt_status: string | null;
  input_id: string | null; source_head: FileRef | null; product_source_head: FileRef | null;
  source_day: string | null; execution_day: string | null;
  case_count: number | null; ready_case_count: number | null; not_ready_case_count: number | null;
  candidate_order_count: number | null; product_status: string | null;
  committed_at: string | null; refs: Record<string, FileRef>;
};
type DailyView = {
  schema: string; read_at: string; status: string; reason?: string | null;
  routes: RouteView[]; saved_artifacts_revalidated: boolean;
};
const routeNames: Record<string, string> = {
  fixed_lowvol: '固定低波 · 历史对照', eight_type: '八类龙头', leader20: '原20组龙头', alpha158: '冻结 Alpha158',
  native_preopen: '原生短线原始方案', native_ipoqualified: '原生短线资格修正版',
  native_lowvol: '固定低波 · 原纸面起点',
  native_lowvol_bootstrap_20261019: '固定低波 · 20日纸面启动',
  entry_comparison: '入场与持有确认 · 15组', public_seat_static: '公开席位 · 固定事件4组',
  public_seat_append: '公开席位 · 追加事件4组',
  dated_catchup: '补涨龙 · 前日身份', eight_type_residual: '八类龙头 · 持续退出对照',
  known_identity: '穿越龙 · 固定身份',
  leader_type_router: '龙型切换 · 原登记',
  leader_type_router_runtime_repair: '龙型切换 · 修复版本4组',
};
const statusNames: Record<string, string> = {
  current_saved_product: '保存方案与来源一致',
  source_changed_product_required: '来源已更新，待生成对应方案',
  source_unavailable: '当前来源不可读取',
  recoverable_saved_product: '已有保存结果，待恢复发布',
  failed_same_input_no_retry: '本来源处理失败，保留原失败',
  unresolved_same_input_no_retry: '本来源处理尚无确认结果',
};
const refNames: Record<string, string> = {
  preview: '纸面预览', opening_pack: '开盘纸面包', intent: '保存的组合意图', result: '方案生成回执',
};
const count = (value: number | null | undefined) => value == null ? '—' : value;

function SavedRef({ label, value }: { label: string; value: FileRef | null }) {
  return <div className="opening-daily-ref"><dt>{label}</dt><dd>{value ? <><code>{value.path}</code><small>SHA256 {value.sha256}</small></> : '—'}</dd></div>;
}

export function OpeningDailyStatus() {
  const [view, setView] = useState<DailyView | null>(null);
  const [error, setError] = useState('');
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [busy, setBusy] = useState(true);
  const [detailRoute, setDetailRoute] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    api<DailyView>('/trading/research/opening-daily', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setView(value); setError(''); } })
      .catch((err: Error) => { if (!controller.signal.aborted) { setView(null); setError(err.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [refreshVersion]);
  return <section className="trade-section opening-daily" aria-label="每日纸面方案" aria-busy={busy}>
    <div className="opening-daily-heading"><div><h2>每日纸面方案</h2><p className="subtle">研究路线共用日更入口；账本或已发布计划变化时才生成新方案。</p></div>
      <button className="icon-button" aria-label="刷新每日纸面方案状态" title="只读刷新" disabled={busy} onClick={() => setRefreshVersion(v => v + 1)}><RefreshCw size={17} /></button>
    </div>
    {error && <p className="window-stale" role="status">保存状态读取失败：{error}。本次不展示旧成功结果。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取保存状态…</p>}
    {view && <>
      <p className="subtle opening-daily-read-at">页面核对 {view.read_at}；方案保存时间见各路线。</p>
      {view.status === 'unavailable' && <p className="window-stale" role="status">尚无可读取的日更登记：{view.reason || '未提供原因'}。</p>}
      <div className="opening-daily-grid">{view.routes.map(route => <article key={route.id} data-opening-route={route.id} data-status={route.status} data-stale={route.stale}>
        <h3>{routeNames[route.id] || route.id}</h3>
        <p role="status" className={route.status === 'current_saved_product' && !route.stale ? 'opening-daily-state' : 'window-stale'}>{statusNames[route.status] || route.status}</p>
        {route.stale && <p className="window-stale" data-saved-product="stale">下列日期、数量和文件属于旧方案，不代表当前来源的新结果。</p>}
        {route.error && <p className="window-stale" data-route-error>{route.error}</p>}
        <dl className="opening-daily-facts">
          <div><dt>方案来源日</dt><dd data-field="source-day">{route.source_day || '—'}</dd></div>
          <div><dt>计划执行日</dt><dd data-field="execution-day">{route.execution_day || '—'}</dd></div>
          <div><dt>独立案例</dt><dd data-field="case-count">{count(route.case_count)}</dd></div>
          <div><dt>已生成 / 缺项</dt><dd data-field="ready-count">{count(route.ready_case_count)} / {count(route.not_ready_case_count)}</dd></div>
          <div><dt>候选委托行</dt><dd data-field="candidate-order-count">{count(route.candidate_order_count)}</dd></div>
        </dl>
        <p className="subtle opening-daily-saved-at">方案保存 {route.committed_at || '—'}</p>
        <button className="opening-daily-case-button" aria-label={`查看${routeNames[route.id] || route.id}逐案方案`} aria-expanded={detailRoute === route.id} disabled={busy || (!route.refs.opening_pack && !(['native_lowvol', 'registered_research'].includes(route.family) && route.refs.result))} onClick={() => setDetailRoute(current => current === route.id ? null : route.id)}>查看逐案目标与委托</button>
        <details className="offline-details" aria-label={`${routeNames[route.id] || route.id}保存来源`}><summary>保存文件与处理记录</summary>
          <dl className="opening-daily-refs">
            {Object.entries(route.refs).map(([key, value]) => <SavedRef key={key} label={refNames[key] || key} value={value} />)}
            <SavedRef label="方案绑定来源" value={route.product_source_head} />
            <SavedRef label="当前来源" value={route.source_head} />
            <div className="opening-daily-ref"><dt>处理记录</dt><dd>状态 {route.attempt_status || '—'}<br />输入 {route.input_id || '—'}</dd></div>
          </dl>
        </details>
      </article>)}</div>
      {view.routes.filter(route => route.id === detailRoute).map(route => <OpeningDailyCases key={`${route.id}:${route.input_id}:${route.product_source_head?.sha256}:${route.source_head?.sha256}`} routeId={route.id} label={routeNames[route.id] || route.id} sourceSha={route.source_head?.sha256 ?? null} productSha={route.product_source_head?.sha256 ?? null} close={() => setDetailRoute(null)} />)}
      <p className="subtle opening-daily-footnote">各案例独立比较，候选委托行是纸面预检，尚不是账户成交。刷新只读保存状态，不运行策略或恢复任务。</p>
    </>}
  </section>;
}
