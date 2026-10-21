import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { fmt } from '../types';
import { NativeShortlineEvidence, type NativeEvidence } from './NativeShortlineEvidence';
import './ResearchCandidateEvidence.css';

type EvidenceSource = { path: string; sha256?: string };
type RankSummary = {
  scope: string; subset: string; arm: string;
  registered_month_count: number | null; valid_IC_months: number | null;
  mean_rank_IC: number | null; positive_IC_fraction: number | null;
  valid_group_spread_months: number | null; mean_Q5_minus_Q1_price_spread: number | null;
  paired_valid_months: number | null; mean_delta_IC: number | null;
};
type RankingDiagnostic = {
  status: string; reason?: string | null; saved_at: string | null;
  source: EvidenceSource | null;
  coverage: { score_months: number | null; unique_symbols: number | null;
    label_rows: number | null; observed_labels: number | null; missing_labels: number | null;
    unique_symbols_reason?: string | null; unique_symbols_provenance?: EvidenceSource | null } | null;
  summaries: RankSummary[] | null; nonoverlap_retained_dates?: unknown;
};
type AccountCase = {
  year: number | string; arm: string; status: string; evidence_status?: string; reason?: string | null;
  engine_sessions_committed: number | null; complete_PnL_available: boolean | null;
  metrics: { net_return?: number | null; max_drawdown?: number | null } | null;
  residual_positions: unknown; residual_security_count: number | null; residual_share_count: number | null;
  stopped_day: string | null; source_issues: unknown; parent_orders: number | null; fill_callbacks: number | null;
};
type AccountComparison = {
  status: string; reason?: string | null; saved_at: string | null; source: EvidenceSource | null;
  cases: AccountCase[] | null; complete_cases: number | null; unresolved_cases: number | null; unknown_cases?: number | null;
};
type ResearchCandidate = {
  id: string | null; title: string | null; reviewed_at: string | null; reproduction_status: string | null;
  period: unknown; source_review?: string | null; source?: string[] | null;
  known_gaps: unknown[] | null; next_test: unknown; status: string; reason?: string | null;
  ranking_diagnostic: RankingDiagnostic | null; account_comparison: AccountComparison | null;
  native_evidence?: NativeEvidence | null;
};
type EvidenceView = {
  schema: string; status: string; reason?: string | null; served_at: string;
  registry: { path: string; sha256?: string; reviewed_at?: string | null } | null;
  candidate_count: number | null; candidates: ResearchCandidate[];
};

const LATEST_ID = 'adjusted_52week_high_vs_matched_12_1_monthly_positive_pool_v1';
const TITLES: Record<string, string> = {
  [LATEST_ID]: '52周高点接近度与月动量对照',
  native_12_1_monthly_leader_common_universe_benchmark_v1: '原价12-1月动量基准',
  alpha158_lightgbm_fixed_five_name_baseline_v1: 'Alpha158与LightGBM固定模型基准',
  stock_defensive_fixed_40_20_20_v1: '股债金固定配比对照',
  low_risk_price_volume_ablation_v1: '低波动与动量三规则对照',
  three_asset_equal_risk_1y_v1: '股债金等风险配置改编',
  three_asset_class_own_total_return_10m_v1: '股债金总回报十月均线择时',
  six_equity_etf_own_total_return_10m_v1: '六只股票ETF总回报均线择时',
  csi300_monthly_variance_local_v1: '月动量与沪深300波动率仓位控制',
  monthly_momentum_csi300_10m_local_v2: '沪深300均线过滤月动量',
  MASTER_AAAI2024_local_adaptation_candidate: 'MASTER本地模型改编对照',
  TSFM_China_JEF2024_pending_fundamentals: '中国因子择时文献候选（未运行）',
  V61C_turnover_size_PIT_reproduction_candidate: 'V61C低换手小市值候选（未运行）',
  emotion_core_replenishment_v2: '连板延续与情绪风格假说',
  full_source_public_seat_donchian_v1: '公开席位事件与唐奇安趋势对照',
  lhb_identifiable_buy_concentration_v1: '龙虎榜席位集中度对照',
  shortline_fixed_emotion_rule_v1: '固定情绪量价规则对照',
  long_term_owner_quality_v1: '长期持有风格质量因子改编',
  native_fixed_eight_leaders_environment_comparison_v1: '八类龙头与市场环境对照',
};
const ARM_NAMES: Record<string, string> = { high52: '52周高点接近度', momentum12_1: '匹配月动量' };
const title = (item: ResearchCandidate) => item.title || (item.id ? TITLES[item.id] || item.id : '未知研究条目');
const text = (item: unknown, missing = '未提供') => item == null || item === '' ? missing
  : typeof item === 'object' ? JSON.stringify(item) : String(item);
const finite = (item: unknown): item is number => typeof item === 'number' && Number.isFinite(item);
const count = (item: unknown) => finite(item) ? fmt(item, 0) : '未提供';
const numeric = (item: unknown, digits = 4) => finite(item) ? fmt(item, digits) : '不可用';
const percent = (item: unknown) => finite(item) ? `${fmt(item * 100, 2)}%` : '不可用';
const spread = (item: unknown) => finite(item) ? `${fmt(item * 100, 3)} 个百分点` : '不可用';
const clock = (item: string | null | undefined) => item ? item.replace('T', ' ') : '未提供';
const armName = (arm: string) => ARM_NAMES[arm] || arm;
const scopeName = (scope: string) => scope === 'all' ? '两年合计（描述）' : scope;
const subsetName = (subset: string) => subset === 'fixed_nonoverlap_anchors' ? '固定非重叠原点'
  : subset === 'all_registered_months' ? '全部登记月份' : subset;

function SourceLine({ source }: { source: EvidenceSource | null | undefined }) {
  return source ? <code className="rce-source">{source.path}</code> : <span className="subtle">来源未提供</span>;
}

function RankTable({ rows, label, scope = false }: { rows: RankSummary[]; label: string; scope?: boolean }) {
  return <div className="table-scroll"><table className="compact rce-table" aria-label={label}>
    <thead><tr><th>{scope ? '范围 / 抽样' : '研究年份'}</th><th>方法</th><th>有效IC月 / 登记月</th><th>平均IC</th><th>正IC月份比例</th><th>Q5−Q1价差</th><th>价差有效月</th></tr></thead>
    <tbody>{rows.map((row, index) => <tr key={`${row.scope}:${row.subset}:${row.arm}:${index}`}>
      <td>{scopeName(row.scope)}{scope ? <><br /><span className="subtle">{subsetName(row.subset)}</span></> : null}</td>
      <td>{armName(row.arm)}</td><td>{count(row.valid_IC_months)} / {count(row.registered_month_count)}</td>
      <td>{numeric(row.mean_rank_IC)}</td><td>{percent(row.positive_IC_fraction)}</td>
      <td>{spread(row.mean_Q5_minus_Q1_price_spread)}</td><td>{count(row.valid_group_spread_months)}</td>
    </tr>)}</tbody>
  </table></div>;
}

function PairedRankTable({ rows }: { rows: RankSummary[] }) {
  return <div className="table-scroll"><table className="compact rce-table" aria-label="共同有效月份的配对IC差异">
    <thead><tr><th>范围</th><th>抽样</th><th>两臂共同有效月份</th><th>平均ΔIC（高价−动量）</th></tr></thead>
    <tbody>{rows.map((row, index) => <tr key={`${row.scope}:${row.subset}:${index}`}>
      <td>{scopeName(row.scope)}</td><td>{subsetName(row.subset)}</td><td>{count(row.paired_valid_months)}</td><td>{numeric(row.mean_delta_IC)}</td>
    </tr>)}</tbody>
  </table></div>;
}

function RankingEvidence({ evidence }: { evidence: RankingDiagnostic | null }) {
  const saved = evidence?.status === 'saved';
  const rows = saved && Array.isArray(evidence.summaries) ? evidence.summaries : [];
  const individual = rows.filter(row => ['high52', 'momentum12_1'].includes(row.arm));
  const primary = individual.filter(row => ['2024', '2025'].includes(row.scope) && row.subset === 'all_registered_months');
  const other = individual.filter(row => !(['2024', '2025'].includes(row.scope) && row.subset === 'all_registered_months'));
  const paired = rows.filter(row => row.arm === 'paired_high52_minus_momentum12_1');
  return <details className="offline-details rce-detail" aria-label="全池排名诊断">
    <summary><strong>全池排名诊断</strong><span className="rce-summary-note">{saved ? `${count(evidence.coverage?.score_months)}个评分日` : '证据不可用'}</span></summary>
    {!saved ? <p className="rce-message" role="status">排名证据不可用：{text(evidence?.reason, '尚未保存可读取的诊断')}。账户回放与研究库独立展示。</p>
      : <>
        <p className="subtle rce-note">从次日开盘到其后第20个交易日开盘的重构调整价格变化。IC和分组价差用于检查排序，不是扣费后的账户收益；月份和股票观察也不当作独立市场样本。</p>
        <div className="window-summary rce-coverage">
          <span>评分日 <strong>{count(evidence.coverage?.score_months)}</strong></span>
          <span>不同证券 <strong>{count(evidence.coverage?.unique_symbols)}</strong></span>
          <span>股票月标签 <strong>{count(evidence.coverage?.label_rows)}</strong></span>
          <span>已知 / 缺失 <strong>{count(evidence.coverage?.observed_labels)} / {count(evidence.coverage?.missing_labels)}</strong></span>
        </div>
        {primary.length ? <RankTable rows={primary} label="2024与2025全池因子诊断" /> : <p className="rce-message">两年分列汇总未提供，不按两年合计或旧结果补齐。</p>}
        {other.length || paired.length ? <details className="rce-subdetail"><summary>两年合计、固定非重叠原点与配对差异 · 全部保留</summary>
          {other.length ? <RankTable rows={other} label="全部范围与固定非重叠原点诊断" scope /> : null}
          {paired.length ? <PairedRankTable rows={paired} /> : null}
          <p className="subtle rce-note">配对ΔIC沿保存的共同有效月份计算，不按正数选择方法或抽样起点。</p></details> : null}
        <details className="rce-subdetail"><summary>排名证据来源与实际计算时间</summary><dl className="rce-facts">
          <dt>诊断实际完成</dt><dd>{clock(evidence.saved_at)}</dd><dt>原结果</dt><dd><SourceLine source={evidence.source} /></dd>
          <dt>非重叠原点</dt><dd>{text(evidence.nonoverlap_retained_dates)}</dd>
          <dt>不同证券计数依据</dt><dd>{evidence.coverage?.unique_symbols_provenance ? <SourceLine source={evidence.coverage.unique_symbols_provenance} /> : text(evidence.coverage?.unique_symbols_reason, '未提供独立计数依据')}</dd>
        </dl></details>
      </>}
  </details>;
}

function AccountEvidence({ evidence }: { evidence: AccountComparison | null }) {
  const rows = Array.isArray(evidence?.cases) ? evidence.cases : [];
  const available = evidence?.status === 'saved' || evidence?.status === 'partial';
  return <details className="offline-details rce-detail" aria-label="四组独立账户回放">
    <summary><strong>独立研究账户回放</strong><span className="rce-summary-note">{available ? `${count(evidence.complete_cases)}组完整 / ${count(evidence.unresolved_cases)}组原结果未完成${evidence.status === 'partial' ? ` / ${count(evidence.unknown_cases)}组读取待核` : ''}` : '证据不可用'}</span></summary>
    {!available ? <p className="rce-message" role="status">账户比较证据不可用：{text(evidence?.reason, '尚无可读取的完整比较')}。不使用排名价差补账户收益。</p>
      : evidence.status === 'partial' ? <p className="rce-message" role="status">部分账户证据不可读取，其他已对应记录独立保留；不使用旧结果补齐缺项。</p> : null}
    {rows.length ? <>
      <p className="subtle rce-note">四组账户各自独立，不合并资金。完整收益和未完成账户分开；残仓估值、已提交会话或成交记录都不能代替完整收益。</p>
      <div className="table-scroll"><table className="compact rce-table" aria-label="52周高价与匹配动量四组账户结果"><thead><tr>
        <th>年份 / 方法</th><th>状态</th><th>完整净收益</th><th>最大回撤</th><th>已提交会话</th><th>模拟委托 / 成交事件</th><th>未退出证券 / 股数</th><th>停止日期 / 原缺项</th>
      </tr></thead><tbody>{rows.map((row, index) => {
        const complete = row.evidence_status !== 'unknown' && row.complete_PnL_available === true && row.metrics != null;
        return <tr key={`${row.year}:${row.arm}:${index}`} data-research-account-status={complete ? 'complete' : 'unavailable'}>
          <td>{row.year}<br />{armName(row.arm)}</td><td>{complete ? '完整结果' : '收益不可用'}</td>
          <td>{complete ? percent(row.metrics?.net_return) : '不可用'}</td><td>{complete ? percent(row.metrics?.max_drawdown) : '不可用'}</td>
          <td>{count(row.engine_sessions_committed)}</td><td>{count(row.parent_orders)} / {count(row.fill_callbacks)}</td>
          <td>{count(row.residual_security_count)} / {count(row.residual_share_count)}</td>
          <td className="rce-wrap">{row.stopped_day || (complete ? '原区间已完成' : '日期未提供')}
            <details className="rce-row-detail"><summary>原状态与缺项</summary><dl className="rce-facts">
              <dt>原状态</dt><dd>{text(row.status)}</dd><dt>读取缺项</dt><dd>{text(row.reason, '无额外读取缺项')}</dd>
              <dt>原来源问题</dt><dd>{text(row.source_issues)}</dd><dt>原残仓</dt><dd>{text(row.residual_positions)}</dd>
            </dl></details>
          </td>
        </tr>;
      })}</tbody></table></div>
    </> : available ? <p className="rce-message">四组账户明细未返回，不能解释为零持仓或零收益。</p> : null}
    <details className="rce-subdetail"><summary>账户比较来源与实际计算时间</summary><dl className="rce-facts">
      <dt>比较实际保存</dt><dd>{clock(evidence?.saved_at)}</dd><dt>原结果</dt><dd><SourceLine source={evidence?.source} /></dd>
    </dl></details>
  </details>;
}

function conclusion(candidate: ResearchCandidate | undefined) {
  if (!candidate) return '最新52周高价对照条目尚不可读取，其余研究方法可在下方查看。';
  const ranking = candidate.ranking_diagnostic;
  const all = Array.isArray(ranking?.summaries) ? ranking.summaries.filter(row => row.scope === 'all' && row.subset === 'all_registered_months') : [];
  const paired = ['high52', 'momentum12_1'].map(arm => all.find(row => row.arm === arm));
  const known = ranking?.status === 'saved' && paired.every(row => row && finite(row.mean_rank_IC) && finite(row.mean_Q5_minus_Q1_price_spread));
  const negative = known && paired.every(row => row!.mean_rank_IC! < 0 && row!.mean_Q5_minus_Q1_price_spread! < 0);
  const rankText = ranking?.status !== 'saved' ? '全池排名证据目前不可用，账户与其他研究仍独立保留。'
    : negative ? '52周高价接近度与匹配月动量的全池平均IC、分组价差均为负，当前结果未显示稳定选股优势。'
      : known ? '52周高价接近度与匹配月动量已有全池排名比较，结果仍不足以确认稳定策略优势。'
        : '全池排名汇总有缺项，现有条目不足以确认策略优势。';
  const account = candidate.account_comparison;
  const accountText = (account?.status === 'saved' || account?.status === 'partial') && finite(account.complete_cases) && finite(account.unresolved_cases)
    ? `独立账户中${count(account.complete_cases)}组已有完整结果，${count(account.unresolved_cases)}组原结果仍不可评价。${account.status === 'partial' ? `另有${count(account.unknown_cases)}组读取待核。` : ''}`
    : '账户完整收益仍需按各组保存证据分别核对。';
  return rankText + accountText;
}

export function ResearchCandidateEvidence() {
  const [view, setView] = useState<EvidenceView | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const [version, setVersion] = useState(0);
  const [query, setQuery] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError(''); setView(null);
    api<EvidenceView>('/trading/research/candidate-evidence', { signal: controller.signal })
      .then(next => {
        if (controller.signal.aborted) return;
        if (next.schema !== 'research_candidate_evidence_catalog_v1' || !Array.isArray(next.candidates)) throw new Error('研究库返回格式不可读取。');
        if (next.status === 'unavailable') throw new Error(text(next.reason, '原研究库不可读取。'));
        setView(next);
      })
      .catch((problem: Error) => { if (!controller.signal.aborted) { setView(null); setError(problem.message); } })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [version]);
  const latest = view?.candidates.find(item => item.id === LATEST_ID);
  const native = view?.candidates.find(item => item.native_evidence != null);
  const periods = Array.isArray(latest?.ranking_diagnostic?.summaries)
    ? [...new Set(latest.ranking_diagnostic.summaries.filter(row => ['2024', '2025'].includes(row.scope)).map(row => row.scope))] : [];
  const search = query.trim().toLocaleLowerCase();
  const matches = view?.candidates.filter(item => !search || [title(item), item.id, text(item.period, ''),
    item.source_review || '', text(item.known_gaps, ''), item.reproduction_status || ''].join(' ').toLocaleLowerCase().includes(search)) || [];
  return <section className="trade-section research-candidate-evidence" aria-label="策略研究结论" aria-busy={busy}>
    <div className="research-heading"><h2>策略研究结论</h2><button disabled={busy} aria-label="刷新策略研究结论" title="只读刷新保存的研究证据" onClick={() => setVersion(current => current + 1)}><RefreshCw size={15} />{busy ? '读取中' : '刷新结论'}</button></div>
    {busy ? <p className="subtle" role="status">正在读取已保存的研究结论…</p> : null}
    {error ? <p className="rce-message" role="alert">研究库读取失败：{error}。日更方案与其他研究卡片独立读取。</p> : null}
    {view ? <>
      <NativeShortlineEvidence evidence={native?.native_evidence || null} />
      <p className="rce-conclusion" data-research-conclusion>{conclusion(latest)}</p>
      <div className="window-summary rce-times">
        <span>高价对照时期 <strong>{periods.length ? periods.join(' / ') : text(latest?.period)}</strong></span>
        <span>高价诊断实际完成 <strong>{clock(latest?.ranking_diagnostic?.saved_at)}</strong></span>
        <span>页面读取 <strong>{clock(view.served_at)}</strong></span>
      </div>
      {latest ? <><RankingEvidence evidence={latest.ranking_diagnostic} /><AccountEvidence evidence={latest.account_comparison} /></> : null}
      <details className="offline-details rce-library" aria-label="全部策略研究库"><summary><strong>全部研究库 · {count(view.candidate_count)}项方法</strong><span className="rce-summary-note">研究方法与文献候选，非推荐股票</span></summary>
        <label className="rce-search">检索研究库<input type="search" aria-label="检索策略研究库" placeholder="方法、关键词或缺项" value={query} onChange={event => setQuery(event.target.value)} /></label>
        {view.candidates.length ? <p className="subtle rce-match-count">显示{matches.length} / {view.candidates.length}项，沿原研究库顺序。</p> : <p className="rce-message">未返回可展示的研究条目，不根据旧数量补齐。</p>}
        {!matches.length && view.candidates.length ? <p className="subtle rce-note" role="status">没有匹配的研究方法。</p> : null}
        {matches.map((item, index) => <details className="rce-candidate" key={`${item.id}:${index}`}>
          <summary><strong>{title(item)}</strong>{item.status === 'unknown' ? <span className="rce-summary-note">证据待核</span> : null}</summary>
          {item.status === 'unknown' ? <p className="rce-message">该条证据不可用：{text(item.reason, '原条目未能完整对应')}。</p> : null}
          <dl className="rce-facts">
            <dt>原研究范围</dt><dd>{text(item.period)}</dd><dt>原状态</dt><dd>{text(item.reproduction_status)}</dd>
            <dt>来源解释</dt><dd>{text(item.source_review)}</dd>
            <dt>保留缺项</dt><dd>{Array.isArray(item.known_gaps) ? item.known_gaps.length ? <ul>{item.known_gaps.map((gap, gapIndex) => <li key={gapIndex}>{text(gap)}</li>)}</ul> : '原条目未列缺项，不代表已验证' : '缺项记录未提供'}</dd>
            <dt>原定下一步</dt><dd>{text(item.next_test)}</dd>
          </dl>
          <details className="rce-subdetail"><summary>原方法编号、来源与复核日期</summary><dl className="rce-facts">
            <dt>方法编号</dt><dd><code className="rce-source">{text(item.id)}</code></dd><dt>原复核日期</dt><dd>{text(item.reviewed_at)}</dd>
            <dt>原来源声明</dt><dd>{Array.isArray(item.source) && item.source.length ? <ul>{item.source.map((source, sourceIndex) => <li key={sourceIndex}>{/^https?:\/\//i.test(source) ? <a href={source} target="_blank" rel="noreferrer">{source}</a> : <code className="rce-source">{source}</code>}</li>)}</ul> : '原来源未提供'}</dd>
          </dl></details>
        </details>)}
      </details>
      <p className="subtle rce-footer">仅研究，未用于下单。排名诊断、完整账户收益和资料不足分别保留；文献及公开风格改编不等于完整复现。</p>
      <details className="rce-subdetail"><summary>研究库来源</summary><dl className="rce-facts"><dt>原研究库</dt><dd><code className="rce-source">{text(view.registry?.path)}</code></dd><dt>原复核日期</dt><dd>{text(view.registry?.reviewed_at)}</dd></dl></details>
    </> : null}
  </section>;
}
