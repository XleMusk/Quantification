import { fmt } from '../types';

type Source = { path: string; sha256?: string };
type SavedSection = { status: string; reason?: string | null; source: Source | null; saved_at: string | null };
type QualityRow = {
  model_kind: string; stage: number; event_rows: number | null; evaluable_rows: number | null;
  unknown_rows: number | null; positive_rows: number | null; base_rate: number | null;
  training_prior: number | null; metrics: Record<string, number | null> | null;
};
type Quality = SavedSection & {
  rows: QualityRow[] | null;
  coverage: { registered_days: number | null; unique_events: number | null; mature_events: number | null;
    unknown_events: number | null; unique_symbols: number | null } | null;
  model_available_at: string | null; event_start: string | null; event_end: string | null;
  label_witness_cutoff: string | null;
};
type AccountRow = {
  model_kind: string; committed_sessions: number | null; last_committed_day: string | null;
  blocked_day: string | null; parent_orders: number | null; fill_callbacks: number | null;
  fees: number | null; residual_positions: unknown; pending_targets: unknown;
  complete_PnL_available: boolean | null; metrics: { net_return?: number | null } | null;
  source_issues: { reason: string; symbol?: string; status?: string; day?: string }[] | null;
  main_window: unknown;
};
type Accounts = SavedSection & { rows: AccountRow[] | null };
type AttributionCategory = {
  execution_status: string; selected_slots: number | null; known_label_slots: number | null;
  unknown_label_slots: number | null; hit5_slots: number | null; secondary_hit4_slots: number | null;
};
type AttributionRow = {
  model_kind: string; selected_slots: number | null; filled_slots: number | null;
  blocked_slots: number | null; unavailable_slots: number | null; no_attempt_slots: number | null;
  known_label_slots: number | null; known_hit5_slots: number | null;
  hit5_filled: number | null; hit5_blocked: number | null; hit5_unavailable: number | null;
  categories: AttributionCategory[] | null;
};
type Attribution = SavedSection & { rows: AttributionRow[] | null };
export type NativeEvidence = { quality: Quality | null; accounts: Accounts | null; attribution: Attribution | null };

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const number = (value: unknown, digits = 0) => finite(value) ? fmt(value, digits) : '不可用';
const rate = (value: unknown) => finite(value) ? `${fmt(value * 100, 2)}%` : '不可用';
const difference = (value: unknown) => finite(value) ? value.toExponential(2) : '不可用';
const time = (value: string | null | undefined) => value ? value.replace('T', ' ') : '未提供';
const model = (value: string) => value === 'lr' ? '逻辑回归（LR）' : value === 'lgbm' ? 'LightGBM' : value;
const STAGE_NAMES: Record<number, string> = { 1: '首板', 2: '二板', 3: '三板' };
const stage = (value: number) => STAGE_NAMES[value] || `阶段${value}`;
const text = (value: unknown, missing = '未提供') => value == null || value === '' ? missing
  : typeof value === 'object' ? JSON.stringify(value) : String(value);
const STATUS_NAMES: Record<string, string> = {
  execution_session_unavailable: '执行日尚无已提交记录', buy_order_filled: '准确执行日有买侧成交',
  buy_blocked: '有买侧受阻记录', no_buy_attempt_recorded: '未记录买侧动作',
};
const REASON_NAMES: Record<string, string> = {
  unresolved_blank_terms: '公司行动条款有空项，参考价无法确认',
  saved_native_execution_source_unresolved: '持仓或待执行目标的资料不完整',
  unsupported_action_source_cutoff: '已超出原行动资料覆盖日期',
};

function SourceDetails({ evidence, label }: { evidence: SavedSection | null; label: string }) {
  return <details className="rce-subdetail"><summary>{label}来源与实际保存时间</summary>
    <dl className="rce-facts"><dt>实际保存</dt><dd>{time(evidence?.saved_at)}</dd>
      <dt>保存结果</dt><dd>{evidence?.source ? <code className="rce-source">{evidence.source.path}</code> : '来源未提供'}</dd>
    </dl></details>;
}

function Unavailable({ evidence, label }: { evidence: SavedSection | null; label: string }) {
  return <p className="rce-message" role="status">{label}不可用：{text(evidence?.reason, '尚无可读取的保存证据')}。其他层的证据独立保留。</p>;
}

function QualityEvidence({ evidence }: { evidence: Quality | null }) {
  const saved = evidence?.status === 'saved';
  const rows = saved && Array.isArray(evidence.rows) ? evidence.rows : [];
  return <details className="offline-details rce-detail" data-native-quality>
    <summary><strong>一、模型能否排出差异</strong><span className="rce-summary-note">{saved ? '六个冻结模型 · 概率诊断' : '概率证据不可用'}</span></summary>
    {!saved ? <Unavailable evidence={evidence} label="概率诊断" /> : <>
      <p className="subtle rce-note">AP是平均精确率，用于衡量排序，不是命中率；Brier衡量概率误差，越小越好。比较对象是原训练样本的固定先验概率。排序有效也不代表概率已校准，更不能直接决定仓位。</p>
      <div className="window-summary rce-coverage">
        <span>源交易日 <strong>{number(evidence.coverage?.registered_days)}</strong></span>
        <span>不同事件 <strong>{number(evidence.coverage?.unique_events)}</strong></span>
        <span>成熟 / 未知标签 <strong>{number(evidence.coverage?.mature_events)} / {number(evidence.coverage?.unknown_events)}</strong></span>
        <span>不同证券 <strong>{number(evidence.coverage?.unique_symbols)}</strong></span>
      </div>
      {rows.length ? <div className="table-scroll"><table className="compact rce-table rce-native-quality-table" aria-label="原生连板六模型训练外概率诊断"><thead><tr>
        <th>模型 / 阶段</th><th>成熟 / 未知事件</th><th>到五板正例</th><th>基准发生率</th><th>AP（非命中率）</th><th>Brier</th><th>训练先验Brier</th><th>Brier差（模型−先验）</th>
      </tr></thead><tbody>{rows.map(row => <tr key={`${row.model_kind}:${row.stage}`}>
        <td>{model(row.model_kind)}<br />{stage(row.stage)}</td><td>{number(row.evaluable_rows)} / {number(row.unknown_rows)}</td>
        <td>{number(row.positive_rows)}</td><td>{rate(row.base_rate)}</td><td>{number(row.metrics?.AP, 5)}</td>
        <td>{number(row.metrics?.Brier, 6)}</td><td>{number(row.metrics?.training_prior_Brier, 6)}</td>
        <td>{difference(row.metrics?.Brier_minus_training_prior_Brier)}</td>
      </tr>)}</tbody></table></div> : <p className="rce-message">六模型明细未提供，不能解释为零分或模型无效。</p>}
      <p className="subtle rce-note">事件时期 {text(evidence.event_start)} 至 {text(evidence.event_end)}，标签见证截至 {text(evidence.label_witness_cutoff)}。模型实际可得时间：{time(evidence.model_available_at)}。这是事后完成的训练外诊断，没有当时已发表的记录；全池质量包含范围不完整日期的已知事件，不等于当天可选择的范围。</p>
    </>}
    <SourceDetails evidence={evidence} label="概率诊断" />
  </details>;
}

function AccountEvidence({ evidence }: { evidence: Accounts | null }) {
  const saved = evidence?.status === 'saved';
  const rows = saved && Array.isArray(evidence.rows) ? evidence.rows : [];
  return <details className="offline-details rce-detail" data-native-accounts>
    <summary><strong>二、固定规则进入账户后</strong><span className="rce-summary-note">{saved ? '两本独立研究账 · 完整收益未知' : '账户证据不可用'}</span></summary>
    {!saved ? <Unavailable evidence={evidence} label="研究账户" /> : <>
      <p className="subtle rce-note">两本账户各以10万元起步，沿原固定选择、每票16%目标及同一套撮合记账规则运行。它们停止在不同日期，未完成原主窗和退出尾期；不比较中途账面点值来挑选模型。</p>
      {rows.length ? <div className="table-scroll"><table className="compact rce-table rce-native-account-table" aria-label="原生连板两模型独立研究账户"><thead><tr>
        <th>模型</th><th>已提交会话</th><th>最后提交 / 停止日</th><th>模拟父委托 / 成交回调</th><th>已扣费用（元）</th><th>完整策略净收益</th><th>原缺项、残仓与待执行目标</th>
      </tr></thead><tbody>{rows.map(row => <tr key={row.model_kind}>
        <td>{model(row.model_kind)}</td><td>{number(row.committed_sessions)}</td>
        <td>{text(row.last_committed_day)}<br /><span className="subtle">停止：{text(row.blocked_day)}</span></td>
        <td>{number(row.parent_orders)} / {number(row.fill_callbacks)}</td><td>{number(row.fees, 2)}</td>
        <td>{row.complete_PnL_available === true && row.metrics ? rate(row.metrics.net_return) : '不可用'}</td>
        <td className="rce-wrap">
          {Array.isArray(row.source_issues) && row.source_issues.length ? <ul className="rce-native-issues">{row.source_issues.map((issue, index) => <li key={index}>
            {issue.symbol ? `${issue.symbol}：` : ''}{REASON_NAMES[issue.status || ''] || REASON_NAMES[issue.reason] || issue.reason}
          </li>)}</ul> : '原缺项未提供'}
          <details className="rce-row-detail"><summary>查看原残仓与待执行目标</summary><dl className="rce-facts">
            <dt>残仓股数</dt><dd>{text(row.residual_positions, '不可用，不能推为空仓')}</dd>
            <dt>待执行目标</dt><dd>{text(row.pending_targets, '原记录为空；不代表空仓')}</dd>
          </dl></details>
        </td>
      </tr>)}</tbody></table></div> : <p className="rce-message">两本账户明细未提供，不能解释为零持仓或零收益。</p>}
      <p className="subtle rce-note">一笔父委托可能有多个成交回调，回调数不是独立交易次数；已扣费用也覆盖不同运行前缀。目标受阻、未执行目标和残仓仍保留，不能因为已存在成交记录就视为策略已验证。</p>
    </>}
    <SourceDetails evidence={evidence} label="账户回放" />
  </details>;
}

function AttributionEvidence({ evidence }: { evidence: Attribution | null }) {
  const saved = evidence?.status === 'saved';
  const rows = saved && Array.isArray(evidence.rows) ? evidence.rows : [];
  return <details className="offline-details rce-detail" data-native-attribution>
    <summary><strong>三、选到五板票，当时能否买到</strong><span className="rce-summary-note">{saved ? '准确下一交易日的买侧记录' : '执行归因不可用'}</span></summary>
    {!saved ? <Unavailable evidence={evidence} label="执行归因" /> : <>
      <p className="subtle rce-note">先固定每个原入选槽位的准确下一交易日，再关联保存的标签。到五板且有买侧成交，不代表买在合适价位、后来盈利或完成退出；不同阶段也可能来自同一段连板行情。</p>
      {rows.length ? <>
        <div className="table-scroll"><table className="compact rce-table rce-native-hit-table" aria-label="入选五板事件的实际买侧记录"><thead><tr>
          <th>模型</th><th>原入选槽位</th><th>标签已知</th><th>已知到五板事件</th><th>其中有买侧成交</th><th>其中买侧受阻</th><th>其中执行日未覆盖</th>
        </tr></thead><tbody>{rows.map(row => <tr key={row.model_kind}>
          <td>{model(row.model_kind)}</td><td>{number(row.selected_slots)}</td><td>{number(row.known_label_slots)}</td>
          <td>{number(row.known_hit5_slots)}</td><td>{number(row.hit5_filled)}</td><td>{number(row.hit5_blocked)}</td><td>{number(row.hit5_unavailable)}</td>
        </tr>)}</tbody></table></div>
        <details className="rce-subdetail"><summary>保留每个模型全部入选槽位的状态与四板次要标签</summary>
          <div className="table-scroll"><table className="compact rce-table" aria-label="两模型全部入选槽位执行状态"><thead><tr>
            <th>模型</th><th>准确执行日的记录</th><th>入选槽位</th><th>已知 / 未知标签</th><th>到五板事件</th><th>到四板（次要）</th>
          </tr></thead><tbody>{rows.map(row => Array.isArray(row.categories) ? row.categories.map(category => <tr key={`${row.model_kind}:${category.execution_status}`}>
            <td>{model(row.model_kind)}</td><td>{STATUS_NAMES[category.execution_status] || category.execution_status}</td>
            <td>{number(category.selected_slots)}</td><td>{number(category.known_label_slots)} / {number(category.unknown_label_slots)}</td>
            <td>{number(category.hit5_slots)}</td><td>{number(category.secondary_hit4_slots)}</td>
          </tr>) : <tr key={row.model_kind}><td>{model(row.model_kind)}</td><td colSpan={5}>全部状态明细不可用</td></tr>)}</tbody></table></div>
        </details>
      </> : <p className="rce-message">归因明细未提供，不能解释为没有成交或没有到五板事件。</p>}
      <p className="subtle rce-note">执行日未覆盖属于未知；未记录买侧动作可能因为原本持仓或数量限制，都不算买入失败。同一槽位若同时有成交和受阻，两项原事实保留，主类别按成交记录。槽位数不是独立股票数、独立交易数或收益。</p>
    </>}
    <SourceDetails evidence={evidence} label="执行归因" />
  </details>;
}

export function NativeShortlineEvidence({ evidence }: { evidence: NativeEvidence | null }) {
  const quality = evidence?.quality;
  const rows = quality?.status === 'saved' && Array.isArray(quality.rows) ? quality.rows : [];
  const ranking = rows.length === 6 && new Set(rows.map(row => `${row.model_kind}:${row.stage}`)).size === 6
    && rows.every(row => ['lr', 'lgbm'].includes(row.model_kind) && [1, 2, 3].includes(row.stage)
      && finite(row.metrics?.AP) && finite(row.base_rate) && row.metrics!.AP! > row.base_rate);
  return <div className="rce-native" aria-label="原生连板模型三层研究证据">
    <h3>连板模型：从排序到实际成交</h3>
    <p className="rce-conclusion" data-native-conclusion>{ranking ? '存在排序信息；完整策略收益未知。' : '冻结模型与模拟执行证据，完整策略收益未知。'}<span className="subtle"> 三层证据分别展示；仅读保存研究证据，真实账户未下单。</span></p>
    <QualityEvidence evidence={quality || null} />
    <AccountEvidence evidence={evidence?.accounts || null} />
    <AttributionEvidence evidence={evidence?.attribution || null} />
  </div>;
}
