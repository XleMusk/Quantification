import { fmt } from '../types';

type SavedRef = { path: string; sha256: string };
type SavedOrder = {
  order_id?: string; id?: string; symbol?: string; side?: string; qty?: number | null;
  filled?: number | null; limit_price?: number | null; price_unit?: string;
  fees?: number | null; status?: string; reason?: string | null;
};
type SavedFill = {
  fill_id?: string; id?: string; order_id?: string; qty?: number | null;
  price?: number | null; price_unit?: string; fees?: number | null; available_day?: string | null;
};
type ReasonEvidence = {
  source?: string; mechanical_reason?: string | null; reason?: string | null;
  constraint_flags?: string[] | null;
};
type ReconciliationRow = {
  symbol: string; status?: string; planned_target_quantity: number | null;
  actual_engine_target_quantity: number | null; target_quantity_difference: number | null;
  reference_close_cents: number | null; recorded_engine_mark_cents: number | null;
  reference_price_differs?: boolean | null; price_difference_is_causal_attribution?: boolean;
  parent_order_count: number | null; fill_callback_count: number | null; fee_cents: number | null;
  orders?: SavedOrder[] | null; fills?: SavedFill[] | null; reason_evidence?: ReasonEvidence[] | null;
};
export type PlanExecutionReconciliation = {
  status: string; reason?: string | null; read_at?: string; preview?: string;
  source_run_directory?: string; execution_day?: string | null; last_saved_session?: string | null;
  plan_payload_sealed?: boolean; rows?: ReconciliationRow[] | null;
  plan_identity?: {
    case_id?: string; source_day?: string | null; execution_day?: string | null;
    target_version?: number | null; publication?: SavedRef;
  } | null;
  cash?: { plan_D_balance_cents: number | null; E_saved_balance_rmb: number | null;
    D_to_E_change_is_trade_only?: boolean; cash_bridge_not_calculated?: boolean } | null;
};
export type PlanReconciliationHistory = {
  status: string; reason?: string | null; saved_plan_count?: number | null;
  history_basis?: string; last_executed_plan_reconciliation?: PlanExecutionReconciliation | null;
};

const number = (value: number | null | undefined, decimals = 0) =>
  typeof value === 'number' && Number.isFinite(value) ? fmt(value, decimals) : '待核';
const money = (value: number | null | undefined) =>
  typeof value === 'number' && Number.isFinite(value) ? fmt(value / 100, 2) : '待核';
const quantityDifference = (value: number | null | undefined) =>
  typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${fmt(value)}` : '待核';
const price = (value: number | null | undefined, unit: string | undefined) =>
  typeof value !== 'number' || !Number.isFinite(value) ? '待核'
    : unit === 'cent' ? fmt(value / 100, 2) : unit === 'mill' ? fmt(value / 1000, 3) : `${value}（单位待核）`;
const statusNames: Record<string, string> = {
  pending: '等待执行日记录', reconciled_saved_execution: '已对应保存的执行记录', unknown: '对账待核',
};
const reason = (value: string | null | undefined) => {
  if (value === 'exact_E_Engine_session_not_saved') return '尚未保存准确执行日的模拟账本，当前没有可对照的成交记录。';
  if (!value) return '尚未保存能准确对应此预案的证据。';
  const raw = value.toLowerCase();
  if (raw.includes('legacy plan quantities') || raw.includes('not sealed before execution')) return '旧预案缺少执行前的数量校验记录，不能进行事后数量对账。';
  if (raw.includes('not saved before its exact')) return '预案保存时间未能证明早于对应执行日开盘，暂不能对账。';
  if (raw.includes('context') || raw.includes('another research run')) return '预案与执行记录的账户、日期或版本未能准确对应。';
  if (raw.includes('target version')) return '预案目标版本与保存的执行记录不一致，暂不能对账。';
  if (raw.includes('published target receipt') || raw.includes('consumption') || raw.includes('receipt')) return '尚不能确认执行记录使用了这份原预案，暂不能对账。';
  if (raw.includes('publication')) return '原发表记录与这份预案未能准确对应，暂不能对账。';
  if (raw.includes('bytes changed') || raw.includes('binding changed')) return '保存记录的来源校验未通过，暂不能对账。';
  return '保存记录未能准确对应此预案，请展开来源查看具体缺项。';
};

function SavedCallbacks({ row }: { row: ReconciliationRow }) {
  return <details><summary>查看此票的原委托与回调</summary>
    {row.orders == null ? <p className="subtle">原父委托明细待核。</p>
      : row.orders.length === 0 ? <p className="subtle">此票保存的父委托为0笔。</p>
        : <div className="table-scroll"><table className="compact" aria-label={`${row.symbol}对账原父委托`}><thead><tr>
          <th>父委托 / 方向</th><th>委托 / 成交股数</th><th>限价（元）</th><th>累计费用（元）</th><th>原状态 / 原因</th>
        </tr></thead><tbody>{row.orders.map((order, index) => <tr key={`${order.order_id ?? order.id ?? index}`}>
          <td className="opening-ledger-id">{order.order_id ?? order.id ?? '待核'}<br />{order.side === 'buy' ? '买入' : order.side === 'sell' ? '卖出' : '方向待核'}</td>
          <td>{number(order.qty)} / {number(order.filled)}</td><td>{price(order.limit_price, order.price_unit)}</td>
          <td>{money(order.fees)}</td><td className="opening-daily-wrapped">{order.status ?? '待核'}<br />{order.reason ?? '原记录未列原因'}</td>
        </tr>)}</tbody></table></div>}
    {row.fills == null ? <p className="subtle">原成交回调明细待核。</p>
      : row.fills.length === 0 ? <p className="subtle">此票保存的成交回调为0条。</p>
        : <div className="table-scroll"><table className="compact" aria-label={`${row.symbol}对账原成交回调`}><thead><tr>
          <th>回调 / 父委托</th><th>股数</th><th>成交价（元）</th><th>本回调增量费用（元）</th><th>可用日期</th>
        </tr></thead><tbody>{row.fills.map((fill, index) => <tr key={`${fill.fill_id ?? fill.id ?? index}`}>
          <td className="opening-ledger-id">{fill.fill_id ?? fill.id ?? '待核'}<br />{fill.order_id ?? '待核'}</td>
          <td>{number(fill.qty)}</td><td>{price(fill.price, fill.price_unit)}</td><td>{money(fill.fees)}</td><td>{fill.available_day ?? '待核'}</td>
        </tr>)}</tbody></table></div>}
  </details>;
}

function SavedPlan({ label, slot, value }: {
  label: string; slot: 'latest' | 'executed'; value: PlanExecutionReconciliation | null | undefined;
}) {
  const reconciled = value?.status === 'reconciled_saved_execution';
  return <section aria-label={label} data-testid={`opening-plan-${slot}`} data-reconciliation-status={value?.status ?? 'not_saved'}>
    <h5>{label} · {value ? statusNames[value.status] ?? '状态待核' : '未保存'}</h5>
    {value ? <>
      <p className="subtle">原计划 {value.plan_identity?.source_day ?? '待核'} → 执行 {value.plan_identity?.execution_day ?? value.execution_day ?? '待核'}；目标版本 {number(value.plan_identity?.target_version)}。</p>
      {value.plan_payload_sealed === false ? <p className="window-stale" data-testid={`opening-plan-${slot}-unsealed`}>旧预案未保存执行前的数量校验记录，不能补写后冒充原记录。</p>
        : value.plan_payload_sealed == null ? <p className="subtle">预案数量封存状态待核。</p> : null}
      {!reconciled ? <p className={value.status === 'pending' ? 'subtle' : 'window-stale'} role="status">{reason(value.reason)}</p>
        : value.rows == null ? <p className="window-stale">已对应执行记录，但逐票对账行未提供，数量和费用仍待核。</p>
          : <>
            <p className="subtle opening-daily-case-note">参考目标来自原预案；引擎目标与委托、回调来自准确执行日。目标差数为“引擎目标减参考目标”，参考价不同只表示保存值不同，不能据此认定差数原因。成交回调条数不是独立交易笔数。</p>
            {value.rows.length === 0 ? <p className="subtle">这份准确对账保存了0条证券行，不据此补零收益。</p>
              : <div className="table-scroll"><table className="compact opening-sizing-table" aria-label={`${label}逐票数量与执行证据`}><thead><tr>
                <th>证券</th><th>参考目标 / 引擎目标（股）</th><th>目标差数（股）</th><th>参考价 / 引擎标记（元）</th><th>父委托 / 回调条数</th><th>回调增量费用合计（元）</th><th>保存的原因与约束</th>
              </tr></thead><tbody>{value.rows.map((row, index) => <tr key={`${row.symbol}-${index}`} data-reconciliation-symbol={row.symbol}>
                <td>{row.symbol}</td><td>{number(row.planned_target_quantity)} / {number(row.actual_engine_target_quantity)}</td>
                <td data-reconciliation-field="quantity-difference">{quantityDifference(row.target_quantity_difference)}</td>
                <td>{money(row.reference_close_cents)} / {money(row.recorded_engine_mark_cents)}<br /><span className="subtle">{row.reference_price_differs === true ? '保存价格不同，原因未归因' : row.reference_price_differs === false ? '保存价格相同' : '保存价格待核'}</span></td>
                <td>{number(row.parent_order_count)} / {number(row.fill_callback_count)}<SavedCallbacks row={row} /></td>
                <td>{money(row.fee_cents)}</td>
                <td className="opening-daily-wrapped">{row.reason_evidence == null ? '原原因证据待核' : row.reason_evidence.length === 0 ? '未保存数量计算或受阻原因，不能反推原因'
                  : row.reason_evidence.map((evidence, evidenceIndex) => <div key={evidenceIndex}>{evidence.mechanical_reason ?? evidence.reason ?? '原记录未列具体原因'}
                    {evidence.constraint_flags != null && evidence.constraint_flags.length > 0 ? <><br /><span className="subtle">{evidence.constraint_flags.join('；')}</span></> : null}
                  </div>)}</td>
              </tr>)}</tbody></table></div>}
            {value.cash ? <>
              <dl className="opening-daily-facts"><div><dt>计划来源日现金余额（元）</dt><dd>{money(value.cash.plan_D_balance_cents)}</dd></div>
                <div><dt>执行日保存现金余额（元）</dt><dd>{number(value.cash.E_saved_balance_rmb, 2)}</dd></div></dl>
              <p className="subtle">本接口未拆分现金余额变动来源，不能把两日差额当作纯交易盈亏。</p>
            </> : <p className="subtle">两日现金余额待核。</p>}
          </>}
      <details className="offline-details"><summary>查看此计划的对账来源</summary><dl className="opening-daily-case-audit">
        <dt>原预案</dt><dd>{value.preview ?? '待核'}</dd><dt>独立研究账户目录</dt><dd>{value.source_run_directory ?? '待核'}</dd>
        <dt>原发表文件</dt><dd>{value.plan_identity?.publication?.path ?? '待核'}</dd>
        <dt>对账读取时间</dt><dd>{value.read_at ?? '待核'}</dd><dt>原状态 / 缺项</dt><dd>{value.status} / {value.reason ?? '无'}</dd>
      </dl></details>
    </> : <p className="subtle">尚无保存的{slot === 'latest' ? '最新预案对账观察' : '已执行计划对账'}，不能用另一份计划补齐。</p>}
  </section>;
}

export function OpeningPlanReconciliation({ latest, executed, history }: {
  latest: PlanExecutionReconciliation | null | undefined;
  executed: PlanExecutionReconciliation | null | undefined;
  history: PlanReconciliationHistory | null | undefined;
}) {
  return <section aria-label="保存预案与实际模拟执行对账" data-testid="opening-plan-reconciliation">
    <h4>保存预案与实际模拟执行对账</h4>
    <p className="subtle opening-daily-case-note">最新待执行预案与最近已执行计划分开读取。这里只展示已经保存的研究模拟证据，刷新不会运行策略或提交委托。</p>
    <SavedPlan label="最新保存预案" slot="latest" value={latest} />
    <SavedPlan label="最近已执行计划对账" slot="executed" value={executed} />
    {history && history.status !== 'saved_execution_plan_selected' ? <p className={history.status === 'unknown' ? 'window-stale' : 'subtle'} data-testid="opening-plan-history-status">
      {history.status === 'no_saved_plan_with_execution_session' ? '现有保存历史中暂无能准确对应执行日的计划。' : `计划历史对应仍待核：${reason(history.reason)}`}
    </p> : null}
  </section>;
}
