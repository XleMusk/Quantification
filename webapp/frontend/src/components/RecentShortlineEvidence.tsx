import { useEffect, useState } from 'react';
import { api } from '../api';
import { fmt } from '../types';

type Stage = { stage: number; known_stage_events: number; complete_labels: number; unresolved_labels: number;
  hit4_complete_known_stage: number; hit5_complete_known_stage: number };
type Batch = { day: string; stage: number; status: string; unknown_stage_membership_count: number;
  unknown_rule_field_count: number; known_rule_match_rows: number };
type View = { status: string; source_file: string | null; read_at: string; created_at?: string; method_id?: string;
  source_days: string[]; base_history_cutoff?: string; event_rows: number | null; complete_labels: number | null;
  unresolved_labels: number | null; unresolved_membership_rows: number | null; known_rule_match_rows: number | null;
  incomplete_stage_day_batches: number | null; stage_day_batch_count?: number; by_stage: Stage[];
  stage_day_batches: Batch[]; candidate_batches_source_file?: string; fixed_rule?: string };
const count = (value?: number | null) => value == null ? '待核验' : fmt(value, 0);
const stageName = (stage: number) => `${['', '首', '二', '三'][stage] || stage}板事件`;

export function RecentShortlineEvidence({ refreshVersion }: { refreshVersion: number }) {
  const [view, setView] = useState<View | null>(null), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setError('');
    api<View>('/trading/research/recent-shortline', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } });
    return () => controller.abort();
  }, [refreshVersion]);
  return <div className="offline-subsection" aria-label="新来源短线接力诊断">
    <h3>短线接力 · 新来源诊断</h3>
    {error && <p className="subtle" role="status">短线新批次读取失败：{error}。不采用旧批次替代，其他研究回执独立读取。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取已保存短线诊断，不启动训练、策略或取数。</p>}
    {view?.status === 'unavailable' && <p className="subtle" role="status">尚无新方法完成回执；旧短线研究仍单独保留。</p>}
    {view?.status === 'completed_recent_shortline_diagnostics' && <>
      <p className="subtle">来源收盘日 {view.source_days.join('、')}；历史前缀截止 {view.base_history_cutoff}。实际派生 {view.created_at}，属于事后研究，不能当作来源日盘前已发布信号。</p>
      <div className="window-summary"><span>已知阶段事件 <strong>{count(view.event_rows)}</strong></span><span>标签已完成 / 未完成 <strong>{count(view.complete_labels)} / {count(view.unresolved_labels)}</strong></span><span>阶段归属未知 <strong>{count(view.unresolved_membership_rows)} 条</strong></span><span>已知规则匹配 <strong>{count(view.known_rule_match_rows)} 条</strong></span></div>
      <p className="subtle">阶段归属未知另列，未包含在已知阶段事件中；未完成标签与未知归属均不计失败。{count(view.known_rule_match_rows)} 条只是在已知记录中匹配固定表达式，不是当前信号、入选组合或可交易数量。</p>
      <div className="table-scroll"><table className="compact" aria-label="短线新批次阶段标签"><thead><tr><th>阶段</th><th>已知阶段事件</th><th>标签已完成</th><th>标签未完成</th><th>已完成事件达四板</th><th>已完成事件达五板</th></tr></thead><tbody>{view.by_stage.map(row => <tr key={row.stage}><td>{stageName(row.stage)}</td><td>{count(row.known_stage_events)}</td><td>{count(row.complete_labels)}</td><td>{count(row.unresolved_labels)}</td><td>{count(row.hit4_complete_known_stage)}</td><td>{count(row.hit5_complete_known_stage)}</td></tr>)}</tbody></table></div>
      <p className="subtle">四板、五板仅统计阶段已知且标签完成的事件，须有准确交易日的非涨停后继证据才能结束标签；同一股票或连板路径可重复出现，不是全体股票晋级率。</p>
      <p className="subtle">不完整阶段日批次 {count(view.incomplete_stage_day_batches)} / {count(view.stage_day_batch_count)}。固定表达式为前窗成交额相对中位数 ≤ 1 且市场广度 ≥ 0；前窗取最多20条有效观察、至少5条，不等于20个连续交易日。未知不能当作空仓或完整空候选。</p>
      <details className="offline-details"><summary>逐日阶段覆盖 · {count(view.stage_day_batch_count)} 批</summary>
        <div className="table-scroll"><table className="compact" aria-label="短线逐日阶段覆盖"><thead><tr><th>日期</th><th>阶段</th><th>批次</th><th>可能阶段归属未知</th><th>规则字段未知</th><th>已知规则匹配</th></tr></thead><tbody>{view.stage_day_batches.map(row => <tr key={`${row.day}-${row.stage}`}><td>{row.day}</td><td>{stageName(row.stage)}</td><td>{row.status === 'incomplete' ? '不完整' : '完整研究批次'}</td><td>{count(row.unknown_stage_membership_count)}</td><td>{count(row.unknown_rule_field_count)}</td><td>{count(row.known_rule_match_rows)}</td></tr>)}</tbody></table></div>
        <p className="subtle">同日三个阶段共享可能归属未知集合，不能将三行未知人数相加。匹配行仍受整日证据不完整约束。</p>
      </details>
      <p className="subtle">新样本为完整来源的沪深活跃非 ST 范围，与旧短线母集口径不同，旧面板和评分保留。该诊断未加载已训练模型或更新权重。本卡展示事件与标签诊断，回放结果另行保存；此处不提供收益。没有订单不等于0收益。</p>
      <p className="subtle">PIT、公司行动与历史公开时间仍未核验。model_refit=false · future_labels_used_for_score=false · selection_enabled=false · execution_admitted=false。</p>
    </>}
    {view && <details className="offline-details"><summary>短线批次来源与读取时间</summary><dl><dt>完成回执</dt><dd>{view.source_file || '尚未形成'}</dd><dt>读取时间</dt><dd>{view.read_at}</dd><dt>方法</dt><dd>{view.method_id || '尚未形成'}</dd><dt>阶段日回执</dt><dd>{view.candidate_batches_source_file || '尚未形成'}</dd></dl></details>}
  </div>;
}
