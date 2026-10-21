import { fmt } from '../types';

type Mode = 'cumulative' | 'same_watch_window_delta';
type Summary = { member_count: number; price_known_count: number; known_activity_count: number;
  unknown_activity_count: number; known_amount_cny: string | null; known_volume_shares: string | null;
  full_group_amount_cny: string | null; unknown_reason_counts?: Record<string, number> };
type Group = { id: string; name: string; member_count: number;
  current_summary: Summary | null; observation_summary: Summary | null };
export type MarketActivityContext = { schema: string; mode: Mode; status: string;
  contract_available_at?: string | null; derived_now?: string | null; read_at?: string;
  current_summary: Summary | null; observation_summary: Summary | null; groups: Group[]; archetype_groups: Group[];
  source_clocks?: { snapshot_started_at?: string; snapshot_response_received_at?: string;
    baseline_response_received_at?: string; latest_response_received_at?: string;
    source_day?: string; source_available_at?: string }; error?: string };
const names: Record<string, string> = { space_height_leader: '空间龙', relay_continuation_leader: '接力龙',
  trend_leader: '趋势龙', liquidity_capacity_proxy: '容量龙', cross_regime_candidate: '穿越龙候选',
  rebound_repair_candidate: '反包龙候选', sector_catchup_leader: '补涨龙', turnover_leader: '换手龙' };
const count = (value?: number | null) => value == null || !Number.isFinite(value) ? '待核验' : fmt(value, 0);
// The exact Decimal strings remain server evidence. Number is used only for display units.
const amount = (value?: string | null) => value == null || !Number.isFinite(Number(value)) ? '待核验' : fmt(Number(value) / 1e8, 3);
const volume = (value?: string | null) => value == null || !Number.isFinite(Number(value)) ? '待核验' : fmt(Number(value) / 1e4, 2);
const fullAmount = (value?: Summary | null) => value && value.member_count > 0 && value.unknown_activity_count === 0 ? amount(value.full_group_amount_cny) : '待核验';

function ActivitySummary({ value, delta }: { value: Summary; delta: boolean }) {
  return <>
    <div className="window-summary"><span>完整名册分母 <strong>{count(value.member_count)}</strong></span><span>{delta ? '价格配对已知' : '价格已知'} <strong>{count(value.price_known_count)}</strong></span><span>量额已知 / 未知 <strong>{count(value.known_activity_count)} / {count(value.unknown_activity_count)}</strong></span><span>已知子集{delta ? '新增' : '累计'}成交额（亿元） <strong title={value.known_amount_cny || undefined}>{value.known_activity_count ? amount(value.known_amount_cny) : '待核验'}</strong></span><span>已知子集成交量（万股） <strong title={value.known_volume_shares || undefined}>{value.known_activity_count ? volume(value.known_volume_shares) : '待核验'}</strong></span></div>
    <p className="subtle">完整名册{delta ? '区间新增' : '累计'}成交额（亿元）：<strong>{fullAmount(value)}</strong>。有未知成员时仅显示已知子集合计，不能说成完整名册或全市场总额。</p>
  </>;
}

function ActivityGroups({ groups, current, archetypes, delta }: { groups: Group[]; current: boolean; archetypes?: boolean; delta: boolean }) {
  return <details className="offline-details"><summary>{archetypes ? `原590龙型量额分组 · ${groups.length} 类` : `名册背景量额分组 · ${groups.length} 组`}</summary>
    <p className="subtle">{current ? '本次读取时仍有效的研究子集。' : '以下为保存的历史量额，不代表当前。'}{archetypes ? '标签沿用原590登记，多标签人数不得相加；空组表示没有已知登记匹配。' : '前日ST等背景与590、其余成员分组重叠，人数不能相加。'}空组和资料未知均不填零，不按成交额排名。</p>
    <div className="table-scroll"><table className="compact" aria-label={archetypes ? '原龙型量额分布' : '名册背景量额分布'}><thead><tr><th>{archetypes ? '登记龙型' : '固定背景组'}</th><th>完整人数</th><th>{delta ? '价格配对已知' : '价格已知'}</th><th>量额已知 / 未知</th><th>已知子集{delta ? '新增' : '累计'}额（亿元）</th><th>已知子集量（万股）</th><th>全组额（亿元）</th></tr></thead><tbody>{groups.map(group => {
      const value = current ? group.current_summary : group.observation_summary;
      return <tr key={group.id}><td>{archetypes ? names[group.id] || group.name : group.name}</td><td>{count(group.member_count)}</td><td>{count(value?.price_known_count)}</td><td>{value ? `${count(value.known_activity_count)} / ${count(value.unknown_activity_count)}` : '待核验'}</td><td title={value?.known_amount_cny || undefined}>{value?.known_activity_count ? amount(value.known_amount_cny) : '待核验'}</td><td title={value?.known_volume_shares || undefined}>{value?.known_activity_count ? volume(value.known_volume_shares) : '待核验'}</td><td>{fullAmount(value)}</td></tr>;
    })}</tbody></table></div>
  </details>;
}

export function MarketRosterActivity({ value, current, mode }: { value?: MarketActivityContext | null; current: boolean; mode: Mode }) {
  const delta = mode === 'same_watch_window_delta';
  const available = value?.schema === 'market_roster_research_activity_context_v1'
    && value.mode === mode && value.status === 'observed_research_quantity_activity';
  const currentSummary = available && current ? value.current_summary : null;
  const historical = available ? value.observation_summary : null;
  const summary = currentSummary || historical;
  return <details className="offline-details" aria-label={delta ? '宽名册区间量额观察' : '宽名册累计量额观察'}>
    <summary>{delta ? '同一窗口成交量额增量' : '当日累计成交量额'} · {summary ? currentSummary ? '读取时有效的研究子集' : '保存历史量额，非当前' : '量额资料待核验'}</summary>
    {summary ? <>
      <ActivitySummary value={summary} delta={delta} />
      {currentSummary && value?.observation_summary && <details className="offline-details"><summary>保存观察时量额 · 不代表当前</summary><ActivitySummary value={value.observation_summary} delta={delta} /></details>}
      {!!value?.groups.length && <ActivityGroups groups={value.groups} current={!!currentSummary} delta={delta} />}
      {!!value?.archetype_groups.length && <ActivityGroups groups={value.archetype_groups} current={!!currentSummary} archetypes delta={delta} />}
    </> : <p role="status">本次量额资料不可用或尚未形成，保持待核验；不回退到旧量额，不影响独立的价格观察。</p>}
    <p className="subtle">{delta ? '区间新增量额来自同一证券前后累计量额之差，不是资金流入差；昨收修订、累计数回退或口径不兼容时保留未知。' : '累计量额来自各证券自己的报价源时点，分批接收不代表共同瞬时。'}这是成交活动的研究口径，不是真实换手率、净流入、封板承接或参与者意图；不据此排名、选仓或生成交易。</p>
    <p className="subtle">已知小计会随有效报价覆盖变化，不能把跨刷新小计差当作成交额增量；增量只看同一窗口内已配对成员。</p>
    <p className="subtle">量额研究口径实际确认 {value?.contract_available_at || '待核验'}；本次实际衍生 {value?.derived_now || '待核验'}。保存报价的早期时点不表示当时已经发布这一口径的信号。</p>
    {value?.source_clocks && <details className="offline-details"><summary>量额观察来源时间</summary><p className="subtle">名册来源日 {value.source_clocks.source_day || '待核验'}，来源实际可得 {value.source_clocks.source_available_at || '待核验'}；{delta ? `窗口响应 ${value.source_clocks.baseline_response_received_at || '待核验'} 至 ${value.source_clocks.latest_response_received_at || '待核验'}` : `采集开始 ${value.source_clocks.snapshot_started_at || '待核验'}，响应接收 ${value.source_clocks.snapshot_response_received_at || '待核验'}`}。这些接收时间不能替代各证券自己的源报价时间。</p></details>}
  </details>;
}
