export type IndicatorCategory = '趋势与通道' | '动量与震荡' | '量价分析' | '波动与风险' | '统计与相对强弱';
export type IndicatorParam = { key: string; label: string; default: number; min: number; max: number; step: number };
export type IndicatorDefinition = {
  id: string; name: string; category: IndicatorCategory; description: string; formula: string; caveat: string;
  sources: string[]; params: IndicatorParam[]; pane: 'price' | 'normal'; native: boolean; requires?: 'benchmark' | 'turnover';
};
export type IndicatorInstance = { id: string; key: string; params: number[]; visible: boolean };
export type IndicatorPeriod = 'D' | 'W' | 'M';
export type IndicatorContext = { hasBenchmark: boolean; hasTurnover: boolean; period: string; barCount: number };
const KLINE = 'https://v9.klinecharts.com/en-US/guide/indicator';
const TALIB = 'https://ta-lib.org/functions/';
const SHARPE = 'https://web.stanford.edu/~wfsharpe/art/sr/sr.htm';
const NIST = 'https://www.itl.nist.gov/div898/handbook/';
const nativeCaveat = '由 KLineCharts 9.8.12 原生实现计算；初值、平滑和缺口处理可能与其他终端不同，比较前需核对口径。';
const researchCaveat = '采用当前及过去已观测柱计算；预热不足或分母无效时留空。公开公式用于研究观察，不代表策略收益。';
const annualCaveat = '每年柱数默认 252，对应日线假设；周线、月线应调整该参数。滚动统计不是已扣成本的账户绩效。';
const p = (label: string, value: number, max = 1000, min = 1, step = 1): IndicatorParam => ({ key: label, label, default: value, min, max, step });
const windows = (values: number[], labels?: string[]) => values.map((v, i) => p(labels?.[i] ?? `周期 ${i + 1}`, v));
function native(id: string, name: string, category: IndicatorCategory, values: number[], description: string, formula: string, pane: 'price' | 'normal' = 'normal', labels?: string[], requires?: 'benchmark' | 'turnover'): IndicatorDefinition {
  return { id, name, category, description, formula, caveat: nativeCaveat, sources: [KLINE], params: windows(values, labels), pane, native: true, requires };
}
function custom(id: string, name: string, category: IndicatorCategory, params: IndicatorParam[], description: string, formula: string, options: Partial<Pick<IndicatorDefinition, 'pane' | 'requires' | 'caveat' | 'sources'>> = {}): IndicatorDefinition {
  return { id, name, category, params, description, formula, caveat: researchCaveat, sources: [TALIB], pane: 'normal', native: false, ...options };
}
export const CATALOG: IndicatorDefinition[] = [
  native('MA', 'MA 简单均线', '趋势与通道', [5, 10, 30, 60], '观察不同周期的价格趋势。', '每条线为最近 N 个收盘价的算术平均。', 'price'),
  native('EMA', 'EMA 指数均线', '趋势与通道', [6, 12, 20], '对近期价格给予更高权重。', 'EMA 递推平滑，权重 α = 2 / (N + 1)。', 'price'),
  native('SMA', 'SMA 原生平滑线', '趋势与通道', [12, 2], '使用组件的递推平滑口径。', '预热为 N 柱均值；后续 = [M×C + (N−M+1)×上一值] / (N+1)。', 'price', ['窗口 N', '平滑权重 M']),
  native('BBI', 'BBI 多空指数', '趋势与通道', [3, 6, 12, 24], '组合四条均线观察中短期趋势。', '四个周期 MA 的平均值。', 'price'),
  native('BOLL', 'BOLL 布林带', '趋势与通道', [20, 2], '观察价格与滚动均值及波动带的位置。', '中轨 MA；上下轨 = MA ± 倍数×总体标准差。', 'price', ['周期', '标准差倍数']),
  native('SAR', 'SAR 抛物线转向', '趋势与通道', [2, 2, 20], '观察随趋势移动的价格参考点。', 'SAR 随极值点及加速因子递推；三个参数按百分数解释。', 'price', ['初始因子 %', '步长 %', '最大因子 %']),
  native('VOL', 'VOL 成交量', '量价分析', [5, 10, 20], '观察成交活跃程度及量均线。', '原始柱成交量与各周期成交量均线。'),
  native('AVP', 'AVP 累计成交均价', '量价分析', [], '从已载入起点累计成交额和成交量。', 'Σ成交额 / Σ成交量；不是点击锚定 VWAP。', 'price', [], 'turnover'),
  native('MACD', 'MACD 趋势动量', '动量与震荡', [12, 26, 9], '观察快慢指数均线差与平滑信号。', 'DIF = EMA快 − EMA慢；DEA = EMA(DIF)；柱 = 2×(DIF−DEA)。', 'normal', ['快线', '慢线', '信号线']),
  native('RSI', 'RSI 相对强弱', '动量与震荡', [6, 12, 24], '观察上涨与下跌幅度的相对关系。', '按原生递推上涨幅及绝对涨跌幅比值输出 0—100 指数。'),
  native('KDJ', 'KDJ 随机指标', '动量与震荡', [9, 3, 3], '观察收盘价在近期高低区间中的位置。', 'RSV = (C−LLV)/(HHV−LLV)×100；K、D 递推；J = 3K−2D。', 'normal', ['区间周期', 'K 平滑', 'D 平滑']),
  native('CCI', 'CCI 商品通道', '动量与震荡', [20], '观察典型价格偏离均值的程度。', '典型价与其均值之差 / (0.015×平均绝对偏差)。'),
  native('WR', 'WR 威廉指标', '动量与震荡', [6, 10, 14], '观察收盘价距离近期最高价的位置。', '(最高价−收盘价)/(最高价−最低价)×100，使用原生正值显示。'),
  native('MTM', 'MTM 动量', '动量与震荡', [12, 6], '观察价格变化量及平滑结果。', 'C − N 柱前收盘价；另绘 M 周期均线。', 'normal', ['比较周期', '平滑周期']),
  native('ROC', 'ROC 变动率', '动量与震荡', [12, 6], '观察一段窗口的价格变化百分比。', '(C/C[N]−1)×100；附加平滑线。', 'normal', ['比较周期', '平滑周期']),
  native('BIAS', 'BIAS 乖离率', '动量与震荡', [6, 12, 24], '观察价格偏离各周期均线的程度。', '(C/MA(N)−1)×100。'),
  native('AO', 'AO 动量震荡', '动量与震荡', [5, 34], '观察中间价快慢均线差。', 'SMA((H+L)/2,快) − SMA((H+L)/2,慢)。', 'normal', ['快窗口', '慢窗口']),
  native('DMI', 'DMI 方向与 ADX', '趋势与通道', [14, 6], '观察正负方向运动与趋势强度。', '+DI、−DI、ADX、ADXR；由真实波幅及方向运动递推。', 'normal', ['方向窗口', 'ADX 平滑']),
  native('DMA', 'DMA 平均差', '趋势与通道', [10, 50, 10], '观察快慢简单均线差及信号。', 'MA快−MA慢，再对差值作 M 周期均线。', 'normal', ['快窗口', '慢窗口', '平滑窗口']),
  native('TRIX', 'TRIX 三重指数', '趋势与通道', [12, 9], '对价格作三重指数平滑后观察变化率。', '三重 EMA 的单柱变化百分比，附加均线。', 'normal', ['平滑周期', '信号周期']),
  native('OBV', 'OBV 能量潮', '量价分析', [30], '按收盘涨跌方向累计成交量。', '上涨加量、下跌减量，平盘不变；附加 MA。'),
  native('PVT', 'PVT 价量趋势', '量价分析', [], '将价格收益率与成交量结合累计。', 'PVT[t] = PVT[t−1] + V[t]×(C[t]/C[t−1]−1)。'),
  native('VR', 'VR 成交量比率', '量价分析', [26, 6], '比较上涨、下跌与平盘柱的成交量。', '(上涨量+平盘量/2)/(下跌量+平盘量/2)×100；附加平滑线。', 'normal', ['窗口', '平滑窗口']),
  native('EMV', 'EMV 简易波动', '量价分析', [14, 9], '观察中间价移动与成交量的关系。', '中间价变化×振幅/成交量，按原生缩放和平滑。', 'normal', ['窗口', '信号窗口']),
  native('BRAR', 'BRAR 意愿人气', '动量与震荡', [26], '比较开盘与前收盘参考下的高低价关系。', 'AR = Σ(H−O)/Σ(O−L)×100；BR 使用前收盘差值。'),
  native('PSY', 'PSY 心理线', '动量与震荡', [12, 6], '统计窗口中的上涨柱比例。', '上涨柱数/N×100，附加 M 周期均线。', 'normal', ['统计窗口', '平滑窗口']),
  native('CR', 'CR 中间意愿', '动量与震荡', [26, 10, 20, 40, 60], '观察相对于前一柱中间价的高低价力量。', '原生 CR 比较 H 与前中间价的正差、前中间价与 L 的正差，并绘平滑线。'),
  custom('Q_ATR', 'ATR 真实波幅', '波动与风险', windows([14], ['Wilder 周期']), '衡量含跳空的价格振幅。', 'TR = max(H−L, |H−前C|, |L−前C|)，ATR 为 Wilder 平滑。'),
  custom('Q_NATR', 'NATR 标准化波幅', '波动与风险', windows([14]), '将 ATR 转换成可跨价格尺度比较的百分数。', '100×ATR(N)/C。'),
  custom('Q_SUPERTREND', 'Supertrend 波动趋势线', '趋势与通道', [p('ATR 周期', 10), p('ATR 倍数', 3, 20, .1, .1)], '用 ATR 带及价格穿越维护趋势参考线。', 'HL2±倍数×ATR；递推上下带和趋势方向。', { pane: 'price' }),
  custom('Q_DONCHIAN', 'Donchian 唐奇安通道', '趋势与通道', windows([20]), '显示当前与过去窗口的价格区间。', '上轨 max(H,N)，下轨 min(L,N)，中轨为二者均值。', { pane: 'price', caveat: '含当前柱。用于突破信号时应与前一柱通道比较；不是已经产生的交易信号。' }),
  custom('Q_KELTNER', 'Keltner 波动通道', '趋势与通道', [p('EMA 周期', 20), p('ATR 周期', 10), p('ATR 倍数', 2, 20, .1, .1)], '围绕指数均线显示真实波幅带。', 'EMA(C,N) ± 倍数×ATR(M)。', { pane: 'price' }),
  custom('Q_STOCHRSI', 'StochRSI 随机强弱', '动量与震荡', windows([14, 14, 3, 3], ['RSI 周期', '随机窗口', 'K 平滑', 'D 平滑']), '观察 RSI 在自身滚动范围中的位置。', '100×(RSI−minRSI)/(maxRSI−minRSI)，再作 K、D 均线。'),
  custom('Q_CMF', 'CMF 蔡金资金流', '量价分析', windows([20]), '结合收盘位置与成交量观察量价压力。', 'Σ[((2C−H−L)/(H−L))×V]/ΣV。', { caveat: '基于 OHLCV 的量价代理，不是真实主动买卖资金流。H=L 时乘数置 0，窗口零量留空。' }),
  custom('Q_MFI', 'MFI 资金流量指数', '量价分析', windows([14]), '比较典型价格上涨和下跌时的价量乘积。', 'TP=(H+L+C)/3；正负 TP×V 比值变换到 0—100。', { caveat: '典型价×量是公式的代理量，不是供应商成交额或真实资金流。' }),
  custom('Q_CHAIKIN', 'Chaikin 蔡金震荡', '量价分析', windows([3, 10], ['快 EMA', '慢 EMA']), '观察累积派发线的快慢均线差。', 'ADL 累计收盘位置乘量；EMA快(ADL)−EMA慢(ADL)。', { caveat: 'ADL 是 OHLCV 代理，不是逐笔主买主卖数据。' }),
  custom('Q_FORCE', 'Force Index 劲道指数', '量价分析', windows([13], ['EMA 周期']), '结合收盘变化量与成交量。', 'EMA((C−前C)×V,N)。'),
  custom('Q_RVOL', 'RVOL 相对成交量', '量价分析', windows([20], ['过去窗口']), '比较当前量与此前平均量。', '当前 V / 前 N 柱成交量均值；基准不含当前柱。', { caveat: '非同一时刻的日内季节性 RVOL；零基准量留空。' }),
  custom('Q_BBP', '布林带 %B', '动量与震荡', [p('周期', 20), p('标准差倍数', 2, 10, .1, .1)], '将价格位置标准化到布林带区间。', '(C−下轨)/(上轨−下轨)；通常 0—1，允许超出。'),
  custom('Q_BBW', '布林带宽度', '波动与风险', [p('周期', 20), p('标准差倍数', 2, 10, .1, .1)], '观察波动带的扩张与收缩。', '100×(上轨−下轨)/中轨。'),
  custom('Q_ER', 'Kaufman 效率比', '趋势与通道', windows([10]), '比较净位移与路径总长度。', '|C−C[N]| / Σ|逐柱价格变化|。'),
  custom('Q_HVOL', '历史实现波动率', '波动与风险', [p('收益窗口', 20, 1000, 2), p('每年柱数', 252, 100000)], '观察对数收益的滚动年化波动。', '样本标准差(log(C/前C))×√每年柱数×100。', { caveat: annualCaveat, sources: [NIST] }),
  custom('Q_PARKINSON', 'Parkinson 区间波动', '波动与风险', [p('窗口', 20), p('每年柱数', 252, 100000)], '使用最高最低价估计区间波动。', '√[mean(log(H/L)²)/(4ln2)×每年柱数]×100。', { caveat: `${annualCaveat} 对连续无跳跃价格过程的假设较强。`, sources: ['https://doi.org/10.1086/296071'] }),
  custom('Q_GK', 'Garman–Klass 波动', '波动与风险', [p('窗口', 20), p('每年柱数', 252, 100000)], '使用开高低收估计波动。', '√[mean(0.5ln(H/L)²−(2ln2−1)ln(C/O)²)×每年柱数]×100。', { caveat: `${annualCaveat} 不包含隔夜跳空的完整风险。`, sources: ['https://doi.org/10.1086/296072'] }),
  custom('Q_SKEW', '收益偏度', '统计与相对强弱', [p('收益窗口', 60, 1000, 3)], '观察收益分布的左右不对称。', '对数收益的调整 Fisher–Pearson 样本偏度；要求至少 3 个收益。', { sources: [NIST] }),
  custom('Q_ZSCORE', '价格 Z-score', '统计与相对强弱', windows([20], ['窗口']), '观察价格距滚动均值的标准差倍数。', '(C−mean(C,N))/总体标准差(C,N)。', { sources: [NIST], caveat: '价格通常非平稳；Z-score 不是已证明的均值回复机会，也不是 p 值。' }),
  custom('Q_RETURNZ', '收益 Z-score', '统计与相对强弱', windows([20], ['收益窗口']), '观察最新对数收益相对近期分布的位置。', '(最新 log 收益−窗口均值)/总体标准差。', { sources: [NIST] }),
  custom('Q_SLOPE', '回归斜率 %/柱', '统计与相对强弱', windows([20], ['回归窗口']), '观察价格线性趋势的归一化速度。', '窗口 OLS(C~柱序号)斜率/窗口平均价格×100。', { sources: [NIST] }),
  custom('Q_R2', '价格回归 R²', '统计与相对强弱', windows([20], ['回归窗口']), '观察窗口内价格对线性趋势的拟合程度。', 'OLS 含截距的 R²；不是未来方向预测概率。', { sources: [NIST] }),
  custom('Q_DRAWDOWN', '滚动峰值回撤 %', '波动与风险', windows([60], ['高点窗口']), '观察收盘价距离滚动峰值的幅度。', '(C/max(C,N)−1)×100。', { sources: [NIST], caveat: '价格序列回撤，非账户净值回撤；未计交易费用、分红及仓位。' }),
  custom('Q_SHARPE', '滚动 Sharpe', '波动与风险', [p('收益窗口', 60, 1000, 2), p('每年柱数', 252, 100000)], '观察资产收益相对波动的比例。', 'mean(简单收益)/样本标准差×√每年柱数；无风险收益设 0。', { sources: [SHARPE], caveat: `${annualCaveat} 无风险收益为 0；不是回测 Sharpe。` }),
  custom('Q_SORTINO', '滚动 Sortino', '波动与风险', [p('收益窗口', 60), p('每年柱数', 252, 100000)], '观察资产收益相对下行波动的比例。', 'mean(简单收益)/√mean(min(r,0)²)×√每年柱数。', { sources: ['https://doi.org/10.3905/joi.3.3.59'], caveat: `${annualCaveat} 目标收益为 0，下行平方和按全部窗口样本数归一。` }),
  custom('Q_BETA', '滚动 Beta', '统计与相对强弱', windows([60], ['共同收益窗口']), '观察证券对所选基准的线性收益敏感度。', 'cov(r资产,r基准)/var(r基准)，简单收益与含截距口径。', { requires: 'benchmark', sources: [NIST], caveat: '仅用证券与基准共有且前序时间戳一致的收益；不跨基准缺口计算。' }),
  custom('Q_CORR', '滚动基准相关性', '统计与相对强弱', windows([60], ['共同收益窗口']), '观察证券与基准的收益共同变化。', '简单收益的滚动 Pearson 相关系数。', { requires: 'benchmark', sources: [NIST], caveat: '仅对齐共同收益；相关性不是因果关系，零方差留空。' }),
  custom('Q_RESIDUALZ', '回归残差 Z-score', '统计与相对强弱', windows([60], ['共同收益窗口']), '观察最新收益相对基准回归的残差位置。', '滚动 OLS(r资产~1+r基准)，最新残差除以窗口残差标准差。', { requires: 'benchmark', sources: [NIST], caveat: '诊断性回归残差；不等同于协整检验、可交易价差或已验证统计套利信号。' }),
  custom('Q_RS', '相对基准强弱 %', '统计与相对强弱', windows([20], ['比较窗口']), '比较同一窗口证券与基准的累积价格变化。', '100×[(C/C[N])/(B/B[N])−1]，要求窗口内每个收益区间的时间戳对齐。', { requires: 'benchmark', sources: [NIST], caveat: '相对价格表现，未考虑不同资产分红与货币口径；需使用一致价格基础。' }),
];
const technicalSources: Record<string, string> = {
  Q_ATR: 'https://www.tradingview.com/support/solutions/43000501823-average-true-range-atr/',
  Q_NATR: 'https://ta-lib.org/functions/',
  Q_SUPERTREND: 'https://www.tradingview.com/support/solutions/43000634738-supertrend/',
  Q_DONCHIAN: 'https://www.tradingview.com/support/solutions/43000502253-donchian-channels-dc/',
  Q_KELTNER: 'https://www.tradingview.com/support/solutions/43000502266-keltner-channels-kc/',
  Q_STOCHRSI: 'https://www.tradingview.com/support/solutions/43000502333-stochastic-rsi-stoch-rsi/',
  Q_CMF: 'https://help.chaikinanalytics.com/knowledge-center/chaikin-money-flow',
  Q_MFI: 'https://www.tradingview.com/support/solutions/43000502348-money-flow-mfi/',
  Q_CHAIKIN: 'https://www.tradingview.com/support/solutions/43000501979-chaikin-oscillator/',
  Q_FORCE: 'https://www.tradingview.com/support/solutions/43000502259-elder-s-force-index-efi/',
  Q_BBP: 'https://www.tradingview.com/support/solutions/43000501971-bollinger-bands-b-b/',
  Q_BBW: 'https://www.tradingview.com/support/solutions/43000501972-bollinger-bandwidth-bbw/',
  Q_ER: 'https://toslc.thinkorswim.com/center/reference/Tech-Indicators/studies-library/E-F/EfficiencyRatio.html',
};
for (const definition of CATALOG) if (technicalSources[definition.id]) definition.sources = [technicalSources[definition.id]];
const catalogIndex = new Map(CATALOG.map(definition => [definition.id, definition]));
export const CATEGORIES = [...new Set(CATALOG.map(definition => definition.category))];
export function getIndicatorDefinition(id: string): IndicatorDefinition | undefined { return catalogIndex.get(id); }
export function getUnavailableReason(definition: IndicatorDefinition, context: IndicatorContext): string | null {
  if (/^(1|5|15|30|60)m$/.test(context.period) && ['Q_HVOL', 'Q_PARKINSON', 'Q_GK', 'Q_SHARPE', 'Q_SORTINO'].includes(definition.id)) return '分钟线年化需独立交易时段与频率假设，当前不沿用日线每年 252 柱';
  if (definition.requires === 'turnover' && !context.hasTurnover) return '需要真实成交额 turnover 与同单位成交量；当前数据字段不足';
  if (definition.requires === 'benchmark' && !context.hasBenchmark) return '请先选择有共同观测日期的比较基准';
  return null;
}
export function validateParams(definition: IndicatorDefinition, values: unknown): string | null {
  if (!Array.isArray(values) || values.length !== definition.params.length) return '参数数量不匹配';
  for (let index = 0; index < definition.params.length; index++) {
    const spec = definition.params[index]; const value = values[index];
    if (typeof value !== 'number' || !Number.isFinite(value) || value < spec.min || value > spec.max) return `${spec.label}须在 ${spec.min}—${spec.max} 之间`;
    if (spec.step === 1 && !Number.isInteger(value)) return `${spec.label}须为整数`;
  }
  if (['MACD', 'DMA', 'AO', 'Q_CHAIKIN'].includes(definition.id) && values[0] >= values[1]) return '快周期须小于慢周期';
  if (definition.id === 'SMA' && values[1] > values[0]) return '平滑权重 M 不能大于窗口 N';
  if (definition.id === 'SAR' && (values[0] > values[2] || values[1] > values[2])) return '初始因子和步长不能超过最大因子';
  return null;
}
const annualizedIds = new Set(['Q_HVOL', 'Q_PARKINSON', 'Q_GK', 'Q_SHARPE', 'Q_SORTINO']);
export const normalizeIndicatorPeriod = (period: unknown): IndicatorPeriod => period === 'W' || period === 'M' ? period : 'D';
export const annualBarsForPeriod = (period: unknown): number => period === 'W' ? 52 : period === 'M' ? 12 : 252;
/** A saved template carries its observation frequency; only cross-frequency loads rebase annual assumptions. */
export function rebaseAnnualization(instances: IndicatorInstance[], sourcePeriod: IndicatorPeriod, targetPeriod: IndicatorPeriod): IndicatorInstance[] {
  return instances.map(instance => {
    const params = [...instance.params];
    if (sourcePeriod !== targetPeriod && annualizedIds.has(instance.id)) params[1] = annualBarsForPeriod(targetPeriod);
    return { ...instance, params };
  });
}
let instanceCounter = 0;
export function defaultInstance(id: string, period = 'D'): IndicatorInstance {
  const definition = getIndicatorDefinition(id);
  if (!definition) throw new Error('未知指标');
  const params = definition.params.map(param => param.default);
  if (annualizedIds.has(id)) params[1] = annualBarsForPeriod(period);
  return { id, key: `${id}_${Date.now().toString(36)}_${++instanceCounter}`, params, visible: true };
}
export const DEFAULT_INDICATORS: IndicatorInstance[] = [
  { id: 'MA', key: 'default_ma', params: [5, 10, 30, 60], visible: true },
  { id: 'VOL', key: 'default_vol', params: [5, 10, 20], visible: true },
  { id: 'MACD', key: 'default_macd', params: [12, 26, 9], visible: true },
];
export function validateInstances(input: unknown): IndicatorInstance[] {
  if (!Array.isArray(input)) return [];
  const result: IndicatorInstance[] = []; const seen = new Set<string>(); const keys = new Set<string>(); let normalCount = 0;
  for (const raw of input) {
    if (result.length >= 12 || !raw || typeof raw !== 'object') continue;
    const value = raw as Partial<IndicatorInstance>; const definition = typeof value.id === 'string' ? getIndicatorDefinition(value.id) : undefined;
    if (!definition || seen.has(definition.id) || typeof value.key !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(value.key) || keys.has(value.key) || typeof value.visible !== 'boolean' || validateParams(definition, value.params)) continue;
    if (value.visible && definition.pane === 'normal' && normalCount >= 6) continue;
    if (value.visible && definition.pane === 'normal') normalCount++;
    seen.add(definition.id); keys.add(value.key); result.push({ id: definition.id, key: value.key, params: [...value.params!], visible: value.visible });
  }
  return result;
}
export const TOOLBOX_PRESETS = [
  { id: 'trend', name: '趋势与突破', ids: ['EMA', 'BOLL', 'VOL', 'DMI', 'Q_ATR'] },
  { id: 'momentum', name: '动量与量价', ids: ['MA', 'MACD', 'RSI', 'Q_RVOL', 'Q_CMF'] },
  { id: 'risk', name: '波动与回撤', ids: ['MA', 'Q_HVOL', 'Q_PARKINSON', 'Q_ATR', 'Q_DRAWDOWN', 'Q_SHARPE'] },
  { id: 'relative', name: '基准与统计', ids: ['EMA', 'Q_ZSCORE', 'Q_BETA', 'Q_CORR', 'Q_RESIDUALZ', 'Q_RS'] },
] as const;
export const CAPABILITY_REQUIREMENTS = [
  { name: '锚定 VWAP', requirement: '待接入真实成交额、锚点选择与会话口径；当前 AVP 不替代此工具。' },
  { name: '成交量分布 / POC / 价值区', requirement: '待接入价位成交分布或合适细粒度数据；日线 OHLCV 无法恢复真实分布。' },
  { name: '足迹图 / Delta / 订单流', requirement: '需要真实逐笔及主买主卖或深度数据，当前未提供。' },
  { name: 'Pine Script / 社区脚本', requirement: '当前没有 Pine 运行时；公开公式通过已实现的 TypeScript 指标计算。' },
];
