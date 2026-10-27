import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, ShieldCheck } from 'lucide-react';
import { api, post } from '../api';
import { ErrorMessage, Warning } from './Common';
import { fmt } from '../types';
import type { OfflineAccountRiskView, OfflineBasketScheduleView, OfflineBlotterView, OfflineBrokerObservationView, OfflineDecisionQueueView, OfflineExecutionAttributionView, OfflineExecutionEvidenceView, OfflineFunction, OfflineInstrumentMasterView, OfflineManualStatementView, OfflineObservationBundleImportResponse, OfflineObservationBundleReadiness, OfflineOpeningWindowStatusView, OfflineOperatorAction, OfflineOperatorCandidate, OfflineOperatorPackage, OfflinePaperLifecycleView, OfflinePaperPreviewLatestView, OfflinePaperPreviewReadinessView, OfflinePaperSelectionResponse, OfflinePaperSelectionView, OfflinePortfolioControlsView, OfflinePortfolioRiskView, OfflineReleaseControlsView, OfflineSelectedPlanView, OfflineSessionHandoffView, OfflineStrategyEvidenceView, OfflineThsJournalStatus, OfflineThsNativeObservationView, OfflineThsPrefillReadbackResponse, OfflineThsReferencePayload, OfflineThsSimulatedLedgerMutationResponse, OfflineThsSimulatedLedgerReadiness, OfflineThsLedgerImportPreview, OfflineQuoteObservationImportResponse } from '../tradingTypes';

const candidateNames: Record<string, string> = {
  alpha158_allseed_rank_mean: 'Alpha158 多种子排序均值',
  momentum_12_1_20: '12-1-20 中期动量',
  stock_equal_three_families: '股票三类因子等权',
  stock_factor_candidate_leave_out_templates: '股票因子模板留一组合',
};
const stateNames: Record<string, string> = {
  paper_comparison_ready: '纸面比较就绪',
  promotion_review_template_ready: '晋级审查模板',
  selected_plan_unified_view_ready: '选中方案统一视图',
  paper_plan_partial: '纸面方案未完整',
  paper_lifecycle_rehearsal_ready: '纸面生命周期演练',
  paper_fill_projection_ready: '纸面成交投影',
  paper_recovery_defined: '纸面恢复流程已定义',
  paper_account_sized_risk_ready: '纸面账户风控就绪',
  paper_manual_sequence: '纸面人工顺序',
  paper_controls_defined: '纸面释放控制已定义',
  paper_fill_aware_child_projection_ready: '纸面子单投影',
  paper_scenario_ready: '纸面情景分析',
  offline_prefill_payload_ready: '离线预填载荷',
  missing_for_open: '开盘缺少输入',
  paper_candidate_stress_ready: '纸面候选压力测试',
  paper_internal_reconciliation_ready: '纸面内部对账',
  paper_allocation_projection_ready: '纸面分配投影',
  paper_alerts_ready: '纸面告警',
  candidate_evidence_card_ready: '候选证据卡',
  submission_disabled: '自动提交关闭',
};
const useNames: Record<string, string> = { review_only: '仅复核', paper_only: '仅纸面', manual_recovery: '人工恢复', manual_sequence: '人工顺序', manual_review: '人工复核', after_account_and_quotes: '补齐账户与行情后', required_input: '必须输入', manual_ths_only: '仅同花顺人工', enabled: '已启用' };
const liveNames: Record<string, string> = { network_probe_attempted: '网络探测', account_observed: '账户观察', quotes_observed: '行情观察', client_interacted: '客户端交互', ledger_mutated: '账本变更', submission_attempted: '提交尝试', execution_admitted: '执行准入' };
const lifecycleNames: Record<string, string> = { paper_preflight_accepted: '账本预检接受', paper_preflight_rejected: '账本预检拒绝', synthetic_submission_assumed: '纸面假定提交', synthetic_full_fill_assumed: '纸面假定全成', settlement_pending: '待跨日结算' };
const recoveryNames: Record<string, string> = { before_prefill: '预填前', prefill_interrupted: '预填中断', submission_unknown: '提交状态未知', partial_fill: '部分成交', end_of_day: '收盘' };
const pct = (value: number | null | undefined) => value == null ? '--' : `${fmt(value * 100, 2)}%`;
const label = (mapping: Record<string, string>, value: string) => mapping[value] || value;
const emptyActivityNames: Record<string, string> = { orders: '委托', trades: '成交', cashflows: '流水' };
const explicitEmptyActivityLabel = (value: Record<string, boolean | undefined> | null | undefined) => {
  if (!value) return '未知';
  const names = Object.entries(value).filter(([, empty]) => empty).map(([kind]) => emptyActivityNames[kind] || kind);
  return names.length ? names.join('、') : '无';
};
const sideNames: Record<string, string> = { buy: '买入', sell: '卖出' };
const phaseNames: Record<string, string> = { sell_release_cash: '卖出释放现金', buy_deploy_cash: '买入部署资金' };
const routeNames: Record<string, string> = { route_available: '主板人工路由可用', unsupported_instrument_route: '当前路由不支持' };
const prefillNames: Record<string, string> = { reference_payload_only: '冻结参考载荷', not_prefillable_route_unavailable: '无可预填路由' };
const decisionNames: Record<string, string> = { hold_research_only_preflight_rejected: '研究留置：账本预检拒绝', hold_research_only_route_unavailable: '研究留置：路由不可用', hold_paper_only_missing_session_inputs: '纸面留置：缺少当日输入', manual_review_before_prefill: '预填前人工复核' };
const bundleStatusNames: Record<string, string> = { missing_template: '等待四类导出', templates_blank: '空白模板可刷新', validation_error: '导出校验失败', bundle_ready_for_reconciliation: '可人工导入核对' };
const bundleFileNames: Record<string, string> = { account: '账户观察 JSON', orders: '委托 CSV', trades: '成交 CSV', cashflows: '累计资金流水 CSV' };
const manualStatementNames: Record<string, string> = { core_observation_bundle: '账户/委托/成交/资金核心包', cashflows: '累计资金流水', movements: '非交易资金流水', restriction_releases: '开盘解禁明细', share_credits: '红股/股份到账' };
const valueText = (value: unknown) => value == null || value === '' ? '--' : String(value);
const evidenceNames: Record<string, string> = { opening_strategy_state: '开盘候选状态', factor_strategy_state: '因子策略状态' };
const dataUsabilityNames: Record<string, string> = { paper_review_stale_valuation: '纸面复核：估值有滞后', paper_review_marks_complete: '纸面复核：标记完整', paper_review_stale_valuation_unknown: '纸面复核：标记未知' };
const windowStabilityNames: Record<string, string> = { no_selected_case_metrics: '无候选窗口指标', insufficient_windows_for_stability: '窗口不足', positive_across_available_windows: '可用窗口均为正', negative_across_available_windows: '可用窗口均为负', mixed_window_results: '窗口结果混合' };
const selectionNames: Record<string, string> = { paper_candidate_explicitly_selected: '人工选定纸面候选', selection_required: '待人工选择', not_indexed: '未索引' };

function CandidateTable({ rows, onSelect, selected }: { rows: OfflineOperatorCandidate[]; onSelect: (strategyId: string) => void; selected: string }) {
  return <div className="table-scroll"><table className="compact"><thead><tr><th>候选策略</th><th>回放收益</th><th>最大回撤</th><th>60日正收益窗口</th><th>120日正收益窗口</th><th>50bp压力收益</th><th>跨窗口稳定性</th><th>独立样本</th><th>最差窗口收益</th><th>滞后估值日</th><th>数据可用性</th><th>纸面订单</th><th>状态</th><th>纸面动作</th></tr></thead><tbody>{rows.map(row => <tr key={row.strategy_id}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{pct(row.net_return)}</td><td>{pct(row.max_drawdown)}</td><td>{pct(row.robustness_60d_positive_window_ratio)}</td><td>{pct(row.robustness_120d_positive_window_ratio)}</td><td>{pct(row.robustness_extra_50bps_return)}</td><td>{windowStabilityNames[row.saved_ledger_window_stability_status || ''] || row.saved_ledger_window_stability_status || '--'} <small>{row.saved_ledger_positive_window_count == null || row.saved_ledger_window_count == null ? '' : `(${row.saved_ledger_positive_window_count}/${row.saved_ledger_window_count})`}</small></td><td>{row.independent_sample_positive_window_count == null || row.independent_sample_window_count == null ? '--' : `${row.independent_sample_positive_window_count}/${row.independent_sample_window_count} 正`}{row.independent_sample_calendar_continuity_verified === false ? <small> · 连续性未验证</small> : ''}{row.independent_sample_window_start && row.independent_sample_window_end ? <small> · {row.independent_sample_window_start} 至 {row.independent_sample_window_end}</small> : ''}</td><td>{pct(row.independent_sample_worst_return ?? row.saved_ledger_worst_return)}</td><td>{valueText(row.stale_valuation_days)}</td><td title={row.data_usability_reason || undefined}>{dataUsabilityNames[row.data_usability_status || ''] || row.data_usability_status || '--'}</td><td>{fmt(row.candidate_order_count ?? row.orders ?? 0, 0)}</td><td>{row.status === 'research_preview_not_ready_for_prefill' ? '研究预览，未预填' : row.status}</td><td><button className="text-button" disabled={Boolean(selected)} onClick={() => onSelect(row.strategy_id)}>{selected === row.strategy_id ? '已生成' : selected ? '已选择其他候选' : '生成纸面方案'}</button></td></tr>)}</tbody></table></div>;
}

function PaperPreviewLatestSection({ value }: { value: OfflinePaperPreviewLatestView }) {
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>最近纸面调仓预览</h3><p className="subtle">由固定研究回放生成的下一交易日候选，已进入操作员视图但不写入真实订单账本</p></div><span className="bundle-status">只读预览 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>{candidateNames[value.strategy_id || ''] || value.strategy_id || '未指定策略'}</strong><span>纸面日 {value.execution_day || '--'}</span><span>账户未绑定</span><span>同日行情未观察</span><span>不预填/不提交</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>候选排名</span><strong>{value.provisional_rank_count}</strong></div><div><span>目标持仓</span><strong>{value.provisional_target_count}</strong></div><div><span>差额订单</span><strong>{value.provisional_order_count}</strong></div><div><span>释放状态</span><strong className="metric-text">{value.release_state || value.status}</strong></div></div>
    <dl className="offline-life-details"><dt>研究截止</dt><dd>{value.source_cutoff || '--'}</dd><dt>回放纸面权益/现金</dt><dd>{value.terminal_equity_rmb == null ? '--' : `${value.terminal_equity_rmb} / ${value.cash_rmb ?? '--'} RMB`}</dd><dt>阻断</dt><dd>{value.release_blockers.join('；') || '--'}</dd><dt>产物</dt><dd className="mono">{value.artifact_root || value.source_file || '--'}</dd></dl>
    <p className="subtle">{value.next_action}</p>
    {value.why_no_order.length > 0 && <ul className="offline-not-attempted">{value.why_no_order.map(item => <li key={item}>{item}</li>)}</ul>}
  </section>;
}

function ThsNativeObservationSection({ value }: { value: OfflineThsNativeObservationView }) {
  const metrics = value.account_summary.metrics_raw || {};
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>同花顺后台只读观察</h3><p className="subtle">来自已打开客户端的静态账户摘要与持仓候选，作为联调证据展示</p></div><span className="bundle-status">候选观察 · 未入账</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>{value.status}</strong><span>{value.client_session?.identity_verified ? '客户端身份匹配' : '客户端身份需重检'}</span><span>当前页 {value.client_session?.window?.title || '--'}</span><span>观察时间 {value.account_summary.observed_at || '--'}</span><span>委托/成交未读</span><span>不作为下单依据</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>可用资金</span><strong>{valueText(metrics.cash_available)}</strong></div><div><span>股票市值</span><strong>{valueText(metrics.stock_market_value)}</strong></div><div><span>总资产</span><strong>{valueText(metrics.total_assets)}</strong></div><div><span>持仓候选</span><strong>{value.position_candidate.row_count}</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>证券</th><th>名称</th><th>余额</th><th>可卖</th><th>市价</th><th>市值</th><th>日盈亏</th></tr></thead><tbody>{value.position_candidate.rows.map(row => <tr key={String(row.symbol || row.name)}><td className="mono">{valueText(row.symbol)}</td><td>{valueText(row.name)}</td><td>{valueText(row.quantity)}</td><td>{valueText(row.sellable)}</td><td>{valueText(row.market_price)}</td><td>{valueText(row.market_value)}</td><td>{valueText(row.day_pnl)}</td></tr>)}</tbody></table></div>
    <p className="subtle">仍缺：{value.missing_evidence.join('；') || '无'}。{value.next_action}</p>
  </section>;
}

function PaperPreviewReadinessSection({ value, onImportQuote, quoteImporting, quoteImportStatus, onInitializeQuote, quoteInitializing }: { value: OfflinePaperPreviewReadinessView; onImportQuote: () => void; quoteImporting: boolean; quoteImportStatus: string; onInitializeQuote: () => void; quoteInitializing: boolean }) {
  const quoteInput = value.quote_input;
  const quotes = value.quote_observation;
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>统一交易就绪判断</h3><p className="subtle">把策略预览、账户观测和券商证据合并为唯一发布状态</p></div><span className="bundle-status">{value.status === 'blocked' ? '已阻断' : '需人工复核'}</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>当前不可提交</strong><span>预填：{value.prefill_admitted ? '允许' : '关闭'}</span><span>提交：{value.submitted ? '已提交' : '关闭'}</span><span>账本：{value.ledger_mutated ? '已变更' : '未变更'}</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>纸面预览</span><strong>{value.preview_available ? '已生成' : '缺失'}</strong></div><div><span>账户证据</span><strong>{value.account_observation_complete ? '完整' : value.account_summary_only ? '仅摘要' : value.account_evidence_available ? '有导出' : '缺失'}</strong></div><div><span>原始行情</span><strong>{quotes?.status === 'complete' ? `${quotes.price_count || 0} 条` : '缺失'}</strong></div><div><span>阻塞项</span><strong>{value.blockers.length}</strong></div><div><span>发布状态</span><strong className="metric-text">{value.status}</strong></div></div>
    <dl className="offline-life-details"><dt>结构化导出包</dt><dd>{value.structured_bundle?.status || '未导入'} · {value.structured_bundle?.account_alias || '--'} · {value.structured_bundle?.trading_date || '--'} · 账户 {value.structured_bundle?.row_counts?.account ?? '--'} 行 · 委托 {value.structured_bundle?.row_counts?.orders ?? '--'} 行 · 成交 {value.structured_bundle?.row_counts?.trades ?? '--'} 行 · 流水 {value.structured_bundle?.row_counts?.cashflows ?? '--'} 行</dd><dt>同花顺分层观测</dt><dd>摘要 {value.structured_account_summary_observed ? '已观测' : '缺失'} · 候选持仓 {value.structured_holdings_candidate_observed ? (value.structured_holdings_candidate_semantic_verified ? '已语义确认' : '仅候选') : '缺失'} · 订单 {value.structured_orders_observed ? '已观测' : '缺失'} · 成交 {value.structured_trades_observed ? '已观测' : '缺失'} · 资金流水 {value.structured_cashflows_observed ? '已观测' : '缺失'} · 明确空表 {explicitEmptyActivityLabel(value.structured_empty_activity_exports)} · 可对账 {value.structured_package_ready_for_ledger_reconciliation ? '是' : '否'}</dd><dt>原生凭据文件</dt><dd>账户 {value.structured_account_structured_observed ? '可验证' : '缺失'} · 委托 {value.structured_receipts?.orders ? '可验证' : '缺失'} · 成交 {value.structured_receipts?.trades ? '可验证' : '缺失'} · 流水 {value.structured_receipts?.cashflows ? '可验证' : '缺失'}</dd><dt>摘要观测时间</dt><dd>{value.structured_account_summary_observed_at || '--'}</dd><dt>行情文件投递</dt><dd>{quoteInput?.status || '缺失'} · {quoteInput?.quote_root || '--'} · {quoteInput?.price_count || 0} 条</dd><dt>行情覆盖</dt><dd>{quoteInput?.coverage_status || quotes?.coverage_status || '--'} · 缺少 {quoteInput?.missing_symbols?.join('、') || quotes?.missing_symbols?.join('、') || '无'}</dd><dt>原始人民币行情</dt><dd>{quotes?.status || '未导入'} · {quotes?.trading_date || '--'} · {quotes?.observed_at || '--'} · {quotes?.source || '--'}</dd><dt>交易范围</dt><dd>{value.paper_preview.execution_scope || '--'} · 范围外持仓暂缓 {value.paper_preview.scope_excluded_position_symbols?.join('、') || '无'} · 不生成对应订单</dd><dt>阻塞清单</dt><dd>{value.blockers.join('；') || '无'}</dd><dt>下一动作</dt><dd>{value.next_action}</dd></dl>
    {(quoteInput?.status === 'missing_template' || quoteInput?.status === 'template_ready') && <div className="trade-boundary"><button className="text-button" onClick={onInitializeQuote} disabled={quoteInitializing || quoteInput.status === 'template_ready'}>{quoteInput.status === 'template_ready' ? '行情模板已生成' : quoteInitializing ? '正在生成模板…' : '生成行情模板'}</button><span>{quoteInput.status === 'template_ready' ? '等待当日原始报价' : '只创建本地空模板'}</span><span>不填价格</span><span>不操作客户端</span></div>}
    {quoteInput?.status === 'ready_for_import' && <div className="trade-boundary"><button className="text-button" onClick={onImportQuote} disabled={quoteImporting}>{quoteImporting ? '正在导入…' : '导入本地行情观察'}</button><span>只写入 THS 观察日志</span><span>不预填</span><span>不提交</span></div>}
    {quoteImportStatus && <p className="subtle">行情导入：{quoteImportStatus}</p>}
  </section>;
}

function FunctionTable({ rows }: { rows: OfflineFunction[] }) {
  return <div className="table-scroll"><table className="compact"><thead><tr><th>机构参考</th><th>机构能力</th><th>本地状态</th><th>本地证据</th><th>明日用途</th><th>优先级</th><th>下一缺口</th><th>下一实现</th></tr></thead><tbody>{rows.map(row => <tr key={row.function_id}><td>{row.institutional_reference || '--'}{row.source_url && <a className="source-tag" href={row.source_url} target="_blank" rel="noreferrer">参考</a>}</td><td>{row.institutional_capability}</td><td>{label(stateNames, row.local_state)}</td><td className="mono">{row.artifact || '--'}</td><td>{label(useNames, row.tomorrow_use)}</td><td>{row.priority}</td><td>{row.next_gap}</td><td>{row.next_implementation || '--'}</td></tr>)}</tbody></table></div>;
}

function ActionTable({ rows }: { rows: OfflineOperatorAction[] }) {
  return <div className="table-scroll"><table className="compact offline-action-table"><thead><tr><th>优先级</th><th>功能</th><th>当前状态</th><th>动作</th><th>前置条件</th><th>阻断原因</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.function_id}-${index}`}><td>{row.priority}</td><td>{row.function_id}</td><td>{row.state === 'blocked' ? '阻断' : '可行动'}</td><td>{row.action}</td><td>{row.prerequisite}</td><td>{row.blockers || '--'}</td></tr>)}</tbody></table></div>;
}

function PortfolioControlsSection({ value }: { value: OfflinePortfolioControlsView }) {
  const total = value.allocation.total_rows[0] || {};
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>纸面组合情景与分配结算</h3><p className="subtle">把价格冲击、现金变化和 T+1 依赖集中到交易员可复核的控制面板</p></div><span className="bundle-status">只读投影 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>冻结参考</strong><span>不使用实时行情</span><span>不使用账户持仓/现金</span><span>不代表成交或结算确认</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>价格情景</span><strong>{value.scenario.row_count}</strong></div><div><span>投影订单</span><strong>{value.allocation.order_count}</strong></div><div><span>投影总名义金额</span><strong>{valueText(total.reference_notional_rmb)} RMB</strong></div><div><span>执行准入</span><strong className="metric-text">关闭</strong></div></div>
    <h4 className="offline-table-heading">价格冲击情景</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>情景</th><th>买入冲击</th><th>卖出冲击</th><th>总名义金额</th><th>估算费用/滑点</th><th>总参考影响</th><th>依据</th></tr></thead><tbody>{value.scenario.rows.map(row => <tr key={row.scenario_id}><td>{row.scenario_id || '--'}</td><td>{row.buy_price_shock_pct || '--'}%</td><td>{row.sell_price_shock_pct || '--'}%</td><td>{row.gross_notional_rmb || '--'}</td><td>{row.estimated_fees_slippage_rmb || '--'}</td><td>{row.total_reference_impact_rmb || '--'}</td><td>{row.basis || '--'}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>逐笔分配与 T+1 结算投影</summary><div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>方向</th><th>数量</th><th>参考价</th><th>现金变化</th><th>持仓变化</th><th>结算</th><th>T+1 依赖</th><th>预检</th></tr></thead><tbody>{value.allocation.rows.map(row => <tr key={`${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.qty || '--'}</td><td>{row.reference_price_rmb || '--'}</td><td>{row.projected_cash_delta_rmb || '--'}</td><td>{row.projected_position_delta_shares || '--'}</td><td>{row.settlement_state || '--'}</td><td>{row.t1_dependency || '--'}</td><td>{row.preflight_status || '--'}</td></tr>)}</tbody></table></div><p className="subtle">{value.allocation.basis}。结算行只是纸面推演，必须以后续账户、委托、成交和资金流水观察替换。</p></details>
  </section>;
}

function PortfolioRiskSection({ value }: { value: OfflinePortfolioRiskView }) {
  const shared = value.shared_account_scenario || {};
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>候选组合风险书与账户容量</h3><p className="subtle">对应机构组合风险与容量检查：逐策略比较承载能力，禁止把候选策略混进同一账本</p></div><span className="bundle-status">纸面风险书 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>互斥候选</strong><span>不使用实时行情</span><span>不使用真实账户现金</span><span>不自动选择策略</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>候选策略</span><strong>{value.candidate_count}</strong></div><div><span>配置现金</span><strong>{valueText(shared.configured_initial_cash_rmb)} RMB</strong></div><div><span>合并买入后现金</span><strong>{valueText(shared.cash_after_all_candidate_buys_rmb)} RMB</strong></div><div><span>合并状态</span><strong className="metric-text">{valueText(shared.capacity_status)}</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>买入现金占用</th><th>买入后现金比例</th><th>最大单笔占用</th><th>预检接受/拒绝</th><th>风险标记</th><th>下一动作</th></tr></thead><tbody>{value.rows.map(row => <tr key={String(row.strategy_id)}><td>{candidateNames[String(row.strategy_id)] || valueText(row.strategy_id)}</td><td>{valueText(row.paper_accepted_buy_gross_rmb)}</td><td>{pct(Number(row.buy_cash_utilization))}</td><td>{valueText(row.largest_order_cash_ratio)}</td><td>{valueText(row.paper_preflight_accepted)}/{valueText(row.paper_preflight_rejected)}</td><td>{valueText(row.risk_flags)}</td><td>{valueText(row.next_required_action)}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>风险书口径与未执行事项</summary><ul className="offline-not-attempted">{value.not_attempted.map(item => <li key={item}>{item}</li>)}</ul><p className="subtle">{value.basis}。</p></details>
  </section>;
}

function SelectedPaperSection({ value, capacity }: { value: NonNullable<OfflinePaperSelectionResponse['receipt']>; capacity: OfflinePaperSelectionView['capacity'] }) {
  const capacityRow = capacity?.row || {};
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>已生成的单策略纸面方案</h3><p className="subtle">这是明确人工选择后的独立纸面工作台，不改变候选比较包，也不代表晋级或真实委托</p></div><span className="bundle-status">纸面方案 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>{candidateNames[value.strategy_id] || value.strategy_id}</strong><span>未读账户</span><span>未读行情</span><span>未操作同花顺</span><span>未提交委托</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>纸面订单</span><strong>{value.order_count}</strong></div><div><span>账本预检通过</span><strong>{value.ledger_preflight_accepted}</strong></div><div><span>账本预检拒绝</span><strong>{value.ledger_preflight_rejected}</strong></div><div><span>执行准入</span><strong className="metric-text">关闭</strong></div></div>
    <p className="subtle mono">工作台：{value.trader_desk_snapshot}</p>
    {capacity && <><h4 className="offline-table-heading">选中策略的纸面容量行</h4><div className="table-scroll"><table className="compact"><thead><tr><th>买入现金占用</th><th>买入后现金比例</th><th>最大单笔占用</th><th>风险标记</th><th>下一动作</th></tr></thead><tbody><tr><td>{valueText(capacityRow.paper_accepted_buy_gross_rmb)}</td><td>{capacityRow.buy_cash_utilization == null ? '--' : pct(Number(capacityRow.buy_cash_utilization))}</td><td>{valueText(capacityRow.largest_order_cash_ratio)}</td><td>{valueText(capacityRow.risk_flags)}</td><td>{valueText(capacityRow.next_required_action)}</td></tr></tbody></table></div><p className="subtle">容量行来源：{capacity.source_file}。这是冻结纸面配置，不是账户现金或实时风险限额。</p></>}
    <p className="subtle">{value.next_action}</p>
  </section>;
}

function SelectedPlanSection({ value }: { value: OfflineSelectedPlanView }) {
  if (!value.summary || !value.paper || !value.selected_strategy_id) return null;
  const summary = value.summary;
  const risk = value.risk_row || {};
  const prefill = value.prefill_reconciliation;
  const readbackTemplate = value.prefill_readback_template;
  const templateDiagnostics = (readbackTemplate as (NonNullable<OfflineSelectedPlanView['prefill_readback_template']> & {
    readback_diagnostics?: {
      status: string;
      applicable_row_count: number;
      pending_row_count: number;
      pending_field_count: number;
      route_unavailable_count: number;
      mismatch_count: number;
      readback_complete: boolean;
    };
  }) | null)?.readback_diagnostics;
  const readbackDiagnostics: {
    status: string;
    applicable_row_count: number;
    pending_row_count: number;
    pending_field_count: number;
    route_unavailable_count: number;
    mismatch_row_count?: number;
    mismatch_count?: number;
    readback_complete: boolean;
  } | null = value.prefill_readback_validation || templateDiagnostics || null;
  const comparison = value.saved_comparison;
  const decisionQueue = value.decision_queue;
  const researchEvidenceSection = value.research_evidence ? (
    <details className="offline-details" open>
      <summary>选中方案研究证据边界</summary>
      <div className="trade-boundary"><ShieldCheck size={16} /><strong>研究只读</strong><span>冻结快照：{summary.research_snapshot_status || '--'}</span><span>独立样本：{summary.research_independent_sample_status || '--'}</span><span>窗口稳定性：{summary.research_window_stability_status || '--'}</span><span>执行准入：关闭</span></div>
      <dl className="offline-life-details"><dt>StockDB 冻结快照</dt><dd className="mono">{value.source_files.stockdb_frozen_snapshot || '--'}</dd><dt>独立样本比较</dt><dd className="mono">{value.source_files.stockdb_independent_sample || '--'}</dd><dt>窗口稳定性</dt><dd className="mono">{value.source_files.stockdb_independent_window_stability || '--'}</dd></dl>
      {value.research_evidence.independent_sample?.ledger_results && <div className="table-scroll"><table className="compact" aria-label="独立样本策略比较"><thead><tr><th>策略</th><th>独立样本收益</th><th>最大回撤</th><th>订单</th><th>成交</th></tr></thead><tbody>{value.research_evidence.independent_sample.ledger_results.map(row => <tr key={row.candidate}><td>{row.candidate}</td><td>{pct(row.net_return)}</td><td>{pct(row.max_drawdown)}</td><td>{row.orders}</td><td>{row.fills}</td></tr>)}</tbody></table></div>}
      <p className="subtle">这些结果用于审阅和策略边界判断，不自动晋级策略、不替代账户观察，也不构成当前交易信号。</p>
    </details>
  ) : null;
  const nextSession = value.paper.next_session as {
    next_exchange_session?: string | null;
    next_exchange_session_status?: string | null;
    calendar_id?: string | null;
    calendar_source_url?: string | null;
    pending_settlement_order_count?: number;
    pending_t1_buys?: Record<string, unknown>[];
    sell_release_review?: Record<string, unknown>[];
    synthetic_cash_delta_rmb?: string | number | null;
    synthetic_cash_is_account_balance?: boolean;
    next_session_requirements?: string[];
  };
  const lifecycleBySequence = new Map(value.paper.lifecycle.rows.map(row => [String(row.sequence || ''), row]));
  const releaseBySequence = new Map(value.paper.release.rows.filter(row => row.row_type !== 'summary').map(row => [String(row.sequence || ''), row]));
  const lifecycleStates = Object.entries(summary.paper_state_counts).map(([state, count]) => `${state}:${count}`).join('；');
  const reconciliationStates = Object.entries(summary.paper_reconciliation_status_counts).map(([state, count]) => `${state}:${count}`).join('；');
  return <section className="offline-subsection">
    {researchEvidenceSection}
    <div className="research-heading"><div><h3>选中方案统一总览</h3><p className="subtle">将策略选择、目标订单、THS 预填字段、纸面生命周期和容量风险按同一策略 ID 对齐</p></div><span className="bundle-status">{candidateNames[value.selected_strategy_id] || value.selected_strategy_id} · 纸面</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>只读总览</strong><span>目标订单已生成</span><span>账户与实时行情未观察</span><span>同花顺未操作</span><span>提交准入关闭</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>目标订单</span><strong>{summary.target_order_count}</strong></div><div><span>THS 可预填</span><strong>{summary.ths_route_available_count}/{summary.ths_order_count}</strong></div><div><span>字段映射一致</span><strong>{summary.prefill_matched_count}/{summary.target_order_count}</strong></div><div><span>买入后纸面现金</span><strong>{valueText(summary.paper_cash_after_buys_rmb)}</strong></div></div>
    {comparison && <details className="offline-details" open><summary>同账本历史比较（只读）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>{comparison.start || '--'} 至 {comparison.end || '--'}</strong><span>{comparison.comparable_cases}/{comparison.requested_cases} 案例可比</span><span>成本配置一致：{comparison.same_cost_settings ? '是' : '否'}</span><span>不构成晋级或当前信号</span></div>{comparison.selected_case ? <div className="metrics trade-metrics offline-life-metrics"><div><span>净收益</span><strong>{pct(comparison.selected_case.net_return)}</strong></div><div><span>最大回撤</span><strong>{pct(comparison.selected_case.max_drawdown)}</strong></div><div><span>Sharpe</span><strong>{valueText(comparison.selected_case.sharpe_zero_rf_252)}</strong></div><div><span>滞后估值日</span><strong>{valueText(comparison.selected_case.stale_valuation_days)}</strong></div></div> : <p className="subtle">比较文件中没有与选中策略 ID 匹配的案例。</p>}{comparison.cases.length > 0 && <div className="table-scroll"><table className="compact" aria-label="同账本策略比较"><thead><tr><th>方案</th><th>研究族</th><th>收益</th><th>回撤</th><th>Sharpe</th><th>费用</th><th>滞后估值日</th><th>状态</th></tr></thead><tbody>{comparison.cases.map(row => <tr key={row.case_id}><td><strong>{row.label || row.signal}</strong><small className="mono">{row.signal}</small></td><td>{row.family}</td><td>{pct(row.net_return)}</td><td>{pct(row.max_drawdown)}</td><td>{valueText(row.sharpe_zero_rf_252)}</td><td>{row.fees == null ? '--' : `${fmt(row.fees, 2)} 元`}</td><td>{valueText(row.stale_valuation_days)}</td><td>{row.execution_admitted ? '执行准入' : row.descriptive_nondominated ? '仅描述性前沿' : '研究待审'}</td></tr>)}</tbody></table></div>}<p className="subtle">来源：<span className="mono">{comparison.source_artifact}</span>。该结果沿用已保存统一账本日标记，仍不是独立留出集、账户观察或实时行情。</p></details>}
    {decisionQueue && <details className="offline-details" open><summary>选中方案决策队列与恢复状态</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>{decisionQueue.decision || 'paper_hold'}</strong><span>下一动作：{decisionQueue.next_operator_action || '--'}</span><span>自动重试：{decisionQueue.recovery_retry_permitted ? '允许' : '禁止'}</span><span>待补证据 {decisionQueue.recovery_pending_evidence_count || 0} 类</span><span>执行准入：关闭</span></div><dl className="offline-life-details"><dt>暂缓原因</dt><dd>{decisionQueue.reason || '--'}</dd><dt>恢复交接</dt><dd>{decisionQueue.recovery_handoff_status || '--'} · 未决场景 {decisionQueue.recovery_unresolved_scenario_count}</dd><dt>待补证据类型</dt><dd>{decisionQueue.recovery_pending_evidence?.join('、') || '无'}</dd><dt>候选合并</dt><dd>{decisionQueue.portfolio_merge_status || '--'}</dd><dt>队列来源</dt><dd className="mono">{decisionQueue.source_artifact}</dd><dt>恢复交接来源</dt><dd className="mono">{decisionQueue.recovery_handoff_source || '--'}</dd></dl>{decisionQueue.recovery_scenarios.length > 0 && <div className="table-scroll"><table className="compact"><thead><tr><th>场景</th><th>状态</th><th>恢复动作</th><th>必须观察</th><th>人工指令</th></tr></thead><tbody>{decisionQueue.recovery_scenarios.map(row => <tr key={row.scenario_id || row.recovery_action}><td>{row.scenario_id || '--'}</td><td>{row.lifecycle_status || '--'}</td><td>{row.recovery_action || '--'}</td><td>{row.required_observation || 'none'}</td><td>{row.operator_instruction || '--'}</td></tr>)}</tbody></table></div>}{(decisionQueue.recovery_evidence_checks?.length || 0) > 0 && <div className="table-scroll"><table className="compact"><thead><tr><th>证据检查</th><th>状态</th><th>所需证据</th><th>尚缺证据</th></tr></thead><tbody>{decisionQueue.recovery_evidence_checks?.map(row => <tr key={`evidence-${row.scenario_id}`}><td>{row.scenario_id || '--'}</td><td>{row.status || '--'}</td><td>{row.required_evidence.join('、') || '无'}</td><td>{row.pending_evidence.join('、') || '无'}</td></tr>)}</tbody></table></div>}</details>}
    <details className="offline-details" open><summary>跨交易日结转与 T+1 状态</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>{nextSession.next_exchange_session || '下一交易日未锁定'}</strong><span>{nextSession.next_exchange_session_status || '--'}</span><span>日历 {nextSession.calendar_id || '未固定'}</span><span>待结算 {nextSession.pending_settlement_order_count || 0} 笔</span><span>纸面现金差额不等于账户现金</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>T+1 待解锁买入</span><strong>{nextSession.pending_t1_buys?.length || 0}</strong></div><div><span>卖出待复核</span><strong>{nextSession.sell_release_review?.length || 0}</strong></div><div><span>纸面现金差额</span><strong>{valueText(nextSession.synthetic_cash_delta_rmb)}</strong></div><div><span>账户现金</span><strong className="metric-text">未观察</strong></div></div>{(nextSession.sell_release_review?.length || 0) > 0 && <div className="table-scroll"><table className="compact"><thead><tr><th>证券</th><th>卖出数量</th><th>状态</th><th>必须证据</th></tr></thead><tbody>{nextSession.sell_release_review?.map(row => <tr key={String(row.symbol)}><td className="mono">{valueText(row.symbol)}</td><td>{valueText(row.sell_qty)}</td><td>{valueText(row.state)}</td><td>{valueText(row.required_evidence)}</td></tr>)}</tbody></table></div>}<ul className="offline-not-attempted">{(nextSession.next_session_requirements || []).map(item => <li key={item}>{item}</li>)}</ul><p className="subtle">下一交易日仅来自固定日历；账户持仓、可卖数量和券商成交回报仍需实际观察后，才能释放或重试订单。</p></details>
    <div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>方向</th><th>订单数量</th><th>目标数量</th><th>目标状态</th><th>纸面路由</th><th>纸面预检</th><th>生命周期</th><th>释放决策</th></tr></thead><tbody>{value.target_orders.map(row => { const lifecycle = lifecycleBySequence.get(String(row.sequence || '')); const release = releaseBySequence.get(String(row.sequence || '')); return <tr key={`${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.order_qty || '--'}</td><td>{row.target_qty || '--'}</td><td>{row.status || '--'}</td><td>{row.route_status || '--'}</td><td>{row.preflight_status || '--'}</td><td>{lifecycle?.state || '--'}</td><td title={release?.reason || undefined}>{release?.paper_release_decision || '--'}</td></tr>; })}</tbody></table></div>
    <details className="offline-details"><summary>统一状态、预填一致性与容量证据</summary><dl className="offline-life-details"><dt>目标状态</dt><dd>{Object.entries(summary.target_status_counts).map(([state, count]) => `${state}:${count}`).join('；') || '--'}</dd><dt>纸面生命周期</dt><dd>{lifecycleStates || '--'}</dd><dt>纸面对账</dt><dd>{reconciliationStates || '--'}</dd><dt>释放决策</dt><dd>{Object.entries(summary.paper_release_decision_counts).map(([state, count]) => `${state}:${count}`).join('；') || '--'}</dd><dt>预填字段核对</dt><dd>{prefill?.status || '--'} · {prefill?.matched_count || 0}/{prefill?.target_order_count || 0}，路由不可用 {prefill?.route_unavailable_count || 0} 行</dd><dt>回读模板</dt><dd>{readbackTemplate?.status || '未生成'} · {readbackDiagnostics?.applicable_row_count || readbackTemplate?.rows.filter(row => row.readback_applicable).length || 0} 行待人工回读 · 待补字段 {readbackDiagnostics?.pending_field_count || '--'}</dd><dt>回读诊断</dt><dd>{readbackDiagnostics ? `${readbackDiagnostics.status} · 不可路由 ${readbackDiagnostics.route_unavailable_count} 行 · 不一致 ${readbackDiagnostics.mismatch_row_count ?? readbackDiagnostics.mismatch_count ?? 0}` : '模板未提供诊断'}</dd><dt>风险标记</dt><dd>{valueText(summary.risk_flags || risk.risk_flags)}</dd><dt>风险书来源</dt><dd className="mono">{value.source_files.portfolio_risk || '--'}</dd><dt>模板来源</dt><dd className="mono">{value.source_files.ths_prefill_readback_template || '--'}</dd><dt>批量校验来源</dt><dd className="mono">{value.source_files.ths_prefill_readback_validation || '--'}</dd><dt>同账本比较来源</dt><dd className="mono">{value.source_files.saved_ledger_comparison || '--'}</dd></dl>{prefill && <div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>状态</th><th>路由</th><th>预填状态</th><th>差异</th></tr></thead><tbody>{prefill.rows.map(row => <tr key={`prefill-${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{row.status}</td><td>{row.route_status || '--'}</td><td>{row.prefill_status || '--'}</td><td>{row.mismatches.join('；') || '--'}</td></tr>)}</tbody></table></div>}<p className="subtle">容量行是冻结纸面配置：买入后现金 {valueText(risk.paper_cash_after_buys_rmb || summary.paper_cash_after_buys_rmb)}，不是账户现金。预填一致性也不等于真实客户端表单回读；模板中的 readback 字段保持空白，直到获得授权的客户端观察。</p></details>
    <p className="subtle">{value.next_action}</p>
  </section>;
}

function ReleaseControlsSection({ value }: { value: OfflineReleaseControlsView }) {
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>释放控制与异常告警</h3><p className="subtle">把纸面阻断、人工接管和释放规则集中展示；实时触发与自动提交仍关闭</p></div><span className="bundle-status">{value.alerts.open_count} 条未关闭告警</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>人工边界</strong><span>告警只来自冻结纸面包</span><span>不触发客户端动作</span><span>不允许自动重试</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>告警总数</span><strong>{value.alerts.row_count}</strong></div><div><span>P0 告警</span><strong>{value.alerts.severity_counts.P0 || 0}</strong></div><div><span>释放控制</span><strong>{value.controls.row_count}</strong></div><div><span>实时告警</span><strong className="metric-text">关闭</strong></div></div>
    <details className="offline-details" open><summary>未关闭纸面告警</summary><div className="table-scroll"><table className="compact"><thead><tr><th>级别</th><th>状态</th><th>来源</th><th>告警</th><th>人工动作</th><th>实时动作</th></tr></thead><tbody>{value.alerts.rows.map(row => <tr key={row.alert_id}><td>{row.severity || '--'}</td><td>{row.state || '--'}</td><td>{row.source || '--'}</td><td>{row.message || row.code || '--'}</td><td>{row.operator_action || '--'}</td><td>{row.live_alert || 'disabled'}</td></tr>)}</tbody></table></div></details>
    <details className="offline-details"><summary>释放控制规则</summary><div className="table-scroll"><table className="compact"><thead><tr><th>控制</th><th>机构对应功能</th><th>状态</th><th>触发</th><th>参数</th><th>实时动作</th></tr></thead><tbody>{value.controls.rows.map(row => <tr key={row.control_id}><td>{row.control_id || '--'}</td><td>{row.institutional_analogue || '--'}</td><td>{row.state || '--'}</td><td>{row.trigger || '--'}</td><td>{row.parameter || '--'}</td><td>{row.live_action || 'disabled'}</td></tr>)}</tbody></table></div></details>
  </section>;
}

function BasketScheduleSection({ value }: { value: OfflineBasketScheduleView }) {
  const manualRelease = value.schedule.manual_release;
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>篮子执行顺序与人工批次计划</h3><p className="subtle">把机构常用的卖出释放现金、买入部署资金和逐笔顺序落成可复核纸面批次</p></div><span className="bundle-status">顺序冻结 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>人工序列</strong><span>仅显示冻结计划</span><span>不读取实时行情或账户</span><span>不执行预填、提交或重试</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>计划订单</span><strong>{value.schedule.row_count}</strong></div><div><span>卖出/买入</span><strong>{value.schedule.side_counts.sell || 0}/{value.schedule.side_counts.buy || 0}</strong></div><div><span>批次顺序</span><strong>{value.schedule.sell_before_buy && value.schedule.sequence_monotonic ? '卖出后买入' : '需人工复核'}</strong></div><div><span>实时提交</span><strong className="metric-text">{value.schedule.live_submission_disabled_count}/{value.schedule.row_count} 关闭</strong></div></div>
    <h4 className="offline-table-heading">批次摘要</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>阶段</th><th>订单数</th><th>方向</th><th>说明</th></tr></thead><tbody>{value.schedule.phase_sequence.map(phase => <tr key={phase}><td>{phaseNames[phase] || phase}</td><td>{value.schedule.phase_counts[phase] || 0}</td><td>{sideNames[phase === 'sell_release_cash' ? 'sell' : phase === 'buy_deploy_cash' ? 'buy' : 'unknown'] || '--'}</td><td>{phase === 'sell_release_cash' ? '先释放可用资金，再进入买入部署' : phase === 'buy_deploy_cash' ? '现金释放后人工复核买入' : '未知阶段，禁止自动推进'}</td></tr>)}</tbody></table></div>
    {manualRelease && <details className="offline-details" open><summary>候选策略人工释放索引（{manualRelease.candidate_count} 个）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>卖出优先索引</strong><span>顺序确认 {manualRelease.ordering_verified_count}/{manualRelease.candidate_count}</span><span>路由可用 {manualRelease.route_available_count}</span><span>路由阻断 {manualRelease.route_unavailable_count}</span><span>自动选优关闭</span></div><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>卖出复核</th><th>买入释放</th><th>预检阻断</th><th>路由阻断</th><th>顺序</th><th>准入</th></tr></thead><tbody>{manualRelease.rows.map(row => <tr key={String(row.strategy_id)}><td>{candidateNames[String(row.strategy_id)] || String(row.strategy_id || '--')}</td><td>{row.sell_review_count ?? 0}</td><td>{row.buy_after_sell_review_count ?? 0}</td><td>{row.blocked_preflight_count ?? 0}</td><td>{row.blocked_route_count ?? 0}</td><td>{row.ordering_verified ? '已确认' : '需复核'}</td><td>关闭</td></tr>)}</tbody></table></div><p className="subtle">{manualRelease.basis}。</p></details>}
    <details className="offline-details" open><summary>逐笔人工执行顺序（{value.schedule.row_count} 行）</summary><div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>释放序号</th><th>阶段</th><th>策略</th><th>证券</th><th>方向</th><th>数量</th><th>参考价</th><th>状态</th><th>所需观察</th><th>阻断原因</th></tr></thead><tbody>{value.schedule.rows.map(row => <tr key={`${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td>{row.release_sequence || '--'}</td><td>{phaseNames[row.phase] || row.phase || '--'}</td><td>{candidateNames[row.strategy_id] || row.strategy_id || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.qty || '--'}</td><td>{row.reference_price_rmb || '--'}</td><td>{row.status || '--'}</td><td>{row.required_observation || row.release_gate || '--'}</td><td>{row.release_blocker_reason || row.operator_note || '--'}</td></tr>)}</tbody></table></div><p className="subtle">{value.schedule.basis}。</p></details>
  </section>;
}

function BlotterSection({ value }: { value: OfflineBlotterView }) {
  const accepted = value.blotter.preflight_status_counts.accepted || 0;
  const rejected = value.blotter.preflight_status_counts.rejected || 0;
  const staged = value.blotter.lifecycle_counts.paper_staged_only || 0;
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>中央订单台与路由状态</h3><p className="subtle">对应机构 EMS 的订单总览：从预检、路由到纸面生命周期集中查看</p></div><span className="bundle-status">纸面订单台 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>只读订单台</strong><span>不发出订单</span><span>不读取券商回报</span><span>不修改统一账本</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>订单行</span><strong>{value.blotter.row_count}</strong></div><div><span>预检接受/拒绝</span><strong>{accepted}/{rejected}</strong></div><div><span>路由可用</span><strong>{value.blotter.route_available_count}</strong></div><div><span>纸面暂存</span><strong>{staged}</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>策略</th><th>证券</th><th>方向</th><th>数量</th><th>参考价</th><th>预检</th><th>路由</th><th>生命周期</th><th>原因</th></tr></thead><tbody>{value.blotter.rows.map(row => <tr key={`${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td>{candidateNames[row.strategy_id] || row.strategy_id || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.qty || '--'}</td><td>{row.limit_price_rmb || '--'}</td><td>{row.preflight_status || '--'}</td><td>{row.route_status || '--'}</td><td>{row.lifecycle || '--'}</td><td>{row.route_reason || row.preflight_reason || '--'}</td></tr>)}</tbody></table></div><p className="subtle">{value.blotter.basis}。</p>
  </section>;
}

function ExecutionAttributionSection({ value }: { value: OfflineExecutionAttributionView }) {
  const stress50 = value.execution_cost_stress.filter(row => Number(row.additional_slippage_bps) === 50);
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>母单—子单执行归因与压力</h3><p className="subtle">对应机构 EMS/TCA 的执行后复盘：把纸面拆单、费用和未完成压力集中到一个视图</p></div><span className="bundle-status">纸面归因 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>合成成交</strong><span>只使用冻结参考价</span><span>不等于券商成交回报</span><span>不产生补单或重试指令</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>母单/子单</span><strong>{value.parent_count}/{value.child_count}</strong></div><div><span>完整/剩余母单</span><strong>{value.completed_parent_count}/{value.residual_parent_count}</strong></div><div><span>基准全成本</span><strong>{value.tca_metrics.estimated_all_in_cost_rmb || '--'} RMB</strong></div><div><span>执行准入</span><strong className="metric-text">关闭</strong></div></div>
    <h4 className="offline-table-heading">部分执行压力</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>情景</th><th>使用子单</th><th>假定成交数量</th><th>剩余母单</th><th>买入现金</th><th>卖出释放</th><th>剩余名义金额</th></tr></thead><tbody>{value.child_pressure_scenarios.map(row => <tr key={String(row.scenario_id)}><td>{row.scenario_id || '--'}</td><td>{row.child_rows_used || '--'}</td><td>{row.assumed_filled_qty || '--'}</td><td>{row.residual_parent_count || 0}</td><td>{row.buy_cash_required_rmb || '--'}</td><td>{row.sell_cash_release_rmb || '--'}</td><td>{row.residual_notional_rmb || '--'}</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">策略 50bp 成本敏感度</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>合成成交名义金额</th><th>基准费用</th><th>增量滑点</th><th>全执行成本</th></tr></thead><tbody>{stress50.map(row => <tr key={String(row.stress_id)}><td>{candidateNames[String(row.strategy_id)] || row.strategy_id || '--'}</td><td>{row.synthetic_filled_notional_rmb || '--'}</td><td>{row.baseline_paper_fees_rmb || '--'}</td><td>{row.incremental_slippage_cost_rmb || '--'}</td><td>{row.estimated_total_execution_cost_rmb || '--'}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>母单归因明细（{value.parents.length} 行）</summary><div className="table-scroll"><table className="compact"><thead><tr><th>母单</th><th>策略</th><th>证券</th><th>方向</th><th>子单数</th><th>计划数量</th><th>合成成交</th><th>剩余名义金额</th><th>状态</th></tr></thead><tbody>{value.parents.map(row => <tr key={`${row.parent_sequence}-${row.symbol}`}><td>{row.parent_sequence || '--'}</td><td>{candidateNames[String(row.strategy_id)] || row.strategy_id || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[String(row.side)] || row.side || '--'}</td><td>{row.child_count || '--'}</td><td>{row.planned_qty || '--'}</td><td>{row.synthetic_fill_qty || '--'}</td><td>{row.synthetic_remaining_notional_rmb || '--'}</td><td>{row.child_execution_status || '--'}</td></tr>)}</tbody></table></div></details>
    <p className="subtle">{value.basis}。</p>
  </section>;
}

function SessionHandoffSection({ value }: { value: OfflineSessionHandoffView }) {
  const reconciliationAttention = (value.paper_reconciliation.status_counts.unobserved || 0) + (value.target_reconciliation.status_counts.unobserved || 0) + (value.target_reconciliation.status_counts.review || 0);
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>会话交接、恢复与纸面对账</h3><p className="subtle">对应机构日终控制与异常接管：把恢复动作、T+1 结转和目标—订单一致性放在同一处</p></div><span className="bundle-status">交接草案 · 准入关闭</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>人工接管</strong><span>不自动重试</span><span>不关闭未确认委托</span><span>不推进下一会话释放</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>恢复阶段</span><strong>{value.recovery.row_count}</strong></div><div><span>纸面对账</span><strong>{value.paper_reconciliation.row_count}</strong></div><div><span>对账关注</span><strong>{reconciliationAttention}</strong></div><div><span>下一会话结转</span><strong>{value.next_session.carry_row_count} 行</strong></div></div>
    <h4 className="offline-table-heading">恢复状态机</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>阶段</th><th>检测状态</th><th>人工动作</th><th>必须证据</th><th>下一状态</th></tr></thead><tbody>{value.recovery.rows.map(row => <tr key={row.stage}><td>{row.stage || '--'}</td><td>{row.detected_state || '--'}</td><td>{row.operator_action || '--'}</td><td>{row.required_evidence || '--'}</td><td>{row.next_state || '--'}</td></tr>)}</tbody></table></div>
    {value.recovery_reconciliation && <details className="offline-details"><summary>券商状态对账交接模板</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>{value.recovery_reconciliation.template_schema}</strong><span>账户/委托/成交均未观察</span><span>待核对 {value.recovery_reconciliation.rows.reduce((total, row) => total + row.pending_check_count, 0)} 项</span><span>待补证据 {value.recovery_reconciliation.rows.reduce((total, row) => total + (row.pending_evidence_count || 0), 0)} 类</span><span>自动重试关闭</span></div><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>场景</th><th>未决场景</th><th>待核对</th><th>待补证据</th><th>状态</th><th>重试</th></tr></thead><tbody>{value.recovery_reconciliation.rows.map(row => <tr key={row.strategy_id || row.source_file}><td>{candidateNames[row.strategy_id || ''] || row.strategy_id || '--'}</td><td>{row.scenario_count}</td><td>{row.unresolved_scenario_count}</td><td>{row.pending_check_count}</td><td>{row.pending_evidence?.join('、') || '--'}</td><td>{row.status}</td><td>{row.retry_permitted ? '允许' : '禁止'}</td></tr>)}</tbody></table></div><p className="subtle">这是等待授权观察的交接模板，不是券商回报；现金流、持仓可卖数量、T+1、同日行情和表单回读分别补齐后，才能完成对账。</p></details>}
    <h4 className="offline-table-heading">下一会话输入</h4>
    <div className="trade-boundary"><strong>{value.next_session.execution_day || '--'} · 纸面结转</strong><span>合成现金变化 {value.next_session.synthetic_cash_delta_rmb || '--'} RMB</span><span>账户/券商/成交均未观察</span></div>
    <ul className="offline-not-attempted">{value.next_session.requirements.map(item => <li key={item}>{item}</li>)}</ul>
    <details className="offline-details"><summary>纸面对账汇总与目标—订单对账</summary><div className="table-scroll"><table className="compact"><thead><tr><th>对账集合</th><th>行数</th><th>状态汇总</th><th>活动目标</th></tr></thead><tbody><tr><td>纸面成交/账本对账</td><td>{value.paper_reconciliation.row_count}</td><td>{Object.entries(value.paper_reconciliation.status_counts).map(([key, count]) => `${key}:${count}`).join('；')}</td><td>--</td></tr><tr><td>目标—订单对账</td><td>{value.target_reconciliation.row_count}</td><td>{Object.entries(value.target_reconciliation.status_counts).map(([key, count]) => `${key}:${count}`).join('；')}</td><td>{value.target_reconciliation.active_target_count}</td></tr></tbody></table></div><div className="table-scroll"><table className="compact"><thead><tr><th>类型</th><th>序号</th><th>证券</th><th>方向</th><th>数量</th><th>状态</th><th>原因</th></tr></thead><tbody>{value.target_reconciliation.rows.slice(0, 100).map((row, index) => <tr key={`${row.row_type}-${row.sequence}-${row.symbol}-${index}`}><td>{row.row_type || '--'}</td><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{row.side || '--'}</td><td>{row.order_qty || row.target_qty || '--'}</td><td>{row.status || '--'}</td><td>{row.reason || '--'}</td></tr>)}</tbody></table></div></details>
    <p className="subtle">{value.basis}。</p>
  </section>;
}

function LifecycleSection({ value }: { value: OfflinePaperLifecycleView }) {
  const stateRows = Object.entries(value.lifecycle.state_counts);
  const releaseRows = Object.entries(value.release.decision_counts);
  const releaseReasons = Object.entries(value.release.reason_counts).filter(([reason]) => reason).map(([reason, count]) => `${reason} (${count})`).join('；') || '--';
  return <section className="offline-subsection">
    <h3>订单生命周期与跨日恢复</h3>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>生命周期订单</span><strong>{value.lifecycle.order_count}</strong></div><div><span>纸面事件</span><strong>{value.lifecycle.event_count}</strong></div><div><span>待跨日结算</span><strong>{value.carry.pending_settlement_order_count}</strong></div><div><span>T+1 买入待释放</span><strong>{value.carry.pending_t1_buy_count}</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>生命周期状态</th><th>事件数</th><th>券商真实状态</th></tr></thead><tbody>{stateRows.map(([state, count]) => <tr key={state}><td>{lifecycleNames[state] || state}</td><td>{count}</td><td>0</td></tr>)}</tbody></table></div>
    <dl className="offline-life-details"><dt>下一交易日</dt><dd>{value.carry.next_exchange_session || '未固定：当前包未跨越可用交易日历'}</dd><dt>卖出可卖数量复核</dt><dd>{value.carry.sell_release_review_count} 条</dd><dt>内部对账</dt><dd>{value.ledger.internal_reconciliation_status}；券商对账：{value.ledger.live_reconciliation_status}</dd><dt>恢复重试</dt><dd>{value.recovery.retry_permitted ? '允许' : '禁止自动重试'}</dd></dl>
    <h4 className="offline-table-heading">逐项内部对账</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>检查项</th><th>范围</th><th>状态</th><th>预期</th><th>实际</th><th>差异</th><th>账户/券商观察</th></tr></thead><tbody>{value.reconciliation.checks.map(row => <tr key={`${row.check_id}-${row.scope}`}><td>{row.check_id || '--'}</td><td>{row.scope || '--'}</td><td>{row.status || '--'}</td><td>{row.expected || '--'}</td><td>{row.actual || '--'}</td><td>{row.difference || '--'}</td><td>{row.account_observed === 'true' || row.broker_observed === 'true' ? '已观察' : '未观察'}</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">目标订单映射与结算投影</h4>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>目标订单</span><strong>{value.target_reconciliation.order_count}</strong></div><div><span>映射通过</span><strong>{value.target_reconciliation.status_counts.pass || 0}</strong></div><div><span>有效目标</span><strong>{value.target_reconciliation.active_target_count}</strong></div><div><span>T+1 投影单</span><strong>{value.settlement_projection.order_count}</strong></div></div>
    <details className="offline-details" open><summary>逐行目标/订单差异</summary><div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>方向</th><th>订单数量</th><th>目标数量</th><th>有效目标</th><th>状态</th><th>持仓观察</th><th>原因</th></tr></thead><tbody>{value.target_reconciliation.rows.map(row => <tr key={`target-${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.order_qty || '--'}</td><td>{row.target_qty || '--'}</td><td>{row.active_target === 'true' ? '是' : '否'}</td><td>{row.status || '--'}</td><td>{row.account_holdings_observed === 'true' ? '已观察' : '未观察'}</td><td>{row.reason || '--'}</td></tr>)}</tbody></table></div></details>
    <details className="offline-details"><summary>逐行纸面结算/T+1 投影</summary><div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>方向</th><th>数量</th><th>参考价</th><th>现金变化</th><th>持仓变化</th><th>结算状态</th><th>T+1 依赖</th></tr></thead><tbody>{value.settlement_projection.rows.map(row => <tr key={`settlement-${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.qty || '--'}</td><td>{row.reference_price_rmb || '--'}</td><td>{row.projected_cash_delta_rmb || '--'}</td><td>{row.projected_position_delta_shares || '--'}</td><td>{row.settlement_state || '--'}</td><td>{row.t1_dependency || '--'}</td></tr>)}</tbody></table></div></details>
    <h4 className="offline-table-heading">异常处理队列</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>优先级</th><th>异常</th><th>数量</th><th>来源</th><th>人工动作</th><th>执行准入</th></tr></thead><tbody>{value.exception_queue.map(row => <tr key={row.exception}><td>{row.priority}</td><td>{row.exception}</td><td>{row.count}</td><td>{row.source}</td><td>{row.action}</td><td>关闭</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">纸面释放决策</h4>
    <details className="offline-details"><summary>逐单纸面释放决策</summary><div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>方向</th><th>数量</th><th>预检</th><th>目标映射</th><th>释放决策</th><th>原因</th><th>账户/行情观察</th></tr></thead><tbody>{value.release.rows.map(row => <tr key={`release-${row.sequence}-${row.symbol}`}><td>{row.sequence || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side || '--'}</td><td>{row.qty || '--'}</td><td>{row.preflight_status || '--'}</td><td>{row.target_reconciliation_status || '--'}</td><td>{row.paper_release_decision || '--'}</td><td>{row.reason || '--'}</td><td>{row.account_observed === 'true' || row.quotes_observed === 'true' ? '已观察' : '未观察'}</td></tr>)}</tbody></table></div></details>
    <div className="table-scroll"><table className="compact"><thead><tr><th>决策</th><th>数量</th><th>主要原因</th></tr></thead><tbody>{releaseRows.map(([decision, count]) => <tr key={decision}><td>{decision}</td><td>{count}</td><td>{releaseReasons}</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">恢复阶段</h4>
    <div className="table-scroll"><table className="compact offline-action-table"><thead><tr><th>恢复阶段</th><th>检测状态</th><th>人工动作</th><th>下一状态</th></tr></thead><tbody>{value.recovery.rows.map((row, index) => <tr key={`${row.stage}-${index}`}><td>{recoveryNames[row.stage] || row.stage}</td><td>{row.detected_state}</td><td>{row.operator_action}</td><td>{row.next_state}</td></tr>)}</tbody></table></div>
  </section>;
}

function DecisionQueueSection({ value }: { value: OfflineDecisionQueueView }) {
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>交易员决策队列</h3><p className="subtle">复用已生成的策略级纸面队列，统一显示留置原因和下一动作</p></div><span className="bundle-status">{value.strategy_count} 个候选均未获执行准入</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>决策边界</strong><span>候选策略不合并到账本</span><span>未观察账户与行情</span><span>自动恢复/重试关闭</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>候选策略</span><strong>{value.strategy_count}</strong></div><div><span>预检拒绝</span><strong>{value.decision_counts.hold_research_only_preflight_rejected || 0}</strong></div><div><span>路由不可用</span><strong>{value.decision_counts.hold_research_only_route_unavailable || 0}</strong></div><div><span>执行准入</span><strong className="metric-text">关闭</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>回放收益</th><th>回撤</th><th>预检接受/拒绝</th><th>路由可用/不可用</th><th>决策</th><th>下一动作</th></tr></thead><tbody>{value.rows.map(row => <tr key={row.strategy_id}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{pct(Number(row.comparison_return))}</td><td>{pct(Number(row.comparison_max_drawdown))}</td><td>{row.accepted_order_count}/{row.rejected_order_count}</td><td>{row.route_available_count}/{row.route_unavailable_count}</td><td>{decisionNames[row.decision] || row.decision}</td><td>{row.next_operator_action}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>每个候选的留置原因</summary><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>恢复未决场景</th><th>待补证据</th><th>组合合并</th><th>人工恢复</th><th>原因</th></tr></thead><tbody>{value.rows.map(row => <tr key={`reason-${row.strategy_id}`}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{row.recovery_unresolved_scenario_count}</td><td>{row.recovery_pending_evidence_count || 0}</td><td>{row.portfolio_merge_status}</td><td>{row.recovery_handoff_status}</td><td>{row.reason.split(';').join('；')}</td></tr>)}</tbody></table></div></details>
    <details className="offline-details"><summary>异常恢复交接（{value.recovery_handoffs.reduce((total, item) => total + item.unresolved_scenario_count, 0)} 个未决场景，{value.recovery_handoffs.reduce((total, item) => total + (item.pending_evidence_count || 0), 0)} 类待补证据）</summary><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>场景</th><th>生命周期</th><th>恢复动作</th><th>必须观察</th><th>操作指令</th></tr></thead><tbody>{value.recovery_handoffs.flatMap(item => item.rows.map(row => <tr key={`${item.strategy_id}-${row.scenario_id}`}><td>{candidateNames[item.strategy_id] || item.strategy_id}</td><td>{row.scenario_id}</td><td>{row.lifecycle_status}</td><td>{row.recovery_action}</td><td>{row.required_observation || '无'}</td><td>{row.operator_instruction}</td></tr>))}</tbody></table></div><p className="subtle">所有恢复交接均禁止自动重试；未决提交状态必须先导入账户委托/成交并完成对账。待补证据类型：{Array.from(new Set(value.recovery_handoffs.flatMap(item => item.pending_evidence || []))).join('、') || '无'}。</p></details>
  </section>;
}

function ThsReferenceSection({ value, onStrategyChange }: { value: OfflineThsReferencePayload; onStrategyChange: (strategyId: string) => void }) {
  return <section className="offline-subsection ths-reference-section"><div className="research-heading"><div><h3>THS 预填参考载荷</h3><p className="subtle">只读查看候选策略的冻结载荷，不改变纸面策略选择 · 载荷文件 {value.source_file}</p></div><label className="inline-field ths-reference-picker">查看策略<select aria-label="查看THS参考载荷策略" value={value.strategy_id || ''} onChange={event => onStrategyChange(event.target.value)}>{value.available_payloads.map(option => <option key={option.strategy_id || option.execution_day} value={option.strategy_id || ''}>{candidateNames[option.strategy_id || ''] || option.strategy_id || '未命名'} · {option.route_available_count} 条可预填</option>)}</select></label></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>只读参考</strong><span>不写入同花顺</span><span>不读取账户或实时行情</span><span>不提交、不撤单</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>订单行</span><strong>{value.order_count}</strong></div><div><span>可预填路由</span><strong>{value.route_available_count}</strong></div><div><span>不可预填路由</span><strong>{value.route_unavailable_count}</strong></div><div><span>提交准入</span><strong className="metric-text">关闭</strong></div></div>
    <Warning>价格基准：{value.price_basis}。这些价格不是当前人民币行情，不能直接用于委托。</Warning>
    <div className="table-scroll"><table className="compact"><thead><tr><th>序号</th><th>证券</th><th>方向</th><th>数量</th><th>冻结参考价</th><th>路由</th><th>预填状态</th></tr></thead><tbody>{value.rows.map(row => <tr key={`${row.sequence}-${row.symbol}`}><td>{row.sequence}</td><td className="mono">{row.symbol || '--'}</td><td>{sideNames[row.side] || row.side}</td><td>{row.qty || '--'}</td><td>{row.price_rmb || '--'}</td><td>{routeNames[row.route_status] || row.route_status || '--'}</td><td>{prefillNames[row.prefill_status] || row.prefill_status || '--'}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>字段映射与未执行事项</summary><div className="table-scroll"><table className="compact"><thead><tr><th>证券</th><th>代码字段</th><th>价格字段</th><th>数量字段</th><th>路由原因</th></tr></thead><tbody>{value.rows.map(row => <tr key={`mapping-${row.sequence}-${row.symbol}`}><td className="mono">{row.symbol || '--'}</td><td>{row.symbol_field || '--'}</td><td>{row.price_field || '--'}</td><td>{row.qty_field || '--'}</td><td>{row.route_reason || '--'}</td></tr>)}</tbody></table></div><ul className="offline-not-attempted">{value.not_attempted.map(item => <li key={item}>{item}</li>)}</ul></details>
    {value.available_payloads.length > 1 && <p className="subtle ths-reference-alternatives">当前离线包另有 {value.available_payloads.length - 1} 个候选策略载荷可供后续人工比较；策略选择仍需明确纸面选择，不自动切换。</p>}
  </section>;
}

function PrefillReadbackSection({ value, onVerify, result, busy }: { value: OfflineThsReferencePayload; onVerify: (input: { strategy_id: string; sequence: number; symbol: string; price: string; qty: string }) => void; result: OfflineThsPrefillReadbackResponse | null; busy: boolean }) {
  const rows = value.rows.filter(row => row.route_status === 'route_available');
  const [sequence, setSequence] = useState(String(rows[0]?.sequence || ''));
  const selectedRow = rows.find(row => String(row.sequence) === sequence) || rows[0];
  const [symbol, setSymbol] = useState('');
  const [price, setPrice] = useState('');
  const [qty, setQty] = useState('');
  useEffect(() => {
    if (!selectedRow) return;
    setSequence(String(selectedRow.sequence));
    setSymbol(selectedRow.symbol_field || String(selectedRow.symbol || '').split('.')[0]);
    setPrice(selectedRow.price_field || selectedRow.price_rmb || '');
    setQty(selectedRow.qty_field || selectedRow.qty || '');
  }, [selectedRow?.sequence, selectedRow?.symbol_field, selectedRow?.price_field, selectedRow?.qty_field]);
  if (!value.strategy_id || !rows.length) return <section className="offline-subsection"><h3>表单回读核对</h3><p className="subtle">当前载荷没有可核对的人工路由订单。</p></section>;
  return <section className="offline-subsection"><div className="research-heading"><div><h3>同花顺表单回读核对</h3><p className="subtle">录入客户端表单读回的三字段，系统只比较冻结载荷，不操作客户端、不提交订单。</p></div><span className="bundle-status">只读核对</span></div><form className="window-controls" onSubmit={event => { event.preventDefault(); onVerify({ strategy_id: value.strategy_id as string, sequence: Number(sequence), symbol, price, qty }); }}><label>订单序号<select aria-label="回读订单序号" value={sequence} onChange={event => setSequence(event.target.value)}>{rows.map(row => <option key={String(row.sequence)} value={String(row.sequence)}>{row.sequence} · {row.symbol}</option>)}</select></label><label>代码<input aria-label="回读代码" value={symbol} onChange={event => setSymbol(event.target.value)} required /></label><label>价格<input aria-label="回读价格" value={price} onChange={event => setPrice(event.target.value)} required /></label><label>数量<input aria-label="回读数量" value={qty} onChange={event => setQty(event.target.value)} required /></label><button type="submit" className="primary" disabled={busy}>{busy ? '核对中' : '核对回读'}</button></form>{result && <div className={`trade-boundary ${result.status === 'readback_verified' ? 'flag-on' : 'flag-off'}`}><ShieldCheck size={16} /><strong>{result.status === 'readback_verified' ? '回读一致' : result.status === 'route_unavailable' ? '路由不可用' : '回读不一致'}</strong>{result.mismatches?.map(item => <span key={item.field}>{item.field}: 预期 {item.expected}，回读 {item.received}</span>)}<span>提交：否 · 账本变更：否</span></div>}</section>;
}

function ObservationBundleSection({ value, journalStatus, ledgerRuns, selectedPlanId, onPlanIdChange, onLedgerRunChange, onInitialize, onImport, onPreviewToLedger, onImportToLedger, ledgerPreview, importStatus, importing }: { value: OfflineObservationBundleReadiness; journalStatus: OfflineThsJournalStatus; ledgerRuns: OfflineThsSimulatedLedgerReadiness['runs']; selectedPlanId: string; onPlanIdChange: (planId: string) => void; onLedgerRunChange: () => void; onInitialize: () => void; onImport: (planId?: string) => void; onPreviewToLedger: (planId: string, runId: string) => void; onImportToLedger: (planId: string, runId: string) => void; ledgerPreview: OfflineThsLedgerImportPreview | null; importStatus: string; importing: boolean }) {
  const preview = value.preview;
  const observations = Object.entries(preview.observations || {});
  const linkage = preview.linkage || {};
  const orderSummaries = linkage.order_summaries || [];
  const boundRuns = ledgerRuns.filter(row => Boolean(row.account_alias));
  const [ledgerRunId, setLedgerRunId] = useState('');
  useEffect(() => {
    if (!boundRuns.some(row => row.run_id === ledgerRunId)) setLedgerRunId(boundRuns[0]?.run_id || '');
  }, [boundRuns.map(row => row.run_id).join('|'), ledgerRunId]);
  return <section className="offline-subsection observation-bundle-section">
    <div className="research-heading"><div><h3>同花顺导出包准备度</h3><p className="subtle">读取本地四类人工导出文件，不访问客户端，不写观察日志或统一账本</p></div><div><span className={`bundle-status bundle-status-${value.status}`}>{bundleStatusNames[value.status] || value.status}</span>{(value.status === 'missing_template' || value.status === 'templates_blank') && <button className="primary" disabled={importing} onClick={onInitialize}>{value.status === 'templates_blank' ? '刷新空白模板' : '生成空白模板'}</button>}<button className="primary" disabled={value.status !== 'bundle_ready_for_reconciliation' || importing} onClick={() => onImport(selectedPlanId || undefined)}>{importing ? '导入中' : '人工确认导入'}</button></div></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>只读校验</strong><span>导入需另行明确确认</span><span>不读取实时行情</span><span>不提交、不撤单</span></div>
    <div className="window-controls"><label>导入后刷新计划<select aria-label="导入后刷新计划" value={selectedPlanId} onChange={event => onPlanIdChange(event.target.value)}><option value="">只导入观察，不刷新计划</option>{journalStatus.plans.map(plan => <option key={plan.plan_id} value={plan.plan_id}>{plan.plan_id} · {plan.status} · {plan.account_alias}</option>)}</select></label><span className="subtle">已有计划 {journalStatus.plan_count} 个 · 暂停写入：{journalStatus.paused ? '是' : '否'}</span></div>
    <div className="window-controls"><label>同一 Ledger 运行<select aria-label="观察包回灌纸面 Ledger 运行" value={ledgerRunId} onChange={event => { setLedgerRunId(event.target.value); onLedgerRunChange(); }}><option value="">暂无已绑定运行</option>{boundRuns.map(row => <option key={row.run_id} value={row.run_id}>{row.name} · {row.account_alias}</option>)}</select></label><button className="text-button" disabled={value.status !== 'bundle_ready_for_reconciliation' || !selectedPlanId || !ledgerRunId || importing} onClick={() => onPreviewToLedger(selectedPlanId, ledgerRunId)}>预览 Ledger 差额</button><button className="primary" disabled={value.status !== 'bundle_ready_for_reconciliation' || !selectedPlanId || !ledgerRunId || !ledgerPreview || ledgerPreview.status !== 'ready_for_import' || importing} onClick={() => onImportToLedger(selectedPlanId, ledgerRunId)}>观察包导入并回灌 Ledger</button><span className="subtle">必须先完成当前计划/运行组合的差额预览；切换后需重新预览。</span></div>
    {ledgerPreview && <div className="trade-boundary"><ShieldCheck size={16} /><strong>预览：{ledgerPreview.status}</strong><span>将变更：{ledgerPreview.would_change ? '是' : '否'}</span><span>现金变化 {ledgerPreview.account_reconciliation?.cash_change_cents == null ? '--' : `${(ledgerPreview.account_reconciliation.cash_change_cents / 100).toFixed(2)} 元`}</span><span>费用 {ledgerPreview.account_reconciliation?.fees_cents == null ? '--' : `${(ledgerPreview.account_reconciliation.fees_cents / 100).toFixed(2)} 元`}</span><span>订单/成交 {ledgerPreview.changes?.order_count ?? '--'}/{ledgerPreview.changes?.fill_count ?? '--'}</span><span>账本未变更</span></div>}
    <div className="metrics trade-metrics offline-life-metrics"><div><span>文件齐全</span><strong>{Object.values(value.files).filter(file => file.present).length}/4</strong></div><div><span>观察视图</span><strong>{observations.length}</strong></div><div><span>关联错误</span><strong>{(preview.errors || []).length}</strong></div><div><span>账本变更</span><strong className="metric-text">否</strong></div></div>
    <Warning>{value.next_action}</Warning>
    {importStatus && <p className="subtle">{importStatus}</p>}
    {journalStatus.plans.length > 0 && <details className="offline-details"><summary>本地计划与最近对账状态</summary><div className="table-scroll"><table className="compact"><thead><tr><th>计划</th><th>账户别名</th><th>交易日</th><th>计划状态</th><th>对账结果</th><th>成交条数</th><th>账户对账</th><th>统一账本</th><th>异常原因</th><th>更新时间</th></tr></thead><tbody>{journalStatus.plans.map(plan => <tr key={`journal-${plan.plan_id}`}><td className="mono">{plan.plan_id}</td><td>{plan.account_alias}</td><td>{plan.trading_date || '--'}</td><td>{plan.status}</td><td>{plan.result_status || '--'}</td><td>{plan.execution_count}</td><td>{plan.account_reconciliation_status || '--'}</td><td>{plan.ledger_imported ? '已入账' : '未入账'}</td><td>{plan.result_reason || '--'}</td><td>{plan.updated_at}</td></tr>)}</tbody></table></div></details>}
    <p className="subtle mono offline-bundle-root">目录：{value.bundle_root}</p>
    <div className="table-scroll"><table className="compact"><thead><tr><th>导出类型</th><th>文件</th><th>状态</th><th>大小</th></tr></thead><tbody>{Object.entries(value.files).map(([kind, file]) => <tr key={kind}><td>{bundleFileNames[kind] || kind}</td><td className="mono">{file.path}</td><td>{file.present ? '已发现' : '缺失'}</td><td>{file.present ? `${file.bytes} B` : '--'}</td></tr>)}</tbody></table></div>
    {observations.length > 0 && <div className="table-scroll"><table className="compact"><thead><tr><th>观察视图</th><th>观察时间</th><th>行数</th></tr></thead><tbody>{observations.map(([kind, item]) => <tr key={kind}><td>{bundleFileNames[kind] || kind}</td><td>{item.observed_at}</td><td>{item.row_count}</td></tr>)}</tbody></table></div>}
    {orderSummaries.length > 0 && <details className="offline-details" open><summary>逐订单成交/资金对账摘要</summary><div className="table-scroll"><table className="compact"><thead><tr><th>订单</th><th>证券</th><th>方向</th><th>委托/成交</th><th>成交笔数</th><th>成交金额</th><th>资金流水</th><th>状态</th></tr></thead><tbody>{orderSummaries.map(row => <tr key={row.order_id}><td className="mono">{row.order_id}</td><td className="mono">{row.symbol}</td><td>{sideNames[row.side] || row.side}</td><td>{row.order_filled_qty}/{row.trade_filled_qty} · {row.order_qty}</td><td>{row.trade_count}</td><td>{(row.trade_gross_cents / 100).toFixed(2)}</td><td>{row.cashflow_present ? `${((row.cash_change_cents || 0) / 100).toFixed(2)} / fee ${((row.fees_cents || 0) / 100).toFixed(2)}` : '缺失'}</td><td>{row.status}{row.reasons.length > 0 ? ` · ${row.reasons.join('；')}` : ''}</td></tr>)}</tbody></table></div></details>}
    {(preview.errors || []).length > 0 && <ul className="offline-not-attempted">{(preview.errors || []).map(error => <li key={error}>{error}</li>)}</ul>}
    {Object.keys(linkage).length > 0 && <details className="offline-details"><summary>订单/成交/资金流水关联结果</summary><pre className="offline-json-preview">{JSON.stringify(linkage, null, 2)}</pre></details>}
  </section>;
}

function SimulatedLedgerSection({ value, onPrepare, onCreateRun, onBind, onImport, onReconcile, busy, status }: { value: OfflineThsSimulatedLedgerReadiness; onPrepare: (runId: string) => void; onCreateRun: (accountAlias: string) => void; onBind: (runId: string, accountAlias: string) => void; onImport: (runId: string, planId: string) => void; onReconcile: (runId: string, includeRestrictionReleases: boolean, includeShareCredits: boolean) => void; busy: boolean; status: string }) {
  const bindableRuns = value.runs.filter(row => row.bindable);
  const readyAccounts = value.accounts.filter(row => row.status === 'ready_for_explicit_binding');
  const boundRuns = value.runs.filter(row => row.account_alias);
  const [runId, setRunId] = useState('');
  const [accountAlias, setAccountAlias] = useState('');
  const [importRunId, setImportRunId] = useState('');
  const [planId, setPlanId] = useState('');
  const [includeRestrictionReleases, setIncludeRestrictionReleases] = useState(false);
  const [includeShareCredits, setIncludeShareCredits] = useState(false);
  useEffect(() => { setRunId(bindableRuns[0]?.run_id || ''); setAccountAlias(readyAccounts[0]?.account_alias || ''); setImportRunId(boundRuns[0]?.run_id || ''); setPlanId(value.plans[0]?.plan_id || ''); }, [value.status, value.runs.length, value.accounts.length, value.plans.length]);
  return <section className="offline-subsection">
    <div className="research-heading"><div><h3>模拟账户接入同一 Ledger</h3><p className="subtle">只接受同花顺人工导出观察；绑定和回灌均不访问客户端、不提交委托。</p></div><span className="bundle-status">{value.status}</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>模拟账户边界</strong><span>真实账户关闭</span><span>自动提交关闭</span><span>仅允许暂停的空白 paper run 绑定</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>可绑定 paper run</span><strong>{bindableRuns.length}</strong></div><div><span>可用账户观察</span><strong>{readyAccounts.length}</strong></div><div><span>待处理计划</span><strong>{value.plans.filter(row => !row.ledger_imported).length}</strong></div><div><span>账本范围</span><strong className="metric-text">统一纸面</strong></div></div>
    <Warning>{value.next_action}</Warning>
    {readyAccounts.map(account => <div className="window-controls" key={`create-${account.account_alias}`}><span className="subtle">已核对账户：{account.account_alias} · {account.trading_date || '--'}</span><button className="primary" disabled={busy} onClick={() => onCreateRun(account.account_alias)}>按账户观察创建 paper run</button></div>)}
    <div className="window-controls"><label>绑定 run<select aria-label="绑定纸面运行" value={runId} onChange={event => setRunId(event.target.value)}><option value="">暂无可绑定 run</option>{bindableRuns.map(row => <option key={row.run_id} value={row.run_id}>{row.name} · {row.run_id}</option>)}</select></label><label>账户别名<select aria-label="绑定模拟账户" value={accountAlias} onChange={event => setAccountAlias(event.target.value)}><option value="">暂无已核对账户</option>{readyAccounts.map(row => <option key={row.account_alias} value={row.account_alias}>{row.account_alias} · {row.trading_date || '--'}</option>)}</select></label><button className="primary" disabled={!runId || !accountAlias || busy} onClick={() => onBind(runId, accountAlias)}>显式绑定模拟账户</button></div>
    {value.runs.filter(row => row.mode === 'paper' && row.status === 'running' && row.issues.includes('运行必须先暂停')).map(row => <div className="window-controls" key={`prepare-${row.run_id}`}><span className="subtle">空白 paper run：{row.name} · 需要先暂停才能接收模拟账户开盘观察</span><button className="primary" disabled={busy} onClick={() => onPrepare(row.run_id)}>准备绑定（暂停空白 run）</button></div>)}
    <div className="window-controls"><label>已绑定 run<select aria-label="选择回灌运行" value={importRunId} onChange={event => setImportRunId(event.target.value)}><option value="">暂无已绑定 run</option>{boundRuns.map(row => <option key={row.run_id} value={row.run_id}>{row.name} · {row.account_alias}</option>)}</select></label><label>计划<select aria-label="选择回灌计划" value={planId} onChange={event => setPlanId(event.target.value)}><option value="">暂无计划</option>{value.plans.map(row => <option key={row.plan_id} value={row.plan_id}>{row.plan_id} · {row.status}</option>)}</select></label><button className="primary" disabled={!importRunId || !planId || busy} onClick={() => onImport(importRunId, planId)}>显式回灌已对账回执</button></div>
    <div className="window-controls"><label><input type="checkbox" checked={includeRestrictionReleases} onChange={event => setIncludeRestrictionReleases(event.target.checked)} />包含开盘解禁明细</label><label><input type="checkbox" checked={includeShareCredits} onChange={event => setIncludeShareCredits(event.target.checked)} />包含股份到账明细</label><button className="primary" disabled={!importRunId || busy} onClick={() => onReconcile(importRunId, includeRestrictionReleases, includeShareCredits)}>核对下一交易日并入账</button></div>
    {status && <p className="subtle">{status}</p>}
    {value.accounts.length > 0 && <details className="offline-details"><summary>账户观测完整度</summary><div className="table-scroll"><table className="compact"><thead><tr><th>账户别名</th><th>观察视图</th><th>交易日</th><th>状态</th><th>缺口/异常</th></tr></thead><tbody>{value.accounts.map(row => <tr key={row.account_alias}><td>{row.account_alias}</td><td>{row.observed_kinds.join(', ') || '--'}</td><td>{row.trading_date || '--'}</td><td>{row.status}</td><td>{row.issues.join('；') || '--'}</td></tr>)}</tbody></table></div></details>}
    <details className="offline-details"><summary>纸面运行绑定条件</summary><div className="table-scroll"><table className="compact"><thead><tr><th>运行</th><th>模式/状态</th><th>绑定账户</th><th>可绑定</th><th>原因</th></tr></thead><tbody>{value.runs.map(row => <tr key={row.run_id}><td className="mono">{row.run_id}</td><td>{row.mode}/{row.status}</td><td>{row.account_alias || '--'}</td><td>{row.bindable ? '是' : '否'}</td><td>{row.issues.join('；') || '--'}</td></tr>)}</tbody></table></div></details>
  </section>;
}

function ManualStatementSection({ value }: { value: OfflineManualStatementView }) {
  const readyCount = value.rows.filter(row => row.template_exists).length;
  return <section className="offline-subsection manual-statement-section">
    <div className="research-heading"><div><h3>券商人工报表导入契约</h3><p className="subtle">把同花顺导出的资金、限制和股份事件接入同一观察/对账入口</p></div><span className="bundle-status">{readyCount}/{value.rows.length} 个模板就绪</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>人工导出</strong><span>不访问客户端</span><span>不读取实时行情</span><span>不自动写账本</span><span>执行准入关闭</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>模板就绪</span><strong>{readyCount}/{value.rows.length}</strong></div><div><span>账户观察</span><strong>{value.account_observed ? '是' : '否'}</strong></div><div><span>账本变更</span><strong>{value.ledger_mutated ? '是' : '否'}</strong></div><div><span>执行准入</span><strong className="metric-text">关闭</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>报表</th><th>用途</th><th>模板</th><th>状态</th><th>后续动作</th></tr></thead><tbody>{value.rows.map(row => <tr key={row.kind}><td>{manualStatementNames[row.kind] || row.kind}</td><td>{row.basis}</td><td className="mono">{row.template}</td><td>{row.template_exists ? '已生成' : '缺失'}</td><td>{row.follow_up || '验证映射后导入'}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>导入命令（仅人工导出后使用）</summary><div className="table-scroll"><table className="compact"><thead><tr><th>报表</th><th>命令</th></tr></thead><tbody>{value.rows.map(row => <tr key={`command-${row.kind}`}><td>{manualStatementNames[row.kind] || row.kind}</td><td className="mono">{row.import_command}</td></tr>)}</tbody></table></div></details>
    <p className="subtle">{value.basis}。任何导入仍需人工确认、字段映射和同一交易日的账户/委托/成交核对。</p>
  </section>;
}

type FactorLibraryAblationView = {
  status: string;
  path: string | null;
  run_id: string | null;
  source_run: string | null;
  candidate_count: number;
  libraries: string[];
  rows: Record<string, unknown>[];
  limitations: string[];
  selection_enabled: boolean;
  execution_admitted: boolean;
};

type ResearchDecisionMatrixWithAblation = NonNullable<OfflineStrategyEvidenceView['research_decision_matrix']> & {
  factor_library_ablation?: FactorLibraryAblationView;
};

type SavedRiskAnalyticsView = {
  status: string;
  path: string;
  run_id: string | null;
  summary: Record<string, unknown>;
  artifacts?: string | null;
  limitations: string[];
  strategy_validated: boolean;
  execution_admitted: boolean;
};

type StrategyEvidenceWithRisk = OfflineStrategyEvidenceView & {
  saved_robustness?: {
    status: string;
    path: string;
    comparison: string | null;
    start: string | null;
    end: string | null;
    sessions: number;
    cases: number;
    window_lengths: number[];
    cost_stress_bps: number[];
    rolling_window_summaries: {
      case_id: string;
      signal: string;
      label: string;
      window_sessions: number;
      window_count: number;
      positive_window_ratio: number | null;
      return_median: number | null;
      worst_window_return: number | null;
      worst_window_drawdown: number | null;
      median_window_sharpe: number | null;
      status: string;
    }[];
    cost_sensitivity: {
      case_id: string;
      signal: string;
      label: string;
      stress_additional_slippage_bps: number;
      baseline_net_return: number | null;
      additional_cost: number | null;
      stressed_net_return: number | null;
      status: string;
    }[];
    fresh_holdout_claimed: boolean;
    strategy_validated: boolean;
    execution_admitted: boolean;
    basis: string;
    limits: string[];
  };
  model_stability?: {
    status: string;
    seed_count: number;
    test_rank_ic_mean: number | null;
    test_rank_ic_min: number | null;
    test_rank_ic_max: number | null;
    test_rank_ic_positive_seed_count: number;
    portfolio_net_return_median: number | null;
    portfolio_net_return_min: number | null;
    portfolio_net_return_max: number | null;
    portfolio_positive_seed_count: number;
    limitations: string[];
    execution_admitted: boolean;
  };
  factor_current_score?: {
    status: string;
    path?: string | null;
    run_id?: string | null;
    signal?: string | null;
    cutoff?: string | null;
    rank_count: number;
    finite_score_count: number;
    rank_is_current_for_execution_day: boolean;
    ranking_path?: string | null;
    top_rankings: { date?: string; rank?: string; symbol?: string; score?: string }[];
    model_source_run?: string | null;
    new_labels_used: boolean;
    limitations: string[];
    execution_admitted: boolean;
  };
  factor_paper_preview?: {
    status: string;
    path?: string | null;
    strategy_id?: string | null;
    source_run?: string | null;
    execution_day?: string | null;
    rank_source?: string | null;
    rank_is_current_for_execution_day: boolean;
    execution_scope?: string | null;
    terminal_equity_rmb?: number | null;
    cash_rmb?: number | null;
    position_count: number;
    provisional_rank_count: number;
    provisional_target_count: number;
    provisional_order_count: number;
    adapter_handoff_order_count: number;
    missing_quote_symbols: string[];
    release_state?: string | null;
    release_blockers: string[];
    artifacts: Record<string, string>;
    limitations: string[];
    execution_admitted: boolean;
  };
  factor_regime_overlay?: {
    status: string;
    path?: string | null;
    run_id?: string | null;
    signal?: string | null;
    result: Record<string, unknown>;
    limitations: string[];
    selection_enabled: boolean;
    execution_admitted: boolean;
    interpretation: string;
  };
  saved_risk_analytics?: {
    status: string;
    views: Record<string, SavedRiskAnalyticsView>;
    selection_enabled: boolean;
    execution_admitted: boolean;
    interpretation: string;
  };
};

/* function ResearchDecisionMatrixSection({ value }: { value: NonNullable<OfflineStrategyEvidenceView['research_decision_matrix']> }) {
  const matrix = value as ResearchDecisionMatrixWithAblation;
  const ablation = matrix.factor_library_ablation;
  return <details className="offline-details" open><summary>研究决策矩阵（机构研究台离线版）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>统一研究消费</strong><span>同一账本</span><span>不重算</span><span>不自动选优</span><span>执行准入关闭</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>比较案例</span><strong>{value.comparison.comparable_cases ?? '--'}/{value.comparison.requested_cases ?? '--'}</strong></div><div><span>比较区间</span><strong className="metric-text">{value.comparison.start || '--'} 至 {value.comparison.end || '--'}</strong></div><div><span>成本口径</span><strong>{value.comparison.same_cost_settings ? '一致' : '需复核'}</strong></div><div><span>执行释放</span><strong className="metric-text">关闭</strong></div></div><div className="table-scroll"><table className="compact"><thead><tr><th>策略/案例</th><th>区间收益</th><th>最大回撤</th><th>Sharpe</th><th>费用</th><th>滞后估值</th><th>状态</th></tr></thead><tbody>{value.strategy_rows.map(row => <tr key={String(row.case_id)}><td>{candidateNames[String(row.signal)] || String(row.signal || row.case_id || '--')}</td><td>{row.net_return == null ? '--' : pct(Number(row.net_return))}</td><td>{row.max_drawdown == null ? '--' : pct(Number(row.max_drawdown))}</td><td>{valueText(row.sharpe_zero_rf_252)}</td><td>{valueText(row.fees)}</td><td>{valueText(row.stale_valuation_days)}</td><td>描述性只读</td></tr>)}</tbody></table></div><h4 className="offline-table-heading">机构功能落地状态</h4><div className="table-scroll"><table className="compact"><thead><tr><th>功能</th><th>状态</th><th>选择</th><th>证据</th></tr></thead><tbody>{value.desk_functions.map(row => <tr key={row.function_id}><td>{row.desk_function}</td><td>{row.status}</td><td>{row.selection}</td><td className="mono">{row.source || row.reason || '--'}</td></tr>)}</tbody></table></div>{ablation?.status === 'completed_factor_candidate_library_ablation' && <><h4 className="offline-table-heading">因子家族增量消融（固定账本）</h4><div className="trade-boundary"><ShieldCheck size={16} /><strong>增量诊断</strong><span>固定候选池</span><span>固定成本</span><span>不自动选优</span><span>执行准入关闭</span></div><div className="table-scroll"><table className="compact"><thead><tr><th>剔除家族</th><th>剔除后收益</th><th>相对完整池</th><th>最大回撤</th><th>Sharpe</th><th>Sharpe变化</th><th>费用</th></tr></thead><tbody>{ablation.rows.map(row => <tr key={String(row.excluded_library)}><td>{String(row.excluded_library)}</td><td>{row.net_return == null ? '--' : pct(Number(row.net_return))}</td><td>{row.net_return_difference == null ? '--' : pct(Number(row.net_return_difference))}</td><td>{row.max_drawdown == null ? '--' : pct(Number(row.max_drawdown))}</td><td>{valueText(row.sharpe_zero_rf_252)}</td><td>{valueText(row.sharpe_difference)}</td><td>{valueText(row.fees)}</td></tr>)}</tbody></table></div><p className="subtle">固定 {ablation.candidate_count} 个候选、{ablation.libraries.join(' / ')}，收益差只说明该预先声明消融在本历史账本中的差异，不能证明因果 alpha 或未来增量。</p></>}{value.limitations.slice(0, 5).map(item => <li key={item}>{item}</li>)}</ul><p className="subtle">{value.operator_next_action || '--'}</p></details>;
} */

function ResearchDecisionMatrixSection({ value }: { value: NonNullable<OfflineStrategyEvidenceView['research_decision_matrix']> }) {
  const matrix = value as ResearchDecisionMatrixWithAblation;
  const ablation = matrix.factor_library_ablation;
  return <details className="offline-details" open>
    <summary>研究决策矩阵（机构研究台离线版）</summary>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>统一研究消费</strong><span>同一账本</span><span>不重算</span><span>不自动选优</span><span>执行准入关闭</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>比较案例</span><strong>{value.comparison.comparable_cases ?? '--'}/{value.comparison.requested_cases ?? '--'}</strong></div><div><span>比较区间</span><strong className="metric-text">{value.comparison.start || '--'} 至 {value.comparison.end || '--'}</strong></div><div><span>成本口径</span><strong>{value.comparison.same_cost_settings ? '一致' : '需复核'}</strong></div><div><span>执行释放</span><strong className="metric-text">关闭</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略/案例</th><th>区间收益</th><th>最大回撤</th><th>Sharpe</th><th>费用</th><th>滞后估值</th><th>状态</th></tr></thead><tbody>{value.strategy_rows.map(row => <tr key={String(row.case_id)}><td>{candidateNames[String(row.signal)] || String(row.signal || row.case_id || '--')}</td><td>{row.net_return == null ? '--' : pct(Number(row.net_return))}</td><td>{row.max_drawdown == null ? '--' : pct(Number(row.max_drawdown))}</td><td>{valueText(row.sharpe_zero_rf_252)}</td><td>{valueText(row.fees)}</td><td>{valueText(row.stale_valuation_days)}</td><td>描述性只读</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">机构功能落地状态</h4><div className="table-scroll"><table className="compact"><thead><tr><th>功能</th><th>状态</th><th>选择</th><th>证据</th></tr></thead><tbody>{value.desk_functions.map(row => <tr key={row.function_id}><td>{row.desk_function}</td><td>{row.status}</td><td>{row.selection}</td><td className="mono">{row.source || row.reason || '--'}</td></tr>)}</tbody></table></div>
    {ablation?.status === 'completed_factor_candidate_library_ablation' && <><h4 className="offline-table-heading">因子家族增量消融（固定账本）</h4><div className="trade-boundary"><ShieldCheck size={16} /><strong>增量诊断</strong><span>固定候选池</span><span>固定成本</span><span>不自动选优</span><span>执行准入关闭</span></div><div className="table-scroll"><table className="compact"><thead><tr><th>剔除家族</th><th>剔除后收益</th><th>相对完整池</th><th>最大回撤</th><th>Sharpe</th><th>Sharpe变化</th><th>费用</th></tr></thead><tbody>{ablation.rows.map(row => <tr key={String(row.excluded_library)}><td>{String(row.excluded_library)}</td><td>{row.net_return == null ? '--' : pct(Number(row.net_return))}</td><td>{row.net_return_difference == null ? '--' : pct(Number(row.net_return_difference))}</td><td>{row.max_drawdown == null ? '--' : pct(Number(row.max_drawdown))}</td><td>{valueText(row.sharpe_zero_rf_252)}</td><td>{valueText(row.sharpe_difference)}</td><td>{valueText(row.fees)}</td></tr>)}</tbody></table></div><p className="subtle">固定 {ablation.candidate_count} 个候选、{ablation.libraries.join(' / ')}；收益差只说明该预先声明消融在本历史账本中的差异，不能证明因果 alpha 或未来增量。</p></>}
    <ul className="offline-not-attempted">{value.limitations.slice(0, 5).map(item => <li key={item}>{item}</li>)}</ul><p className="subtle">{value.operator_next_action || '--'}</p>
  </details>;
}

function FactorLibrarySection({ value }: { value: NonNullable<NonNullable<OfflineStrategyEvidenceView['research_decision_matrix']>['factor_library']> }) {
  return <details className="offline-details"><summary>因子库覆盖、冗余与持有期诊断</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>研究诊断</strong><span>Alpha158 定义已消费</span><span>不等于净收益增量</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>因子定义</span><strong>{value.definitions_completed}</strong></div><div><span>高相关边</span><strong>{value.redundancy_edges}</strong></div><div><span>冗余分量</span><strong>{value.redundancy_components}</strong></div><div><span>账户回测</span><strong>{value.account_backtest ? '是' : '否'}</strong></div></div><div className="table-scroll"><table className="compact"><thead><tr><th>持有期</th><th>训练正向</th><th>验证正向</th><th>2026 诊断正向</th><th>三段均正</th><th>覆盖分量</th></tr></thead><tbody>{value.diagnostic_counts.map(row => <tr key={row.horizon}><td>{row.horizon} 日</td><td>{row.train_positive}</td><td>{row.valid_positive}</td><td>{row.inspected_2026_positive}</td><td>{row.all_three_positive}</td><td>{row.represented_training_components}</td></tr>)}</tbody></table></div><ul className="offline-not-attempted">{value.limitations.slice(0, 5).map(item => <li key={item}>{item}</li>)}</ul><p className="subtle">高相关边和跨年度正向计数用于压缩研究空间与定位候选，不代表独立 alpha、净收益或可执行性。</p></details>;
}

function FactorInventorySection({ value }: { value: NonNullable<NonNullable<OfflineStrategyEvidenceView['research_decision_matrix']>['factor_inventory']> }) {
  return <details className="offline-details"><summary>公式库目录与来源（静态）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>目录消费</strong><span>仅公式定义</span><span>不使用行情</span><span>不证明有效性</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>公式总数</span><strong>{value.formula_count}</strong></div><div><span>Alpha158</span><strong>{value.library_counts.qlib_alpha158 || 0}</strong></div><div><span>Alpha360</span><strong>{value.library_counts.qlib_alpha360 || 0}</strong></div><div><span>本地模板</span><strong>{value.library_counts.local_template || 0}</strong></div></div><p className="subtle">TA-Lib 操作符 {value.operator_count || 0} 个；目录用于研究检索和去重，不能代替因子增量、成本回放或账户验证。</p><p className="mono subtle">{value.formula_path || '--'}</p></details>;
}

function StrategyEvidenceSection({ value }: { value: OfflineStrategyEvidenceView }) {
  const evidenceWithRisk = value as StrategyEvidenceWithRisk;
  const modelStability = evidenceWithRisk.model_stability;
  const factorCurrentScore = evidenceWithRisk.factor_current_score;
  const factorPaperPreview = evidenceWithRisk.factor_paper_preview;
  const regimeOverlay = evidenceWithRisk.factor_regime_overlay;
  const savedRobustness = evidenceWithRisk.saved_robustness;
  const riskViews = evidenceWithRisk.saved_risk_analytics?.views || {};
  const concentration = riskViews.concentration?.summary || {};
  const stress = riskViews.stress_exposure?.summary || {};
  const style = riskViews.style_exposure?.summary || {};
  const factorRows = value.factor_replay.portfolio_metrics;
  const openingRows = value.opening_candidates;
  const capacityRows = value.capacity_rows;
  return <section className="offline-subsection strategy-evidence-section">
    {factorPaperPreview?.status === 'completed_factor_score_paper_preview' && <details className="offline-details" open><summary>因子排名纸面目标持仓与订单预览</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>统一账本预览</strong><span>{factorPaperPreview.execution_day || '--'} 纸面日</span><span>目标/差额 {factorPaperPreview.provisional_target_count}/{factorPaperPreview.provisional_order_count}</span><span>适配交接 {factorPaperPreview.adapter_handoff_order_count}</span><span>未写订单账本</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>纸面账户权益</span><strong>{valueText(factorPaperPreview.terminal_equity_rmb)}</strong></div><div><span>纸面现金</span><strong>{valueText(factorPaperPreview.cash_rmb)}</strong></div><div><span>持仓数</span><strong>{factorPaperPreview.position_count}</strong></div><div><span>候选排名</span><strong>{factorPaperPreview.provisional_rank_count}</strong></div></div><dl className="offline-life-details"><dt>释放状态</dt><dd>{factorPaperPreview.release_state || '--'}</dd><dt>释放阻断</dt><dd>{factorPaperPreview.release_blockers.join('；') || '无'}</dd><dt>行情缺失</dt><dd>{factorPaperPreview.missing_quote_symbols.join('、') || '无'}</dd><dt>来源</dt><dd className="mono">{factorPaperPreview.path || '--'}</dd></dl><p className="subtle">该预览把冻结因子排名送入统一目标持仓/订单计算，但账户来自研究回放、价格来自研究收盘价，不能直接提交。执行准入：关闭。</p><ul className="offline-not-attempted">{factorPaperPreview.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    <div className="research-heading"><div><h3>策略证据卡与晋级审查</h3><p className="subtle">同一冻结截面下的纸面候选、因子诊断和容量证据，不自动挑选或晋级</p></div><span className="bundle-status">{value.promotion.eligible_candidate_count}/{value.promotion.candidate_count} 个候选可晋级</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>研究复核</strong><span>未声明新鲜 holdout</span><span>未观察账户/行情</span><span>自动晋级关闭</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>开盘候选</span><strong>{openingRows.length}</strong></div><div><span>因子种子组合</span><strong>{factorRows.filter(row => row.variant !== 'RULE').length}</strong></div><div><span>晋级候选</span><strong>{value.promotion.eligible_candidate_count}</strong></div><div><span>因子回放</span><strong className="metric-text">诊断</strong></div></div>
     <div className="table-scroll"><table className="compact"><thead><tr><th>候选策略</th><th>回放收益</th><th>最大回撤</th><th>Sharpe</th><th>滞后估值日</th><th>数据可用性</th><th>订单数</th><th>预检接受/拒绝</th><th>交付状态</th></tr></thead><tbody>{openingRows.map(row => <tr key={row.strategy_id}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{pct(Number(row.comparison_return))}</td><td>{pct(Number(row.comparison_max_drawdown))}</td><td>{valueText(row.comparison_sharpe)}</td><td>{valueText(row.stale_valuation_days)}</td><td title={String(row.data_usability_reason || '')}>{dataUsabilityNames[String(row.data_usability_status || '')] || valueText(row.data_usability_status)}</td><td>{valueText(row.candidate_order_count)}</td><td>{valueText(row.ledger_preflight_accepted)}/{valueText(row.ledger_preflight_rejected)}</td><td>{valueText(row.handoff_status)}</td></tr>)}</tbody></table></div>
     {value.research_decision_matrix?.status === 'completed_read_only_research_decision_matrix' && <ResearchDecisionMatrixSection value={value.research_decision_matrix} />}
     {value.research_decision_matrix?.factor_library?.status === 'completed_research_diagnostics' && <FactorLibrarySection value={value.research_decision_matrix.factor_library} />}
     {value.research_decision_matrix?.factor_inventory?.status === 'completed_static_formula_inventory' && <FactorInventorySection value={value.research_decision_matrix.factor_inventory} />}
    {evidenceWithRisk.saved_risk_analytics?.status === 'completed_saved_historical_risk_views' && <details className="offline-details" open><summary>历史风险暴露与集中度诊断</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>独立历史诊断</strong><span>集中度</span><span>压力暴露</span><span>风格暴露</span><span>非当前账户</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>集中度样本</span><strong>{valueText(concentration.sessions)} 日 / {valueText(concentration.symbols)} 证券</strong></div><div><span>集中度前五损失占比</span><strong>{concentration.top_5_loss_share == null ? '--' : pct(Number(concentration.top_5_loss_share))}</strong></div><div><span>压力暴露</span><strong>{valueText(stress.stress_dates)} 日 / {valueText(stress.known_rows)} 已知</strong></div><div><span>风格诊断日期</span><strong>{valueText(style.common_dates)} / {valueText(style.decision_dates)}</strong></div></div><div className="table-scroll"><table className="compact"><thead><tr><th>视图</th><th>来源运行</th><th>核心摘要</th><th>限制</th></tr></thead><tbody>{Object.entries(riskViews).map(([key, view]) => <tr key={key}><td>{key}</td><td className="mono">{view.run_id || '--'}</td><td>{key === 'concentration' ? `${valueText(concentration.positive_symbol_count)} 正 / ${valueText(concentration.negative_symbol_count)} 负，最差日 ${valueText(concentration.worst_day)}` : key === 'stress_exposure' ? `${valueText(stress.exposure_rows)} 行，${valueText(stress.unknown_rows)} 未知` : `${valueText(style.models_refit)} 重拟合，${valueText(style.fresh_holdout_claimed)} 新鲜 holdout`}</td><td>{view.limitations.slice(0, 2).join('；') || '--'}</td></tr>)}</tbody></table></div><p className="subtle">{evidenceWithRisk.saved_risk_analytics.interpretation}</p></details>}
    {value.saved_ledger_comparison && value.saved_ledger_comparison.history.length > 0 && <details className="offline-details" open><summary>同账本比较窗口历史（只读）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>长期与最近窗口并列</strong><span>仅历史标记</span><span>不构成新鲜 holdout</span><span>自动晋级关闭</span></div>{value.saved_ledger_comparison.window_stability && <div className="metrics trade-metrics offline-life-metrics"><div><span>窗口稳定性</span><strong className="metric-text">{windowStabilityNames[value.saved_ledger_comparison.window_stability.status] || value.saved_ledger_comparison.window_stability.status}</strong></div><div><span>已评估窗口</span><strong>{value.saved_ledger_comparison.window_stability.evaluated_windows}</strong></div><div><span>正收益窗口</span><strong>{value.saved_ledger_comparison.window_stability.positive_windows}</strong></div><div><span>最差收益</span><strong>{pct(value.saved_ledger_comparison.window_stability.worst_net_return)}</strong></div></div>}<div className="table-scroll"><table className="compact" aria-label="同账本比较窗口历史"><thead><tr><th>窗口</th><th>交易日</th><th>可比案例</th><th>成本一致</th><th>候选收益</th><th>候选回撤</th><th>候选 Sharpe</th><th>滞后估值</th><th>描述性前沿</th><th>状态</th></tr></thead><tbody>{value.saved_ledger_comparison.history.map(row => <tr key={row.path}><td>{row.start || '--'} 至 {row.end || '--'}</td><td>{row.sessions}</td><td>{row.comparable_cases}/{row.requested_cases}</td><td>{row.same_cost_settings ? '是' : '否'}</td><td>{row.selected_case ? pct(row.selected_case.net_return) : '--'}</td><td>{row.selected_case ? pct(row.selected_case.max_drawdown) : '--'}</td><td>{row.selected_case ? valueText(row.selected_case.sharpe_zero_rf_252) : '--'}</td><td>{row.selected_case ? valueText(row.selected_case.stale_valuation_days) : '--'}</td><td>{row.descriptive_nondominated_case_ids.length || 0}</td><td>{row.execution_admitted ? '执行准入' : row.selection_enabled ? '可选择' : '只读诊断'}</td></tr>)}</tbody></table></div></details>}
    {savedRobustness?.status === 'saved_mark_robustness_diagnostic' && <details className="offline-details" open><summary>同账本稳健性与成本压力（最新固定诊断）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>只读稳健性</strong><span>{savedRobustness.start || '--'} 至 {savedRobustness.end || '--'}</span><span>{savedRobustness.sessions} 个交易日</span><span>{savedRobustness.window_lengths.join('/')} 日滚动窗口</span><span>{savedRobustness.cost_stress_bps.join('/')}bp 压力</span><span>不构成新鲜 holdout</span><span>执行准入关闭</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>候选案例</span><strong>{savedRobustness.cases}</strong></div><div><span>滚动摘要</span><strong>{savedRobustness.rolling_window_summaries.length}</strong></div><div><span>成本压力行</span><strong>{savedRobustness.cost_sensitivity.length}</strong></div><div><span>验证状态</span><strong className="metric-text">研究诊断</strong></div></div><h4 className="offline-table-heading">滚动窗口摘要</h4><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>窗口</th><th>窗口数</th><th>正收益比例</th><th>收益中位数</th><th>最差收益</th><th>最差回撤</th><th>Sharpe中位数</th></tr></thead><tbody>{savedRobustness.rolling_window_summaries.map(row => <tr key={`${row.case_id}-${row.window_sessions}`}><td>{candidateNames[row.signal] || row.label || row.signal}</td><td>{row.window_sessions}日</td><td>{row.window_count}</td><td>{pct(row.positive_window_ratio)}</td><td>{pct(row.return_median)}</td><td>{pct(row.worst_window_return)}</td><td>{pct(row.worst_window_drawdown)}</td><td>{valueText(row.median_window_sharpe)}</td></tr>)}</tbody></table></div><h4 className="offline-table-heading">额外滑点压力</h4><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>额外滑点</th><th>基准收益</th><th>增量成本</th><th>压力后收益</th></tr></thead><tbody>{savedRobustness.cost_sensitivity.map(row => <tr key={`${row.case_id}-${row.stress_additional_slippage_bps}`}><td>{candidateNames[row.signal] || row.label || row.signal}</td><td>{row.stress_additional_slippage_bps}bp</td><td>{pct(row.baseline_net_return)}</td><td>{valueText(row.additional_cost)} RMB</td><td>{pct(row.stressed_net_return)}</td></tr>)}</tbody></table></div><p className="subtle">{savedRobustness.basis}。{savedRobustness.limits.join('；')}</p><p className="mono subtle">{savedRobustness.path}</p></details>}
    <h4 className="offline-table-heading">Alpha158 + LightGBM 种子分歧</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>变体</th><th>种子</th><th>测试 Rank IC</th><th>净收益</th><th>最大回撤</th><th>Sharpe</th><th>订单</th></tr></thead><tbody>{factorRows.map((row, index) => <tr key={`${row.variant}-${row.seed || 'rule'}-${index}`}><td>{valueText(row.variant)}</td><td>{valueText(row.seed)}</td><td>{valueText(row.test_rank_ic)}</td><td>{pct(Number(row.net_return))}</td><td>{pct(Number(row.max_drawdown))}</td><td>{valueText(row.sharpe_zero_rf_252)}</td><td>{valueText(row.orders)}</td></tr>)}</tbody></table></div>
    {factorCurrentScore?.status === 'completed_frozen_cutoff_inference' && <details className="offline-details" open><summary>冻结截面因子排名（研究候选）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>固定模型推断</strong><span>截面 {factorCurrentScore.cutoff || '--'}</span><span>不使用新标签</span><span>不是实时行情</span><span>不生成委托</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>排名数量</span><strong>{factorCurrentScore.rank_count}</strong></div><div><span>可评分标的</span><strong>{factorCurrentScore.finite_score_count}</strong></div><div><span>执行日冻结排名</span><strong>{factorCurrentScore.rank_is_current_for_execution_day ? '是' : '否'}</strong></div><div><span>新标签</span><strong>{factorCurrentScore.new_labels_used ? '有' : '无'}</strong></div></div><div className="table-scroll"><table className="compact"><thead><tr><th>排名</th><th>证券</th><th>模型分数</th><th>截面日期</th></tr></thead><tbody>{factorCurrentScore.top_rankings.map(row => <tr key={`${row.rank}-${row.symbol}`}><td>{row.rank || '--'}</td><td className="mono">{row.symbol || '--'}</td><td>{row.score || '--'}</td><td>{row.date || '--'}</td></tr>)}</tbody></table></div><p className="subtle">该排名已接入研究证据卡，仍需统一账本预检、账户状态、同日人民币行情和人工策略选择后，才能形成纸面目标持仓。来源：<span className="mono">{factorCurrentScore.ranking_path || factorCurrentScore.path || '--'}</span></p><ul className="offline-not-attempted">{factorCurrentScore.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {modelStability?.status === 'completed_saved_model_seed_stability' && <details className="offline-details" open><summary>Alpha158 + LightGBM 模型稳定性汇总</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>种子离散度</strong><span>固定已保存模型</span><span>不重训</span><span>不自动选优</span><span>执行准入关闭</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>模型种子</span><strong>{modelStability.seed_count}</strong></div><div><span>测试 Rank IC 均值</span><strong>{valueText(modelStability.test_rank_ic_mean)}</strong></div><div><span>测试 Rank IC 范围</span><strong>{valueText(modelStability.test_rank_ic_min)} 至 {valueText(modelStability.test_rank_ic_max)}</strong></div><div><span>组合收益范围</span><strong>{modelStability.portfolio_net_return_min == null ? '--' : `${pct(modelStability.portfolio_net_return_min)} 至 ${pct(modelStability.portfolio_net_return_max ?? modelStability.portfolio_net_return_min)}`}</strong></div></div><p className="subtle">正向测试 IC 种子 {modelStability.test_rank_ic_positive_seed_count}/{modelStability.seed_count}；正收益组合种子 {modelStability.portfolio_positive_seed_count}/{modelStability.seed_count}；中位收益 {modelStability.portfolio_net_return_median == null ? '--' : pct(modelStability.portfolio_net_return_median)}。</p><ul className="offline-not-attempted">{modelStability.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {regimeOverlay?.status === 'completed_factor_breadth_regime_overlay' && <details className="offline-details" open><summary>因子组合市场状态覆盖诊断</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>风险覆盖对照</strong><span>固定阈值</span><span>同一账本</span><span>不调参</span><span>不自动晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>覆盖后收益</span><strong>{regimeOverlay.result.net_return == null ? '--' : pct(Number(regimeOverlay.result.net_return))}</strong></div><div><span>覆盖后回撤</span><strong>{regimeOverlay.result.max_drawdown == null ? '--' : pct(Number(regimeOverlay.result.max_drawdown))}</strong></div><div><span>相对收益变化</span><strong>{regimeOverlay.result.net_return_difference == null ? '--' : pct(Number(regimeOverlay.result.net_return_difference))}</strong></div><div><span>风险覆盖日</span><strong>{valueText(regimeOverlay.result.risk_off_decision_days)}</strong></div></div><p className="subtle">相对回撤变化 {regimeOverlay.result.max_drawdown_difference == null ? '--' : pct(Number(regimeOverlay.result.max_drawdown_difference))}；相对 Sharpe 变化 {valueText(regimeOverlay.result.sharpe_difference)}。{regimeOverlay.interpretation}</p><ul className="offline-not-attempted">{regimeOverlay.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {value.factor_attribution?.status === 'accounting_decomposition_only' && <details className="offline-details"><summary>因子候选账本归因（会计分解）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>只读诊断</strong><span>固定账本路径</span><span>不是因果 alpha</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>观察净损益</span><strong>{valueText(value.factor_attribution.net_pnl_cny)}</strong></div><div><span>净收益</span><strong>{value.factor_attribution.metrics.net_return == null ? '--' : pct(Number(value.factor_attribution.metrics.net_return))}</strong></div><div><span>费用</span><strong>{valueText(value.factor_attribution.costs.fees_cny)}</strong></div><div><span>换手/初始资金</span><strong>{value.factor_attribution.metrics.turnover_over_initial == null ? '--' : `${valueText(value.factor_attribution.metrics.turnover_over_initial)}x`}</strong></div></div><div className="table-scroll"><table className="compact"><thead><tr><th>分解项</th><th>数值</th><th>口径</th></tr></thead><tbody><tr><td>成交单数 / 成交笔数</td><td>{valueText(value.factor_attribution.metrics.orders)} / {valueText(value.factor_attribution.metrics.fills)}</td><td>保存账本</td></tr><tr><td>最小佣金溢价</td><td>{valueText(value.factor_attribution.costs.minimum_commission_premium_cny)}</td><td>成本诊断</td></tr><tr><td>同日开盘价差</td><td>{valueText(value.factor_attribution.raw_open_slippage.slippage_cny)}</td><td>模拟器价格差，不是券商冲击</td></tr><tr><td>费用+价差加回</td><td>{valueText(value.factor_attribution.fixed_fill_fee_and_slip_addback_cny)}</td><td>敏感性，不是可交易收益</td></tr></tbody></table></div><p className="subtle">{value.factor_attribution.interpretation} {value.factor_attribution.start || '--'} 至 {value.factor_attribution.end || '--'}，{value.factor_attribution.sessions} 个交易日。</p><ul className="offline-not-attempted">{value.factor_attribution.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {value.factor_cost_replay?.status === 'completed_fixed_cost_path' && <details className="offline-details"><summary>因子组合成本路径诊断（固定死区）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>负结果已保留</strong><span>不改模型</span><span>不调参数</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>成本路径收益</span><strong>{value.factor_cost_replay.result.net_return == null ? '--' : pct(Number(value.factor_cost_replay.result.net_return))}</strong></div><div><span>相对收益差</span><strong>{value.factor_cost_replay.result.net_return_difference == null ? '--' : pct(Number(value.factor_cost_replay.result.net_return_difference))}</strong></div><div><span>费用差</span><strong>{valueText(value.factor_cost_replay.result.fees_difference)}</strong></div><div><span>换手差</span><strong>{valueText(value.factor_cost_replay.result.turnover_difference)}</strong></div></div><p className="subtle">{value.factor_cost_replay.interpretation} 内部交易死区 {valueText(value.factor_cost_replay.execution_policy.minimum_internal_trade_cny)} 元；结果用于下一轮研究，不代表可交易收益。</p><ul className="offline-not-attempted">{value.factor_cost_replay.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {value.factor_cadence_replay?.status === 'completed_fixed_bimonthly_cadence' && <details className="offline-details"><summary>因子组合频率诊断（固定双月）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>固定诊断</strong><span>复用原排名</span><span>不调参</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>双月净收益</span><strong>{value.factor_cadence_replay.result.net_return == null ? '--' : pct(Number(value.factor_cadence_replay.result.net_return))}</strong></div><div><span>相对原频率</span><strong>{value.factor_cadence_replay.result.net_return_difference == null ? '--' : pct(Number(value.factor_cadence_replay.result.net_return_difference))}</strong></div><div><span>费用</span><strong>{valueText(value.factor_cadence_replay.result.fees)}</strong></div><div><span>换手差</span><strong>{valueText(value.factor_cadence_replay.result.turnover_difference)}</strong></div></div><p className="subtle">{String((value.factor_cadence_replay as unknown as { interpretation?: string }).interpretation || '')} 选用 {valueText(value.factor_cadence_replay.result.selected_month_ends)} 个信号月末，跳过 {valueText(value.factor_cadence_replay.result.skipped_month_ends)} 个。</p><ul className="offline-not-attempted">{value.factor_cadence_replay.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {value.factor_cadence_stability?.status === 'completed_saved_ledger_rolling_diagnostic' && <details className="offline-details"><summary>双月频率稳定性（滚动窗口）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>稳定性诊断</strong><span>同一账本</span><span>不重拟合</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>60日正窗口</span><strong>{value.factor_cadence_stability.windows['60']?.positive_window_ratio == null ? '--' : pct(Number(value.factor_cadence_stability.windows['60'].positive_window_ratio))}</strong></div><div><span>120日正窗口</span><strong>{value.factor_cadence_stability.windows['120']?.positive_window_ratio == null ? '--' : pct(Number(value.factor_cadence_stability.windows['120'].positive_window_ratio))}</strong></div><div><span>60日最差</span><strong>{value.factor_cadence_stability.windows['60']?.return_min == null ? '--' : pct(Number(value.factor_cadence_stability.windows['60'].return_min))}</strong></div><div><span>120日最差</span><strong>{value.factor_cadence_stability.windows['120']?.return_min == null ? '--' : pct(Number(value.factor_cadence_stability.windows['120'].return_min))}</strong></div></div><p className="subtle">{value.factor_cadence_stability.interpretation} 正窗口比例是描述性稳定性，不代表未来收益。</p><ul className="offline-not-attempted">{value.factor_cadence_stability.limitations.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul></details>}
    {value.explainability && <details className="offline-details"><summary>保存账本损益归因（历史诊断）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>诊断边界</strong><span>非当前行情</span><span>非因果归因</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>策略</span><strong className="metric-text">{candidateNames[value.explainability.strategy_id] || value.explainability.strategy_id}</strong></div><div><span>账本净损益</span><strong>{valueText(value.explainability.net_pnl_cny)}</strong></div><div><span>费用</span><strong>{valueText(value.explainability.fees_cny)}</strong></div><div><span>开盘价差</span><strong>{valueText(value.explainability.open_slippage_cny)}</strong></div></div><div className="table-scroll"><table className="compact"><thead><tr><th>类别</th><th>证券</th><th>观察损益</th><th>费用</th></tr></thead><tbody>{value.explainability.top_loss_symbols.slice(0, 5).map(row => <tr key={`loss-${row.symbol}`}><td>主要损失</td><td className="mono">{row.symbol}</td><td>{row.observed_pnl_cny}</td><td>{row.fees_cny}</td></tr>)}{value.explainability.top_gain_symbols.slice(0, 5).map(row => <tr key={`gain-${row.symbol}`}><td>主要收益</td><td className="mono">{row.symbol}</td><td>{row.observed_pnl_cny}</td><td>{row.fees_cny}</td></tr>)}</tbody></table></div><p className="subtle">{value.explainability.interpretation}</p></details>}
    {value.explainability && <details className="offline-details"><summary>保存账本损益归因（历史诊断）</summary><div className="trade-boundary"><ShieldCheck size={16} /><strong>诊断边界</strong><span>非当前行情</span><span>非因果归因</span><span>不用于晋级</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>策略</span><strong className="metric-text">{candidateNames[value.explainability.strategy_id] || value.explainability.strategy_id}</strong></div><div><span>账本净损益</span><strong>{valueText(value.explainability.net_pnl_cny)}</strong></div><div><span>费用</span><strong>{valueText(value.explainability.fees_cny)}</strong></div><div><span>开盘价差</span><strong>{valueText(value.explainability.open_slippage_cny)}</strong></div></div>{value.explainability_cases.length > 0 && <div className="table-scroll"><table className="compact"><thead><tr><th>归因案例</th><th>净损益</th><th>费用</th><th>开盘价差</th><th>换手</th><th>成交单数</th></tr></thead><tbody>{value.explainability_cases.map(row => <tr key={row.case_id}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{valueText(row.net_pnl_cny)}</td><td>{valueText(row.fees_cny)}</td><td>{valueText(row.open_slippage_cny)}</td><td>{valueText(row.turnover_value_cny)}</td><td>{valueText(row.filled_orders)}</td></tr>)}</tbody></table></div>}<div className="table-scroll"><table className="compact"><thead><tr><th>类别</th><th>证券</th><th>观察损益</th><th>费用</th></tr></thead><tbody>{value.explainability.top_loss_symbols.slice(0, 5).map(row => <tr key={`loss-${row.symbol}`}><td>主要损失</td><td className="mono">{row.symbol}</td><td>{row.observed_pnl_cny}</td><td>{row.fees_cny}</td></tr>)}{value.explainability.top_gain_symbols.slice(0, 5).map(row => <tr key={`gain-${row.symbol}`}><td>主要收益</td><td className="mono">{row.symbol}</td><td>{row.observed_pnl_cny}</td><td>{row.fees_cny}</td></tr>)}</tbody></table></div><p className="subtle">{value.explainability.interpretation}</p></details>}
    <details className="offline-details"><summary>纸面容量与晋级条件</summary><div className="table-scroll"><table className="compact"><thead><tr><th>候选</th><th>买入金额</th><th>买入后现金</th><th>总容量余量</th><th>单笔余量</th></tr></thead><tbody>{capacityRows.map(row => <tr key={String(row.strategy_id)}><td>{candidateNames[String(row.strategy_id)] || valueText(row.strategy_id)}</td><td>{valueText(row.paper_buy_gross_rmb)}</td><td>{valueText(row.paper_cash_after_buys_rmb)}</td><td>{valueText(row.paper_gross_headroom_rmb)}</td><td>{valueText(row.paper_single_headroom_rmb)}</td></tr>)}</tbody></table></div><ul className="offline-not-attempted">{value.promotion.required_evidence.map(item => <li key={item}>{item}</li>)}</ul></details>
    <details className="offline-details"><summary>当前解释与限制</summary><dl className="offline-life-details">{Object.entries(value.interpretation).filter(([key]) => key !== 'why' && key !== 'seed_return_range').flatMap(([key, item]) => [<dt key={`${key}-dt`}>{evidenceNames[key] || key}</dt>, <dd key={`${key}-dd`}>{valueText(item)}</dd>])}<dt>保留原因</dt><dd>{value.interpretation.why.join('；') || '--'}</dd></dl><ul className="offline-not-attempted">{value.factor_replay.limitations.concat(value.limitations).slice(0, 8).map(item => <li key={item}>{item}</li>)}</ul></details>
  </section>;
}

function ExecutionEvidenceSection({ value, strategyEvidence }: { value: OfflineExecutionEvidenceView; strategyEvidence: OfflineStrategyEvidenceView | null }) {
  const costRows = value.cost_stress.rows;
  const attention = value.risk_checks.attention_rows;
  const explainabilityByStrategy = new Map((strategyEvidence?.explainability_cases || []).map(row => [row.strategy_id, row]));
  const stress50ByStrategy = new Map(costRows.filter(row => Number(row.additional_slippage_bps) === 50).map(row => [row.strategy_id, row]));
  const decisionRows = value.strategy_execution_summary.map(row => ({
    ...row,
    explainability: explainabilityByStrategy.get(row.strategy_id),
    stress50: stress50ByStrategy.get(row.strategy_id),
  }));
  return <section className="offline-subsection execution-evidence-section">
    <div className="research-heading"><div><h3>纸面 TCA 与执行计划</h3><p className="subtle">把成本、风险检查和子单拆分接入同一订单流水线；仅使用冻结纸面投影</p></div><span className="bundle-status">交易证据：纸面诊断</span></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>执行诊断</strong><span>未观察成交</span><span>未连接券商</span><span>自动提交关闭</span></div>
    <div className="metrics trade-metrics offline-life-metrics"><div><span>预交易全成本</span><strong>{value.tca_pretrade.estimated_all_in_cost_rmb || '--'}</strong></div><div><span>滑点压力行</span><strong>{value.cost_stress.row_count}</strong></div><div><span>风险关注项</span><strong>{value.risk_checks.attention_row_count}</strong></div><div><span>子单/母单</span><strong>{value.child_order_schedule.row_count}/{value.child_order_schedule.parent_count}</strong></div></div>
    <div className="table-scroll"><table className="compact"><thead><tr><th>指标</th><th>数值</th><th>口径</th></tr></thead><tbody>{value.tca_pretrade.rows.map(row => <tr key={row.metric}><td>{row.metric}</td><td>{row.value || '--'}</td><td>{row.basis || '--'}</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">额外滑点压力</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>额外滑点</th><th>基准费用</th><th>增量滑点成本</th><th>全执行成本</th><th>实际券商</th></tr></thead><tbody>{costRows.map(row => <tr key={row.stress_id}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{row.additional_slippage_bps} bp</td><td>{row.baseline_paper_fees_rmb}</td><td>{row.incremental_slippage_cost_rmb}</td><td>{row.estimated_total_execution_cost_rmb}</td><td>否</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">按策略执行摘要</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>纸面净收益</th><th>基准/50bp成本</th><th>关注项</th><th>子单/母单</th><th>状态</th></tr></thead><tbody>{value.strategy_execution_summary.map(row => <tr key={row.strategy_id}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{row.paper_net_return == null ? '--' : pct(Number(row.paper_net_return))}</td><td>{row.baseline_cost_rmb || '--'} / {row.cost_at_50bps_rmb || '--'}</td><td>{row.risk_attention_count}/{row.risk_row_count}</td><td>{row.child_order_count}/{row.child_parent_count}</td><td>纸面诊断</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">策略决策对照</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>保存账本净损益</th><th>50bp全成本</th><th>风险关注</th><th>成交单数</th><th>决策状态</th></tr></thead><tbody>{decisionRows.map(row => <tr key={`decision-${row.strategy_id}`}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td>{row.explainability ? valueText(row.explainability.net_pnl_cny) : '--'}</td><td>{row.stress50?.estimated_total_execution_cost_rmb || row.cost_at_50bps_rmb || '--'}</td><td>{row.risk_attention_count}/{row.risk_row_count}</td><td>{row.explainability?.filled_orders ?? '--'}</td><td>{row.explainability ? '历史诊断，禁止晋级' : '纸面执行诊断'}</td></tr>)}</tbody></table></div>
    <h4 className="offline-table-heading">风险检查聚合</h4>
    <div className="table-scroll"><table className="compact"><thead><tr><th>检查项</th><th>阻断/关注</th><th>状态汇总</th><th>下一动作</th></tr></thead><tbody>{value.risk_checks.summary.map(row => <tr key={row.check}><td>{row.check}</td><td>{row.attention_count}</td><td>{Object.entries(row.status_counts).map(([status, count]) => `${status}:${count}`).join('；')}</td><td>{row.next_action}</td></tr>)}</tbody></table></div>
    <details className="offline-details"><summary>风险检查与需要人工关注的订单 ({attention.length})</summary><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>证券</th><th>方向</th><th>数量</th><th>检查</th><th>状态</th><th>原因</th></tr></thead><tbody>{attention.map((row, index) => <tr key={`${row.strategy_id}-${row.symbol}-${row.check}-${index}`}><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td className="mono">{row.symbol || '--'}</td><td>{row.side || '--'}</td><td>{row.qty || '--'}</td><td>{row.check || '--'}</td><td>{row.status || '--'}</td><td>{row.reason || '--'}</td></tr>)}</tbody></table></div></details>
    <details className="offline-details"><summary>子单执行时间表（首 {Math.min(value.child_order_schedule.rows.length, 40)} 行 / 共 {value.child_order_schedule.row_count} 行）</summary><div className="table-scroll"><table className="compact"><thead><tr><th>母单</th><th>子单</th><th>策略</th><th>证券</th><th>方向</th><th>计划时间</th><th>数量</th><th>状态</th></tr></thead><tbody>{value.child_order_schedule.rows.slice(0, 40).map(row => <tr key={`${row.parent_sequence}-${row.child_sequence}-${row.symbol}`}><td>{row.parent_sequence || '--'}</td><td>{row.child_sequence || '--'}</td><td>{candidateNames[row.strategy_id] || row.strategy_id}</td><td className="mono">{row.symbol || '--'}</td><td>{row.side || '--'}</td><td>{row.planned_time || '--'}</td><td>{row.planned_qty || '--'}</td><td>{row.child_state || '--'}</td></tr>)}</tbody></table></div></details>
    <p className="subtle">{value.basis}</p>
  </section>;
}

export function OfflineOperatorBoard() {
  const [payload, setPayload] = useState<OfflineOperatorPackage | null>(null);
  const [instrumentMaster, setInstrumentMaster] = useState<OfflineInstrumentMasterView | null>(null);
  const [brokerObservation, setBrokerObservation] = useState<OfflineBrokerObservationView | null>(null);
  const [accountRisk, setAccountRisk] = useState<OfflineAccountRiskView | null>(null);
  const [openingWindowStatus, setOpeningWindowStatus] = useState<OfflineOpeningWindowStatusView | null>(null);
  const [lifecycle, setLifecycle] = useState<OfflinePaperLifecycleView | null>(null);
  const [thsReference, setThsReference] = useState<OfflineThsReferencePayload | null>(null);
  const [observationBundle, setObservationBundle] = useState<OfflineObservationBundleReadiness | null>(null);
  const [journalStatus, setJournalStatus] = useState<OfflineThsJournalStatus | null>(null);
  const [simulatedLedger, setSimulatedLedger] = useState<OfflineThsSimulatedLedgerReadiness | null>(null);
  const [selectedObservationPlanId, setSelectedObservationPlanId] = useState('');
  const [manualStatements, setManualStatements] = useState<OfflineManualStatementView | null>(null);
  const [bundleImportStatus, setBundleImportStatus] = useState('');
  const [bundleImporting, setBundleImporting] = useState(false);
  const [ledgerPreview, setLedgerPreview] = useState<OfflineThsLedgerImportPreview | null>(null);
  const ledgerPreviewRequestSeq = useRef(0);
  function invalidateLedgerPreview() {
    ledgerPreviewRequestSeq.current += 1;
    setLedgerPreview(null);
  }
  const [decisionQueue, setDecisionQueue] = useState<OfflineDecisionQueueView | null>(null);
  const [evidence, setEvidence] = useState<OfflineStrategyEvidenceView | null>(null);
  const [executionEvidence, setExecutionEvidence] = useState<OfflineExecutionEvidenceView | null>(null);
  const [portfolioControls, setPortfolioControls] = useState<OfflinePortfolioControlsView | null>(null);
  const [portfolioRisk, setPortfolioRisk] = useState<OfflinePortfolioRiskView | null>(null);
  const [releaseControls, setReleaseControls] = useState<OfflineReleaseControlsView | null>(null);
  const [basketSchedule, setBasketSchedule] = useState<OfflineBasketScheduleView | null>(null);
  const [blotter, setBlotter] = useState<OfflineBlotterView | null>(null);
  const [executionAttribution, setExecutionAttribution] = useState<OfflineExecutionAttributionView | null>(null);
  const [sessionHandoff, setSessionHandoff] = useState<OfflineSessionHandoffView | null>(null);
  const [prefillReadbackResult, setPrefillReadbackResult] = useState<OfflineThsPrefillReadbackResponse | null>(null);
  const [prefillReadbackBusy, setPrefillReadbackBusy] = useState(false);
  const [selectedPaper, setSelectedPaper] = useState<NonNullable<OfflinePaperSelectionResponse['receipt']> | null>(null);
  const [selectedPaperCapacity, setSelectedPaperCapacity] = useState<OfflinePaperSelectionView['capacity']>(null);
  const [selectedPlan, setSelectedPlan] = useState<OfflineSelectedPlanView | null>(null);
  const [paperPreviewLatest, setPaperPreviewLatest] = useState<OfflinePaperPreviewLatestView | null>(null);
  const [paperPreviewReadiness, setPaperPreviewReadiness] = useState<OfflinePaperPreviewReadinessView | null>(null);
  const [quoteImportStatus, setQuoteImportStatus] = useState('');
  const [quoteImporting, setQuoteImporting] = useState(false);
  const [quoteInitializing, setQuoteInitializing] = useState(false);
  const [thsNativeObservation, setThsNativeObservation] = useState<OfflineThsNativeObservationView | null>(null);
  const [thsStrategyId, setThsStrategyId] = useState('');
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const refresh = useCallback(async () => {
    try {
      const thsPath = thsStrategyId ? `/trading/ths-reference-payload?strategy_id=${encodeURIComponent(thsStrategyId)}` : '/trading/ths-reference-payload';
      const packageRequest = api<OfflineOperatorPackage>('/trading/operator-package');
      const [nextPayload, nextOpeningWindowStatus, nextLifecycle, nextThsReference, nextObservationBundle, nextJournalStatus, nextSimulatedLedger, nextManualStatements, nextEvidence, nextExecutionEvidence, nextDecisionQueue, nextPortfolioControls, nextPortfolioRisk, nextSelectedPaper, nextSelectedPlan, nextReleaseControls, nextBasketSchedule, nextBlotter, nextExecutionAttribution, nextSessionHandoff, nextPaperPreviewLatest, nextPaperPreviewReadiness, nextThsNativeObservation, nextInstrumentMaster, nextBrokerObservation, nextAccountRisk] = await Promise.all([
        packageRequest,
        api<OfflineOpeningWindowStatusView>('/trading/opening-window-status').catch(() => null),
        api<OfflinePaperLifecycleView>('/trading/operator-package/lifecycle'),
        api<OfflineThsReferencePayload>(thsPath),
        api<OfflineObservationBundleReadiness>('/trading/ths-observation-bundle/readiness'),
        api<OfflineThsJournalStatus>('/trading/ths-observation-bundle/journal-status'),
        api<OfflineThsSimulatedLedgerReadiness>('/trading/ths-simulated-ledger/readiness'),
        api<OfflineManualStatementView>('/trading/operator-package/manual-statements'),
        api<OfflineStrategyEvidenceView>('/trading/operator-package/evidence'),
        api<OfflineExecutionEvidenceView>('/trading/operator-package/execution-evidence'),
        api<OfflineDecisionQueueView>('/trading/operator-package/decision-queue'),
        api<OfflinePortfolioControlsView>('/trading/operator-package/portfolio-controls'),
        packageRequest.then(value => value.artifact_pointers?.offline_portfolio_risk_summary
          ? api<OfflinePortfolioRiskView>('/trading/operator-package/portfolio-risk') : null),
        api<OfflinePaperSelectionView>('/trading/operator-package/paper-selection'),
        api<OfflineSelectedPlanView>('/trading/operator-package/selected-plan'),
        api<OfflineReleaseControlsView>('/trading/operator-package/release-controls'),
        api<OfflineBasketScheduleView>('/trading/operator-package/basket-schedule'),
        api<OfflineBlotterView>('/trading/operator-package/blotter'),
        api<OfflineExecutionAttributionView>('/trading/operator-package/execution-attribution'),
        api<OfflineSessionHandoffView>('/trading/operator-package/session-handoff'),
        api<OfflinePaperPreviewLatestView>('/trading/paper-preview-latest'),
        api<OfflinePaperPreviewReadinessView>('/trading/paper-preview-readiness'),
        api<OfflineThsNativeObservationView>('/trading/ths-native-observation'),
        api<OfflineInstrumentMasterView>('/trading/operator-package/instrument-master').catch(() => null),
        api<OfflineBrokerObservationView>('/trading/operator-package/broker-observation-alerts').catch(() => null),
        api<OfflineAccountRiskView>('/trading/operator-package/account-risk').catch(() => null),
      ]);
      setPayload(nextPayload); setOpeningWindowStatus(nextOpeningWindowStatus); setLifecycle(nextLifecycle); setThsReference(nextThsReference); setObservationBundle(nextObservationBundle); setJournalStatus(nextJournalStatus); setSimulatedLedger(nextSimulatedLedger); setManualStatements(nextManualStatements); setEvidence(nextEvidence); setExecutionEvidence(nextExecutionEvidence); setDecisionQueue(nextDecisionQueue); setPortfolioControls(nextPortfolioControls); setPortfolioRisk(nextPortfolioRisk); setSelectedPaper(nextSelectedPaper.receipt); setSelectedPaperCapacity(nextSelectedPaper.capacity); setSelected(nextSelectedPaper.strategy_id || ''); setSelectedPlan(nextSelectedPlan); setReleaseControls(nextReleaseControls); setBasketSchedule(nextBasketSchedule); setBlotter(nextBlotter); setExecutionAttribution(nextExecutionAttribution); setSessionHandoff(nextSessionHandoff); setPaperPreviewLatest(nextPaperPreviewLatest); setPaperPreviewReadiness(nextPaperPreviewReadiness); setThsNativeObservation(nextThsNativeObservation); setInstrumentMaster(nextInstrumentMaster); setBrokerObservation(nextBrokerObservation); setAccountRisk(nextAccountRisk); setError('');
    } catch (cause) { setError((cause as Error).message); }
  }, [thsStrategyId]);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    // A refresh may replace the local export bundle while keeping the same
    // selected plan/run. Any previous Ledger delta is then stale by definition.
    invalidateLedgerPreview();
  }, [observationBundle]);
  async function selectPaperStrategy(strategyId: string) {
    if (!window.confirm(`确认将“${candidateNames[strategyId] || strategyId}”生成单策略纸面方案？\n\n只写入离线纸面产物，不访问实时行情、账户、同花顺或真实委托。`)) return;
    setSelected(strategyId); setError('');
    try { const result = await post<OfflinePaperSelectionResponse>('/trading/operator-package/paper-selection', { strategy_id: strategyId }); setSelectedPaper(result.receipt); await refresh(); } catch (cause) { setSelected(''); setError((cause as Error).message); }
  }

  async function verifyPrefillReadback(input: { strategy_id: string; sequence: number; symbol: string; price: string; qty: string }) {
    setPrefillReadbackBusy(true); setError('');
    try {
      const result = await post<OfflineThsPrefillReadbackResponse>('/trading/ths-reference-payload/readback', input);
      setPrefillReadbackResult(result);
    } catch (cause) {
      setPrefillReadbackResult(null);
      setError('回读核对失败：' + (cause as Error).message);
    } finally {
      setPrefillReadbackBusy(false);
    }
  }

  async function importQuoteObservation() {
    if (!paperPreviewReadiness?.quote_input || paperPreviewReadiness.quote_input.status !== 'ready_for_import') return;
    if (!window.confirm('确认把固定目录中的原始人民币行情追加到本地观察日志？\n\n不会访问同花顺、不会预填、不会提交委托或修改 Ledger。')) return;
    setQuoteImporting(true); setQuoteImportStatus('正在校验并追加观察日志…'); setError('');
    try {
      const result = await post<OfflineQuoteObservationImportResponse>('/trading/quote-observation/import', { confirm_import: true });
      setQuoteImportStatus(result.status === 'imported' ? '已写入本地观察日志。' : result.status === 'already_present' ? '该观察已存在，未重复写入。' : result.next_action || result.status);
      await refresh();
    } catch (cause) {
      setQuoteImportStatus('导入未执行：' + (cause as Error).message);
    } finally {
      setQuoteImporting(false);
    }
  }

  async function initializeQuoteObservation() {
    if (!paperPreviewReadiness?.quote_input || paperPreviewReadiness.quote_input.status !== 'missing_template') return;
    if (!window.confirm('确认生成当前纸面计划所需的行情空模板？\n\n只创建本地 CSV/JSON 模板，不写入价格、不访问同花顺。')) return;
    setQuoteInitializing(true); setQuoteImportStatus('正在生成空模板…'); setError('');
    try {
      const result = await post<OfflineQuoteObservationImportResponse>('/trading/quote-observation/initialize', { confirm_initialize: true });
      setQuoteImportStatus(result.next_action || result.status);
      await refresh();
    } catch (cause) {
      setQuoteImportStatus('模板未生成：' + (cause as Error).message);
    } finally {
      setQuoteInitializing(false);
    }
  }

  async function importObservationBundle(planId?: string) {
    if (!observationBundle || observationBundle.status !== 'bundle_ready_for_reconciliation') return;
    if (!window.confirm('确认将已校验的同花顺人工导出包追加到本地观察日志？\n\n不会访问客户端、提交委托或修改真实账户；导入后仍需单独完成账本对账。')) return;
    invalidateLedgerPreview();
    setBundleImportStatus('正在追加本地观察日志…'); setBundleImporting(true); setError('');
    try {
      const result = await post<OfflineObservationBundleImportResponse>('/trading/ths-observation-bundle/import', { confirm_import: true, ...(planId ? { plan_id: planId } : {}) });
      const replayNote = result.idempotent_replay ? '（重复导入已幂等去重）' : '';
      setBundleImportStatus(result.status === 'imported' ? (result.plan ? `导出包已写入观察日志${replayNote}，计划 ${planId} 已刷新；仍未修改真实账本。` : `导出包已写入本地观察日志${replayNote}，未修改账本。`) : result.next_action || result.status);
      await refresh();
    } catch (cause) {
      setBundleImportStatus('导入未执行：' + (cause as Error).message);
    } finally {
      setBundleImporting(false);
    }
  }

  async function importObservationBundleToLedger(planId: string, runId: string) {
    if (!observationBundle || observationBundle.status !== 'bundle_ready_for_reconciliation') return;
    if (!window.confirm(`确认把已校验的同花顺观察包写入计划“${planId}”，并回灌到已绑定的暂停 paper Ledger？\n\n必须已逐项核对账户、委托、成交、手续费、现金和持仓；不会访问客户端、提交委托或修改真实账户。`)) return;
    invalidateLedgerPreview();
    setBundleImportStatus('正在导入观察包并回灌同一 paper Ledger…'); setBundleImporting(true); setError('');
    try {
      const result = await post<OfflineObservationBundleImportResponse>('/trading/ths-observation-bundle/import', { confirm_import: true, plan_id: planId, run_id: runId, confirm_ledger_import: true });
      const replayNote = result.idempotent_replay ? '（观察日志幂等去重）' : '';
      const previewNote = result.ledger_preview?.status ? `（服务器预览：${result.ledger_preview.status}）` : '';
      setBundleImportStatus(result.ledger_imported ? `观察包已写入日志${replayNote}，且已回灌同一 paper Ledger${result.ledger_mutated ? '' : '（Ledger 无新增变更）'}${previewNote}。` : result.next_action || result.status);
      await refresh();
    } catch (cause) {
      setBundleImportStatus('观察包已可能写入日志，但 Ledger 回灌未完成：' + (cause as Error).message);
    } finally {
      setBundleImporting(false);
    }
  }

  async function previewObservationBundleToLedger(planId: string, runId: string) {
    if (!observationBundle || observationBundle.status !== 'bundle_ready_for_reconciliation') return;
    const requestSeq = ++ledgerPreviewRequestSeq.current;
    setBundleImportStatus('正在计算同一 paper Ledger 的导入差额（不写入）…'); setBundleImporting(true); setError('');
    try {
      const result = await post<OfflineThsLedgerImportPreview>('/trading/ths-simulated-ledger/preview-import', { run_id: runId, plan_id: planId, use_observation_bundle: true });
      if (requestSeq !== ledgerPreviewRequestSeq.current) return;
      if (result.run_id !== runId || result.plan_id !== planId) {
        setLedgerPreview(null);
        setBundleImportStatus('差额预览身份不匹配，未保留结果。');
        return;
      }
      setLedgerPreview(result);
      setBundleImportStatus(result.status === 'ready_for_import' ? '差额预览完成，Ledger 未变更。' : result.next_action || result.status);
    } catch (cause) {
      if (requestSeq !== ledgerPreviewRequestSeq.current) return;
      setLedgerPreview(null);
      setBundleImportStatus('差额预览未执行：' + (cause as Error).message);
    } finally {
      if (requestSeq === ledgerPreviewRequestSeq.current) setBundleImporting(false);
    }
  }

  async function initializeObservationBundle() {
    if (!window.confirm('确认在本地导出目录生成四个空白模板？\n\n不会读取同花顺、不会写观察日志、不会创建账本或订单。')) return;
    invalidateLedgerPreview();
    setBundleImportStatus('正在生成空白导出模板…'); setBundleImporting(true); setError('');
    try {
      const result = await post<OfflineObservationBundleImportResponse>('/trading/ths-observation-bundle/initialize', { confirm_initialize: true });
      setBundleImportStatus(result.next_action || result.status); await refresh();
    } catch (cause) {
      setBundleImportStatus('模板未生成：' + (cause as Error).message);
    } finally {
      setBundleImporting(false);
    }
  }

  async function bindSimulatedLedger(runId: string, accountAlias: string) {
    if (!window.confirm(`确认把模拟账户“${accountAlias}”绑定到暂停的 paper run？\n\n只会写入同一纸面 Ledger，不会访问客户端或提交订单。`)) return;
    setError('');
    try {
      const result = await post<OfflineThsSimulatedLedgerMutationResponse>('/trading/ths-simulated-ledger/bind', { run_id: runId, account_alias: accountAlias, confirm_simulated_account: true });
      setBundleImportStatus(result.status); await refresh();
    } catch (cause) { setError('模拟账户绑定未执行：' + (cause as Error).message); }
  }

  async function prepareSimulatedLedger(runId: string) {
    if (!window.confirm('确认暂停这个空白 paper run，为同花顺模拟账户开盘观察准备统一 Ledger？\n\n只会暂停本地空白纸面运行，不访问客户端、不读取账户、不提交订单。')) return;
    setError('');
    try {
      const result = await post<OfflineThsSimulatedLedgerMutationResponse>('/trading/ths-simulated-ledger/prepare-run', { run_id: runId, confirm_prepare: true });
      setBundleImportStatus(result.next_action || result.status); await refresh();
    } catch (cause) { setError('模拟账户绑定准备未执行：' + (cause as Error).message); }
  }

  async function createSimulatedLedgerRun(accountAlias: string) {
    if (!window.confirm(`确认按模拟账户“${accountAlias}”的已核对开盘观察创建匹配初始资金的空白 paper run？\n\n只读取本地观察日志，不访问客户端、不提交订单。`)) return;
    setError('');
    try {
      const result = await post<OfflineThsSimulatedLedgerMutationResponse>('/trading/ths-simulated-ledger/create-run', { account_alias: accountAlias, confirm_create: true });
      setBundleImportStatus(result.next_action || result.status); await refresh();
    } catch (cause) { setError('按账户观察创建 paper run 未执行：' + (cause as Error).message); }
  }

  async function importSimulatedLedger(runId: string, planId: string) {
    if (!window.confirm(`确认把计划“${planId}”的已对账模拟成交回灌到同一 paper Ledger？\n\n必须已经核对账户、委托、成交、手续费、现金和持仓。`)) return;
    setError('');
    try {
      const result = await post<OfflineThsSimulatedLedgerMutationResponse>('/trading/ths-simulated-ledger/import', { run_id: runId, plan_id: planId, confirm_import: true });
      setBundleImportStatus(result.status); await refresh();
    } catch (cause) { setError('模拟成交回灌未执行：' + (cause as Error).message); }
  }

  async function reconcileSimulatedLedger(runId: string, includeRestrictionReleases: boolean, includeShareCredits: boolean) {
    if (!window.confirm('确认按当前观察包核对下一交易日并写入同一 paper Ledger？\n\n必须已经核对现金、持仓、可卖/T+1、委托、成交和资金流水；不会访问客户端或提交委托。')) return;
    setError('');
    try {
      const result = await post<OfflineThsSimulatedLedgerMutationResponse>('/trading/ths-simulated-ledger/reconcile-session', { run_id: runId, include_restriction_releases: includeRestrictionReleases, include_share_credits: includeShareCredits, confirm_reconcile: true });
      setBundleImportStatus(result.next_action || result.status); await refresh();
    } catch (cause) { setError('下一交易日对账未执行：' + (cause as Error).message); }
  }

  if (error && !payload) return <section className="trade-section"><div className="research-heading"><h2>离线运营包</h2><button className="icon-button" aria-label="刷新离线运营包" title="刷新" onClick={refresh}><RefreshCw size={17} /></button></div><ErrorMessage message={error} /></section>;
  if (!payload) return <section className="trade-section"><div className="empty small-empty">读取离线运营包</div></section>;
  const flags = Object.entries(payload.live_state);
  return <section className="trade-section offline-board">
    <div className="research-heading"><div><h2>离线运营包与机构功能映射</h2><p className="subtle">交易日 {payload.execution_day} · 数据截止 {payload.source_cutoff} · {payload.status}</p></div><button className="icon-button" aria-label="刷新离线运营包" title="刷新" onClick={refresh}><RefreshCw size={17} /></button></div>
    <div className="trade-boundary"><ShieldCheck size={16} /><strong>纸面模式</strong><span>选择：{selectionNames[payload.strategy_selection.selection_status || ''] || payload.strategy_selection.selection_status || '未记录'}</span><span>自动晋级关闭</span><span>真实账户、实时行情、委托和成交均未观察</span></div>
    <div className="metrics trade-metrics"><div><span>候选策略</span><strong>{payload.strategy_promotion_review.candidate_count}</strong></div><div><span>可晋级候选</span><strong>{payload.strategy_promotion_review.eligible_candidate_count}</strong></div><div><span>预演订单</span><strong>{payload.order_counts.total}</strong></div><div><span>执行准入</span><strong className="metric-text">关闭</strong></div></div>
    <Warning>{payload.next_action}</Warning>
     {payload.stockdb_frozen_snapshot && payload.stockdb_independent_sample && payload.stockdb_independent_window_stability && <section className="offline-subsection"><div className="research-heading"><div><h3>研究证据与样本边界</h3><p className="subtle">当前纸面订单引用的 StockDB 冻结快照、独立样本和窗口稳定性结果</p></div><span className="bundle-status">研究只读</span></div><div className="trade-boundary"><ShieldCheck size={16} /><strong>不自动晋级</strong><span>快照截止匹配：{payload.stockdb_frozen_snapshot.source_cutoff_match ? '是' : '否'}</span><span>样本隔离：{payload.stockdb_independent_sample.independent_sample && !payload.stockdb_independent_sample.prior_forward_overlap ? '是' : '否'}</span><span>执行准入：关闭</span></div><div className="metrics trade-metrics offline-life-metrics"><div><span>冻结股票/日期</span><strong>{payload.stockdb_frozen_snapshot.requested_symbols}/{payload.stockdb_frozen_snapshot.requested_dates}</strong></div><div><span>可用日线</span><strong>{payload.stockdb_frozen_snapshot.available_daily}/{payload.stockdb_frozen_snapshot.requested_daily}</strong></div><div><span>独立评估日</span><strong>{payload.stockdb_independent_sample.evaluation_sessions}</strong></div><div><span>配对行</span><strong>{payload.stockdb_independent_sample.provider_complete_paired_rows}</strong></div><div><span>稳定窗口</span><strong>{payload.stockdb_independent_window_stability.window_count}</strong></div></div><dl className="offline-life-details"><dt>冻结快照</dt><dd className="mono">{payload.stockdb_frozen_snapshot.source_file}</dd><dt>独立样本</dt><dd>{payload.stockdb_independent_sample.selection_cutoff} 选样 → {payload.stockdb_independent_sample.evaluation_start} 至 {payload.stockdb_independent_sample.evaluation_end} · {payload.stockdb_independent_sample.alternate_provider} 配对 · 非重叠 {payload.stockdb_independent_sample.prior_forward_overlap ? '否' : '是'}</dd><dt>窗口覆盖</dt><dd>{payload.stockdb_independent_window_stability.window_start} 至 {payload.stockdb_independent_window_stability.window_end} · 日历连续性 {payload.stockdb_independent_window_stability.calendar_continuity_verified ? '已确认' : '未确认'} · 覆盖 {payload.stockdb_independent_window_stability.calendar_window_coverage_verified ? '已确认' : '未确认'}</dd></dl>{payload.stockdb_independent_window_stability.strategy_summaries && payload.stockdb_independent_window_stability.strategy_summaries.length > 0 && <><h4>跨窗口策略摘要</h4><div className="table-scroll"><table className="compact"><thead><tr><th>策略</th><th>正窗口</th><th>平均收益</th><th>中位收益</th><th>最差收益</th><th>最差回撤</th><th>订单/成交</th></tr></thead><tbody>{payload.stockdb_independent_window_stability.strategy_summaries.map(row => <tr key={row.candidate}><td>{candidateNames[row.candidate] || row.candidate}</td><td>{row.positive_windows}/{row.windows}</td><td>{pct(row.mean_net_return)}</td><td>{pct(row.median_net_return)}</td><td>{pct(row.worst_net_return)}</td><td>{pct(row.worst_max_drawdown)}</td><td>{row.total_orders}/{row.total_fills}</td></tr>)}</tbody></table></div><p className="subtle">窗口账户分别重置；该摘要用于比较和复核，不是连续账户收益，也不产生执行晋级。</p></>}</section>}
    {openingWindowStatus && <section className="offline-subsection"><div className="research-heading"><div><h3>开盘窗口协调状态</h3><p className="subtle">同一交易日只执行一次协调流程，状态由本地协调器写入</p></div><span className="bundle-status">{openingWindowStatus.state || openingWindowStatus.status}</span></div><div className="trade-boundary"><ShieldCheck size={16} /><strong>{openingWindowStatus.state || openingWindowStatus.status}</strong><span>交易日 {openingWindowStatus.trading_date}</span><span>最早动作 {openingWindowStatus.earliest_action_time || '--'}</span><span>客户端 {openingWindowStatus.client_interacted ? '已读取' : '未读取'}</span><span>行情探测 {openingWindowStatus.network_probe_attempted ? '已执行' : '未执行'}</span><span>账本未变更</span></div><dl className="offline-life-details"><dt>观察时间</dt><dd>{openingWindowStatus.observed_at || '--'}</dd><dt>错误</dt><dd>{openingWindowStatus.error || '无'}</dd><dt>下一动作</dt><dd>{openingWindowStatus.next_action || '--'}</dd><dt>来源</dt><dd className="mono">{openingWindowStatus.source_file}</dd></dl></section>}
    {(instrumentMaster || brokerObservation || accountRisk) && <section className="offline-subsection"><div className="research-heading"><div><h3>身份、账户风险与只读回读边界</h3><p className="subtle">三个应用视图来自同一离线运营包；只展示研究/只读证据，不产生账户或订单动作</p></div><span className="bundle-status">执行准入关闭</span></div>
      {instrumentMaster && <div className="trade-boundary"><strong>证券主表</strong><span>{instrumentMaster.status}</span><span>截止 {instrumentMaster.source_cutoff || '--'}</span><span>共同标的 {instrumentMaster.coverage.common_count ?? '--'}</span><span>研究身份元数据</span></div>}
      {accountRisk && <div className="trade-boundary"><strong>账户风险</strong><span>{accountRisk.status || '--'}</span><span>来源 {accountRisk.primary_evidence || accountRisk.source_scope || '--'}</span><span>账本 {accountRisk.ledger_mutated ? '已变更' : '未变更'}</span><span>{accountRisk.blockers?.length || 0} 个阻断</span><span>{accountRisk.next_action || '仅复核'}</span></div>}
      {brokerObservation && <div className="trade-boundary"><strong>券商/同花顺观察</strong><span>{brokerObservation.status}</span><span>告警 {brokerObservation.open_count}</span><span>账户证据 {brokerObservation.persistent_bundle_readiness.bundle_is_account_evidence ? '已验证' : '未验证'}</span><span>来源 {brokerObservation.alert_source_selection.reconciliation_source_scope || '--'}</span><span>{brokerObservation.next_action}</span></div>}
    </section>}
    {paperPreviewReadiness && <PaperPreviewReadinessSection value={paperPreviewReadiness} onImportQuote={importQuoteObservation} quoteImporting={quoteImporting} quoteImportStatus={quoteImportStatus} onInitializeQuote={initializeQuoteObservation} quoteInitializing={quoteInitializing} />}
    {thsNativeObservation && thsNativeObservation.status !== 'incomplete' && <ThsNativeObservationSection value={thsNativeObservation} />}
    {paperPreviewLatest && paperPreviewLatest.status !== 'not_observed' && <PaperPreviewLatestSection value={paperPreviewLatest} />}
    <section className="offline-subsection"><h3>策略候选与成本压力</h3><CandidateTable rows={payload.strategy_candidates} onSelect={selectPaperStrategy} selected={selected} /></section>
    <section className="offline-subsection"><h3>机构交易员功能逐项对照</h3><FunctionTable rows={payload.function_matrix} /></section>
    <section className="offline-subsection"><h3>纸面动作清单</h3><ActionTable rows={payload.operator_actions} /></section>
    {evidence && <StrategyEvidenceSection value={evidence} />}
    {executionEvidence && <ExecutionEvidenceSection value={executionEvidence} strategyEvidence={evidence} />}
    {decisionQueue && <DecisionQueueSection value={decisionQueue} />}
    {portfolioControls && <PortfolioControlsSection value={portfolioControls} />}
    {portfolioRisk ? <PortfolioRiskSection value={portfolioRisk} /> : <section className="offline-subsection"><h3>候选组合风险书与账户容量</h3><p className="subtle" role="status">当前离线包尚无候选组合风险书，现金占用、容量和风险均不可用；不能据此判断零风险。</p></section>}
    {selectedPaper && <SelectedPaperSection value={selectedPaper} capacity={selectedPaperCapacity} />}
    {selectedPlan?.status === 'offline_selected_plan_ready' && <SelectedPlanSection value={selectedPlan} />}
    {releaseControls && <ReleaseControlsSection value={releaseControls} />}
    {basketSchedule && <BasketScheduleSection value={basketSchedule} />}
    {blotter && <BlotterSection value={blotter} />}
    {executionAttribution && <ExecutionAttributionSection value={executionAttribution} />}
    {sessionHandoff && <SessionHandoffSection value={sessionHandoff} />}
    {thsReference && <ThsReferenceSection value={thsReference} onStrategyChange={strategyId => { setThsStrategyId(strategyId); setPrefillReadbackResult(null); }} />}
    {thsReference && <PrefillReadbackSection value={thsReference} onVerify={verifyPrefillReadback} result={prefillReadbackResult} busy={prefillReadbackBusy} />}
    {observationBundle && journalStatus && <ObservationBundleSection value={observationBundle} journalStatus={journalStatus} ledgerRuns={simulatedLedger?.runs || []} selectedPlanId={selectedObservationPlanId} onPlanIdChange={planId => { invalidateLedgerPreview(); setSelectedObservationPlanId(planId); }} onLedgerRunChange={invalidateLedgerPreview} onInitialize={initializeObservationBundle} onImport={importObservationBundle} onPreviewToLedger={(planId, runId) => { invalidateLedgerPreview(); previewObservationBundleToLedger(planId, runId); }} onImportToLedger={importObservationBundleToLedger} ledgerPreview={ledgerPreview} importStatus={bundleImportStatus} importing={bundleImporting} />}
    {simulatedLedger && <SimulatedLedgerSection value={simulatedLedger} onPrepare={prepareSimulatedLedger} onCreateRun={createSimulatedLedgerRun} onBind={bindSimulatedLedger} onImport={importSimulatedLedger} onReconcile={reconcileSimulatedLedger} busy={bundleImporting} status={bundleImportStatus} />}
    {manualStatements && <ManualStatementSection value={manualStatements} />}
    {lifecycle && <LifecycleSection value={lifecycle} />}
    <details className="offline-details"><summary>当前运行边界</summary><div className="offline-flags">{flags.map(([key, value]) => <span key={key} className={value ? 'flag-on' : 'flag-off'}>{liveNames[key] || key}：{value ? '是' : '否'}</span>)}</div></details>
    {error && <p className="subtle offline-refresh-error">刷新未成功，继续显示上次离线包：{error}</p>}
  </section>;
}
