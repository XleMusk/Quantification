import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';

type DatedCoverage = { day: string; full_dated_roster_rows: number; active_non_ST_valid_observations: number;
  suspended_rows_retained_unscored: number; ST_rows_retained_unscored: number; not_scored_count: number;
  native_grid_rows?: number; unobserved_or_out_of_scope_grid_rows?: number;
  missing_feature_cells?: number; all_missing_active_feature_rows?: number };
type RecentAlpha158View = { status: string; source_file: string; read_at: string; reason?: string;
  started_at?: string; finished_at?: string | null; actual_computed_at?: string | null; source_available_at?: string;
  requested_feature_dates?: string[]; score_dates: string[]; score_rows: number | null;
  partitions_completed?: number; native_symbols_completed?: number; native_symbols_total?: number;
  native_feature_rows?: number; native_grid_expected_rows?: number; coverage: DatedCoverage[];
  base_history_end?: string; recent_source_days?: string[]; provider_source_file?: string; method_id?: string;
  provider_calendar_start?: string; provider_calendar_end?: string; provider_dates?: number; model_run?: string;
  error?: string | null };
const statuses: Record<string, string> = { unavailable: '尚无新批次回执', building_dated_native_features: '保存状态：逐日原生特征构建中',
  completed_research_only_dated_score_batch: '逐日评分完成，仅供研究', failed_recent_source_alpha158_scores: '当前批次失败' };
const count = (value?: number | null) => value == null ? '待完成' : fmt(value, 0);

export function RecentAlpha158Evidence({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<RecentAlpha158View | null>(null), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setError('');
    api<RecentAlpha158View>('/trading/research/recent-alpha158', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } });
    return () => controller.abort();
  }, [refreshVersion]);
  const complete = view?.status === 'completed_research_only_dated_score_batch';
  return <div className="offline-subsection" aria-label="新来源 Alpha158 批次">
    <h3>新来源 Alpha158 · 冻结模型批次</h3>
    {error && <p className="subtle" role="status">新来源模型回执读取失败：{error}。其他研究证据独立读取，不采用旧批次替代。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取新来源模型回执，不启动特征计算或评分。</p>}
    {view && <>
      <p role="status">{statuses[view.status] || view.status}{view.status === 'unavailable' ? '：固定外部路径尚未形成可读产物。' : '。'}</p>
      {view.status !== 'unavailable' && <>
        <div className="window-summary"><span>原生证券进度 <strong>{count(view.native_symbols_completed)} / {count(view.native_symbols_total)}</strong></span><span>已保存分块 <strong>{count(view.partitions_completed)}</strong></span><span>原生特征网格 <strong>{count(view.native_feature_rows)} / {count(view.native_grid_expected_rows)}</strong></span><span>评分行 <strong>{complete ? count(view.score_rows) : '尚无完成评分'}</strong></span></div>
        <p className="subtle">历史前缀截至 {view.base_history_end}，近期完整来源 {view.recent_source_days?.join('、')}；新混合来源的累计涨跌幅价格代理与旧复权训练价格存在跨来源差异，仅作诊断。逐日期分别归一化种子排名；旧跨日评分批次保留，不直接拼接比较。</p>
        <div className="window-summary"><span>来源实际可得 <strong>{view.source_available_at}</strong></span><span>实际开始 <strong>{view.started_at}</strong></span><span>实际计算完成 <strong>{complete ? view.actual_computed_at : '尚未完成'}</strong></span></div>
        <p className="subtle">请求特征日期 {view.requested_feature_dates?.join('、')}；{complete ? `已完成评分日期 ${view.score_dates.join('、')}` : '当前没有可展示的终态评分日期'}。原生特征覆盖完整固定证券×请求日期网格，评分仅限各日真实观察到的有效、活跃、非 ST 样本；网格空行不能靠填补变成活跃证券。</p>
        {view.status === 'failed_recent_source_alpha158_scores' && <p role="status">失败时间 {view.finished_at}；{view.error || '未保存具体错误'}。保留实际已完成分块，不采用旧 native 或残留评分文件冒充成功。</p>}
        <details className="offline-details"><summary>逐日期覆盖 · {view.coverage.length} 日</summary>
          <div className="table-scroll"><table className="compact" aria-label="Alpha158 逐日期覆盖"><thead><tr><th>日期</th><th>原始完整名册</th><th>完整原生网格</th><th>活跃非 ST 有效观察</th><th>原名册未评分</th><th>停牌保留</th><th>ST 保留</th><th>网格中未观察或范围外</th><th>活跃样本缺特征单元</th><th>全特征缺失活跃样本</th></tr></thead><tbody>{view.coverage.map(row => <tr key={row.day}><td>{row.day}</td><td>{count(row.full_dated_roster_rows)}</td><td>{count(row.native_grid_rows)}</td><td>{count(row.active_non_ST_valid_observations)}</td><td>{count(row.not_scored_count)}</td><td>{count(row.suspended_rows_retained_unscored)}</td><td>{count(row.ST_rows_retained_unscored)}</td><td>{count(row.unobserved_or_out_of_scope_grid_rows)}</td><td>{count(row.missing_feature_cells)}</td><td>{count(row.all_missing_active_feature_rows)}</td></tr>)}</tbody></table></div>
          <p className="subtle">停牌与 ST 计数可能重叠，不相加充当未评分总数。特征缺失计数不等于可成交证明；PIT、上市资格和公司行动尚未核验。</p>
        </details>
      </>}
      <p className="subtle">只读批次摘要，不展示证券排名，不生成目标、订单或同花顺预填。model_refit=false · future_labels_used_for_score=false · selection_enabled=false · execution_admitted=false；共享 Engine/Ledger 回放尚未由本卡接入。</p>
      <details className="offline-details"><summary>模型批次来源与读取时间</summary><dl><dt>固定批次</dt><dd>{view.source_file}</dd><dt>读取时间</dt><dd>{view.read_at}</dd><dt>方法</dt><dd>{view.method_id || '尚未形成'}</dd><dt>来源视图</dt><dd>{view.provider_source_file || '尚未形成'}</dd><dt>原生日历</dt><dd>{view.provider_calendar_start ? `${view.provider_calendar_start} 至 ${view.provider_calendar_end}，${count(view.provider_dates)} 个观察日期` : '尚未形成'}</dd><dt>冻结模型回执</dt><dd>{view.model_run || '尚未形成'}</dd></dl></details>
    </>}
  </div>;
}
