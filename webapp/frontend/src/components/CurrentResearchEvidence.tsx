import { useEffect, useState } from 'react';
import { RefreshCw, ShieldCheck } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage, Warning } from './Common';
import { RecentAlpha158Evidence } from './RecentAlpha158Evidence';
import { RecentShortlineEvidence } from './RecentShortlineEvidence';
import { LeaderExitHistoryEvidence } from './LeaderExitHistoryEvidence';
import { fmt } from '../types';
import type { CloseWorkbench, LeaderComparison, MarketForecastScoreboard } from '../tradingTypes';

const familyNames: Record<string, string> = { stage1_amount_baseline: '首板接力', stage2_amount_baseline: '二板接力', stage3_amount_baseline: '三板接力', trend_leader_20_60: '趋势龙头代理', daily_rebound_proxy: '收盘反包代理' };
const poolNames: Record<string, string> = { common_pre_window_symbol20: '固定前窗20股', common_pre_window_uncapped: '全部前窗证券' };
const policyNames: Record<string, string> = { complete_empty_and_five_session_exit: '基本退出', verified_full_market_breadth_defense: '广度防守退出' };
const caseNames: Record<string, string> = { completed_already_inspected_engine_diagnostic: '完成旧窗诊断', unavailable_complete_cohort_decision_batches: '缺完整决策批次', unavailable_verified_full_market_breadth: '缺广度核验证据', blocked_engine_replay: '行情阻断' };
const forecastNames: Record<string, string> = { pending: '待核验', scored: '已核验', missing_price: '缺价格证据', retrospective: '事后登记' };
const closeNames: Record<string, string> = { missing: '尚无采集定位', running: '采集中', collector_not_verified: '采集进程未核实', complete_full_dated_close_inventory_not_admitted: '完整名册已采集', partial_budget_checkpoint_not_full_close: '预算检查点，尚未完整', failed_stopped_no_retry: '采集失败，已停止', failed_roster_no_retry: '名册失败，已停止' };
const count = (value?: number | null) => value == null ? '—' : fmt(value, 0);
const percent = (value?: number | null, missing = '不可用') => value == null ? missing : `${fmt(value * 100, 2)}%`;

type ClosePublicationCase = { case_id: string; family: string; pool: string; exit_policy: string; status: string;
  ledger_status: string; no_change_reason: string | null; intent_kind: string; target_weights: Record<string, number>;
  target_cash_weight: number | null; holdings_retention_and_expiry_account_bound: boolean };
type ClosePublicationView = { status: string; source_file: string | null; read_at: string; source_close_day?: string;
  recorded_at?: string; proposed_execution_day?: string; next_open_deadline?: string; timeliness?: string;
  ledger_binding?: string; ledger_checkpoint?: string | null; case_count: number; cases: ClosePublicationCase[];
  history_prefix_binding?: string; history_input_available_at?: string | null; history_source_supplement?: string | null;
  history_source_specified_at?: string | null; source_ready_at?: string | null;
  recent_source_history?: { base_history_end: string; available_at: string; view_sessions: string[]; view_file: string; shared_view_id: string } | null };
const publicationCaseNames: Record<string, string> = { late_context_only: '迟到，仅供研究', unavailable_fixed_rule_target: '规则目标不可用',
  unbound_pure_rule_target_preview: '未绑定纯规则目标', account_bound_research_target_preview: '保存检查点的研究目标' };

function LeaderClosePublicationCard({ version }: { version: number }) {
  const [view, setView] = useState<ClosePublicationView | null>(null), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setError('');
    api<ClosePublicationView>('/trading/research/leader-close-publication', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setView(value); })
      .catch(reason => { if (!controller.signal.aborted) { setView(null); setError(reason.message); } });
    return () => controller.abort();
  }, [version]);
  const history = view?.recent_source_history;
  return <div className="offline-subsection" aria-label="固定规则目标发布">
    <h3>固定规则目标发布 · 只读权重</h3>
    {error && <p className="subtle" role="status">规则目标发布读取失败：{error}。其他研究回执独立读取。</p>}
    {!view && !error && <p className="subtle" role="status">正在读取已保存的规则目标，不运行新策略。</p>}
    {view?.status === 'missing' && <p className="subtle">尚无保存的规则目标发布。</p>}
    {view && view.status !== 'missing' && <>
      <div className="window-summary"><span>行情收盘日 <strong>{view.source_close_day}</strong></span><span>实际记录 <strong>{view.recorded_at}</strong></span><span>原拟执行日 <strong>{view.proposed_execution_day}</strong></span><span>记录时效 <strong>{view.timeliness === 'late_context_only' ? '迟到，仅供研究' : '原次开盘前记录'}</strong></span></div>
      <p className="subtle">原次开盘截止 {view.next_open_deadline}；迟到记录保留原拟执行日，不顺延到下一日。{view.ledger_binding === 'unbound' ? '本发布未绑定 Ledger，持仓留存和到期退出尚无账户依据。' : '本发布仅保存 Ledger 检查点，账户与订单仍未准入。'}</p>
      <p className="subtle">全部 {view.case_count} 个固定比较项按原顺序保留，权重是纯规则研究目标。未生成订单或同花顺预填；execution_admitted=false · selection_enabled=false · ledger_mutated=false。</p>
      {history ? <p className="subtle">历史输入采用新完整来源前缀：原历史截止 {history.base_history_end}，近期日期 {history.view_sessions.join('、')}；实际可得 {view.history_input_available_at || history.available_at}。已保存新前缀的协议绑定，历史资格和公司行动仍待核验。</p>
        : <p className="subtle">旧输入路径：该发布未记录新完整来源前缀绑定；不能据此认定历史输入已补齐。</p>}
      {history && <p className="subtle">前缀协议实际登记 {view.history_source_specified_at || '旧发布未记录'}；全部输入实际就绪 {view.source_ready_at || '旧发布未记录'}。这些时间与目标发布的实际记录时间分别保留。</p>}
      <details className="offline-details"><summary>全部规则目标与完整权重 · {view.case_count} 项</summary>
        <div className="table-scroll"><table className="compact" aria-label="完整规则目标权重"><thead><tr><th>策略</th><th>样本</th><th>退出规则</th><th>发布状态</th><th>目标语义</th><th>完整证券权重</th><th>现金权重</th><th>待绑定或不可用原因</th></tr></thead><tbody>{view.cases.map(row => <tr key={row.case_id}>
          <td>{familyNames[row.family] || row.family}</td><td>{poolNames[row.pool] || row.pool}</td><td>{policyNames[row.exit_policy] || row.exit_policy}</td><td>{publicationCaseNames[row.status] || row.status}</td><td>{row.intent_kind === 'NO_CHANGE' ? '保持原目标' : '完整权重目标'}</td>
          <td>{row.intent_kind === 'NO_CHANGE' ? '未产生新目标' : Object.entries(row.target_weights).map(([symbol, weight]) => `${symbol} ${percent(weight)}`).join('；') || '空证券目标'}</td><td>{row.intent_kind === 'NO_CHANGE' ? '不变更' : percent(row.target_cash_weight, '待核验')}</td><td>{row.no_change_reason || (row.holdings_retention_and_expiry_account_bound ? '保存检查点；仍缺订单定量和账户准入' : '未绑定当日 Ledger，缺账户与报价依据')}</td>
        </tr>)}</tbody></table></div><p className="subtle">所有已保存证券权重完整展示，不按收益选优、不截取组合。空证券目标与保持原目标含义不同，均不能据此提交账户委托。</p>
      </details>
      <details className="offline-details"><summary>目标发布来源与读取时间</summary><dl><dt>目标发布</dt><dd>{view.source_file}</dd><dt>读取时间</dt><dd>{view.read_at}</dd><dt>Ledger 检查点</dt><dd>{view.ledger_checkpoint || '未绑定'}</dd><dt>历史前缀视图</dt><dd>{history?.view_file || '旧输入路径'}</dd><dt>前缀协议</dt><dd>{view.history_source_supplement || '未记录'}</dd></dl></details>
    </>}
  </div>;
}

export function CurrentResearchEvidence() {
  const [leader, setLeader] = useState<LeaderComparison | null>(null);
  const [scoreboard, setScoreboard] = useState<MarketForecastScoreboard | null>(null);
  const [close, setClose] = useState<CloseWorkbench | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setErrors([]);
    Promise.allSettled([
      api<LeaderComparison>('/trading/research/leader-comparison', { signal: controller.signal }),
      api<MarketForecastScoreboard>('/trading/research/market-scoreboard', { signal: controller.signal }),
      api<CloseWorkbench>('/trading/research/close-workbench', { signal: controller.signal }),
    ]).then(([comparison, forecasts, source]) => {
      if (controller.signal.aborted) return;
      const failures: string[] = [];
      if (comparison.status === 'fulfilled') setLeader(comparison.value);
      else { setLeader(null); failures.push(`龙头诊断读取失败：${comparison.reason.message}`); }
      if (forecasts.status === 'fulfilled') setScoreboard(forecasts.value);
      else { setScoreboard(null); failures.push(`大盘预测读取失败：${forecasts.reason.message}`); }
      if (source.status === 'fulfilled') setClose(source.value);
      else { setClose(null); failures.push(`收盘处理读取失败：${source.reason.message}`); }
      setErrors(failures); setBusy(false);
    });
    return () => controller.abort();
  }, [version]);
  const cases = leader?.cases || [];
  const rows = scoreboard?.rows || [];
  const unavailable = cases.filter(row => row.metrics == null).length;
  const pending = rows.filter(row => row.status === 'pending').length;
  return <section className="trade-section research-replays" aria-label="当前研究证据">
    <div className="research-heading"><h2>当前研究证据</h2><button aria-label="刷新研究回执" onClick={() => setVersion(value => value + 1)} disabled={busy}><RefreshCw size={15} />{busy ? '读取中' : '刷新回执'}</button></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>研究只读</strong><span>执行准入关闭</span><span>策略未验证</span><span>自动选优关闭</span></div>
    {errors.map(message => <ErrorMessage key={message} message={message} />)}
    {busy && !leader && !scoreboard && <p className="subtle" role="status">正在读取固定比较和事前预测回执。</p>}
    {close && <div className="offline-subsection" aria-label="最新收盘处理">
      <h3>{close.progress?.acquisition_mode === 'explicit_historical_research_repair' ? '历史收盘缺口补采' : '当前收盘数据任务'} · {close.progress?.day || '日期待确认'}</h3>
      <div className="window-summary"><span>状态 <strong>{closeNames[close.status] || close.status}</strong></span><span>名册进度 <strong>{count(close.progress?.completed_symbols)} / {count(close.progress?.ordinary_symbols)}</strong></span><span>进程 <strong>{close.collector?.status === 'verified_alive' ? '已核实存活' : close.collector?.status || '未核实'}</strong></span><span>检查点 <strong>{close.progress?.updated_at || '无回执'}</strong></span></div>
      <p className="subtle">当前代查询 {count(close.progress?.logical_price_queries)} 次；复用旧代完整原始记录 {count(close.progress?.reused_completed_symbols ?? 0)} 条。名册库存完成不等于全部可成交，停牌空量额保留；刷新只读回执，不启动采集。</p>
      {!close.preview && <p role="status">完整收盘研究预览尚未形成。{close.followup?.preview_exit_code != null && close.followup.preview_exit_code !== 0 ? '后续处理失败，原始进度与失败回执保留。' : '等待完整来源与后续处理；部分采集不展示市场统计或候选。'}</p>}
      {close.preview && <>
        <div className="window-summary"><span>上涨 / 下跌 <strong>{count(close.preview.tape.advance_count)} / {count(close.preview.tape.decline_count)}</strong></span><span>广度 <strong>{percent(close.preview.tape.breadth)}</strong></span><span>成交额 <strong>{fmt(close.preview.tape.amount_cny / 1e8, 2)} 亿元</strong></span><span>成交额前20集中度 <strong>{percent(close.preview.tape.top20_turnover_share)}</strong></span></div>
        <p className="subtle">上涨股与下跌股成交额关联 {percent(close.preview.tape.signed_turnover_association)}；这是量价分布，不能识别主力净流入、身份或隐藏意图。涨停阶段未知 {count(close.preview.tape.limit_up_stage_unknown_count)} 只；上市初期、历史缺口和公司行动仍需单独核对。</p>
        <details className="offline-details"><summary>最新规则候选 · 研究预览</summary><div className="table-scroll"><table className="compact" aria-label="最新规则候选"><thead><tr><th>固定规则</th><th>候选数</th><th>排序前5项预览</th></tr></thead><tbody>{close.preview.families.map(family => <tr key={family.family}><td>{familyNames[family.family] || family.family}</td><td>{count(family.candidate_count)}</td><td>{family.display_top5.map(row => row.symbol).join('、') || '空候选'}</td></tr>)}</tbody></table></div><p className="subtle">前5项仅限制展示长度，不是已选择组合；没有账户目标、订单或执行准入。</p></details>
      </>}
      <details className="offline-details"><summary>收盘处理证据</summary><dl><dt>来源回执</dt><dd>{close.source_receipt || close.source_file}</dd><dt>后续处理</dt><dd>{close.followup?.status || '无回执'}</dd><dt>研究预览</dt><dd>{close.preview?.source_file || '尚未生成'}</dd><dt>读取时间</dt><dd>{close.observed_at}</dd></dl></details>
    </div>}
    <LeaderClosePublicationCard version={version} />
    <RecentAlpha158Evidence refreshVersion={version} />
    <RecentShortlineEvidence refreshVersion={version} />
    <LeaderExitHistoryEvidence refreshVersion={version} />
    {leader?.status === 'missing' && <p className="subtle">尚无正式龙头比较回执。</p>}
    {leader && leader.status !== 'missing' && <>
      <div className="window-summary"><span>决策窗 <strong>{leader.start} 至 {leader.end}</strong></span><span>源截止 <strong>{leader.snapshot_end || '未知'}</strong></span><span>股票池冻结日 <strong>{leader.cohort?.anchor || '未知'}</strong></span><span>全部 <strong>{cases.length}</strong> 个比较项，其中 <strong>{unavailable}</strong> 个收益不可用</span></div>
      <Warning>这是已检查过的历史窗口。固定20股存在样本偏差，不能代替全部前窗证券；源截止晚于决策窗时，额外日期只供执行尾部核对。不可用收益保留为“不可用”；未成交或持续 NO_CHANGE 不证明策略有效。</Warning>
      {leader.price_source_mode === 'mixed_source_research_tail_overlay' && <Warning>研究回放的执行尾部采用 BaoStock 独立来源补价，历史信号不变。补价范围限固定20股，停牌空量额仍保留为不可用，未扩大到全部前窗证券。</Warning>}
      <details className="offline-details" open><summary>龙头固定比较 · {cases.length} 个比较项全部保留</summary>
        <div className="table-scroll"><table className="compact" aria-label="龙头固定比较"><thead><tr><th>策略</th><th>共同样本</th><th>退出规则</th><th>状态</th><th>完整决策日</th><th>NO_CHANGE</th><th>净收益</th><th>最大回撤</th><th>模拟委托/成交</th><th>阻断退出</th><th>未退出证券</th><th>证据</th></tr></thead><tbody>{cases.map(row => <tr key={`${row.family}-${row.pool}-${row.exit_policy}`}>
          <td>{familyNames[row.family] || row.family}</td><td>{poolNames[row.pool] || row.pool}<small className="window-stale">{count(row.cohort_size)} 只</small></td><td>{policyNames[row.exit_policy] || row.exit_policy}</td><td>{caseNames[row.status] || row.status}</td><td>{count(row.decision_coverage.complete_batch_days)}/{count(row.decision_coverage.expected_window_sessions)}</td><td>{percent(row.decision_coverage.no_change_ratio, '—')}</td><td>{row.fills === 0 ? '未成交，不评收益' : row.fills == null ? '不可用' : percent(row.metrics?.net_return)}</td><td>{row.fills === 0 ? '未成交' : row.fills == null ? '不可用' : percent(row.metrics?.max_drawdown)}</td><td>{count(row.orders)}/{count(row.fills)}</td><td>{count(row.blocked_exit_count)}</td><td>{row.open_positions == null ? '—' : count(Object.keys(row.open_positions).length)}</td>
          <td className="window-case"><details className="window-case-source"><summary>来源/阻碍</summary><dl><dt>最后账户日</dt><dd>{row.last_successful_session || '无账户轨迹'}</dd><dt>缺批次仍持仓日</dt><dd>{count(row.decision_coverage.incomplete_batch_held_days)}</dd><dt>超5日仍持仓日</dt><dd>{count(row.decision_coverage.over_five_session_held_days)}</dd><dt>阻碍</dt><dd>{row.error || row.reason || '无额外阻碍；仍仅供研究'}</dd><dt>证据目录</dt><dd>{row.artifacts || '未提供'}</dd></dl></details></td>
        </tr>)}</tbody></table></div>
      </details><p className="subtle">原 V2 在完整决策中安排五日退出，完整空候选转现金目标；缺批次可能抑制旧退出，独立退出见上方对照。退出仍受 T+1、涨跌停、量能与缺行情约束。广度防守缺核验证据时不产生收益数字。</p>
    </>}
    <div className="offline-subsection"><h3>大盘事前预测核验</h3>
      {scoreboard?.status === 'missing' && <p className="subtle">尚无登记预测回执。</p>}
      {scoreboard && scoreboard.status !== 'missing' && <>
        <div className="window-summary"><span>闭市核验截至 <strong>{scoreboard.closed_as_of || '未知'}</strong></span><span>登记 <strong>{count(scoreboard.registrations)}</strong> 批</span><span>待核验 <strong>{pending}</strong> 个标的周期</span></div>
        <div className="table-scroll"><table className="compact" aria-label="预测周期核验摘要"><thead><tr><th>周期</th><th>登记标的</th><th>待核验</th><th>前瞻计分</th><th>独立观察日</th><th>预测准确率</th><th>多数类基线</th><th>不变基线</th></tr></thead><tbody>{Object.entries(scoreboard.summary_by_horizon).map(([horizon, summary]) => <tr key={horizon}><td>{horizon} 个交易日</td><td>{count(summary.registered_targets)}</td><td>{count(summary.status_counts.pending ?? 0)}</td><td>{count(summary.prospective_scored)}</td><td>{count(summary.unique_origin_days)}</td><td>{percent(summary.signal_accuracy, '未计分')}</td><td>{percent(summary.majority_accuracy, '未计分')}</td><td>{percent(summary.nochange_accuracy, '未计分')}</td></tr>)}</tbody></table></div>
        <details className="offline-details"><summary>预测明细 · {rows.length} 个标的周期，{pending} 个待核验</summary><div className="table-scroll"><table className="compact" aria-label="登记预测明细"><thead><tr><th>标的</th><th>观察日</th><th>登记时间</th><th>周期</th><th>目标交易日</th><th>预设方向</th><th>状态</th><th>实际涨跌幅</th><th>是否命中</th><th>前瞻计分</th></tr></thead><tbody>{rows.map(row => <tr key={`${row.registered_at}-${row.target_id}-${row.horizon}`}><td>{row.name}<small className="window-stale">{row.symbol}</small></td><td>{row.origin}</td><td>{row.registered_at}</td><td>{row.horizon} 日</td><td>{row.target_session}</td><td>{row.signal}</td><td title={row.reason}>{forecastNames[row.status] || row.status}</td><td>{percent(row.actual_return, row.status === 'pending' ? '待结果' : '不可用')}</td><td>{row.correct == null ? '待核验' : row.correct ? '命中' : '未命中'}</td><td>{row.included_in_prospective_metrics ? '已纳入' : '未纳入'}</td></tr>)}</tbody></table></div></details>
        <p className="subtle">待核验结果不是零收益。5日核验沿用冻结1日信号；标的和重叠周期不能视为独立市场日。事前核验没有改变策略或执行准入。</p>
      </>}
    </div>
    <details className="offline-details"><summary>只读回执路径与边界</summary><dl><dt>龙头回执</dt><dd>{leader?.source_file || '未读取'}</dd><dt>预测回执</dt><dd>{scoreboard?.source_file || '未读取'}</dd><dt>执行标志</dt><dd>execution_admitted=false · selection_enabled=false · strategy_validated=false</dd></dl></details>
  </section>;
}
