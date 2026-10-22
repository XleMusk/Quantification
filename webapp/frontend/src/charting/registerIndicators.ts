import { IndicatorSeries, registerIndicator, type IndicatorFigure } from 'klinecharts';
import { CATALOG } from './indicatorCatalog';
import { computeTechnical, TECHNICAL_IDS } from './customTechnical';
import { computeStatistics } from './customStatistics';
import type { IndicatorBar, IndicatorResult } from './indicatorMath';

export type ResearchIndicatorContext = { benchmark?: IndicatorBar[] };
let contextSource: () => ResearchIndicatorContext = () => ({});
let registered = false;
const technicalIds = new Set(TECHNICAL_IDS);
const channelIds = new Set(['Q_DONCHIAN', 'Q_KELTNER']);
function figures(id: string): IndicatorFigure<IndicatorResult>[] {
  const keys = channelIds.has(id) ? ['upper', 'middle', 'lower'] : id === 'Q_STOCHRSI' ? ['k', 'd'] : id === 'Q_SUPERTREND' ? ['trend'] : ['value'];
  const colors = ['#d6ad57', '#729ee6', '#68b99c'];
  return keys.map((key, index) => ({ key, title: `${key.toUpperCase()}: `, type: 'line', styles: () => ({ color: colors[index % colors.length], size: 1.3 }) }));
}
/** Register once, refresh context getter per chart owner. Values remain prefix-causal. */
export function registerResearchIndicators(getContext: () => ResearchIndicatorContext): void {
  contextSource = getContext;
  if (registered) return;
  registered = true;
  for (const definition of CATALOG) {
    if (definition.native) continue;
    const bounded = ['Q_MFI', 'Q_STOCHRSI'].includes(definition.id) ? { minValue: 0, maxValue: 100 }
      : ['Q_ER', 'Q_R2'].includes(definition.id) ? { minValue: 0, maxValue: 1 } : {};
    registerIndicator<IndicatorResult>({
      name: definition.id, shortName: definition.id.replace('Q_', ''), precision: 3,
      series: definition.pane === 'price' ? IndicatorSeries.Price : IndicatorSeries.Normal,
      shouldOhlc: definition.pane === 'price',
      calcParams: definition.params.map(param => param.default), figures: figures(definition.id), ...bounded,
      calc: (dataList, indicator) => {
        const data: IndicatorBar[] = dataList.map(bar => ({ timestamp: bar.timestamp, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume ?? Number.NaN, turnover: bar.turnover }));
        const params = indicator.calcParams.map(Number);
        const result = technicalIds.has(definition.id) ? computeTechnical(definition.id, data, params)
          : computeStatistics(definition.id, data, params, contextSource());
        // A malformed observation must never turn into an infinite chart scale.
        return result.map(row => Object.fromEntries(Object.entries(row).filter((entry): entry is [string, number] => typeof entry[1] === 'number' && Number.isFinite(entry[1]))));
      },
    });
  }
}
