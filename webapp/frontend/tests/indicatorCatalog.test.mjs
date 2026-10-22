import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/charting');
/** Load actual TS modules in memory; mock only the canvas SDK registration surface. */
function loader() {
  const cache = new Map(); const templates = [];
  const sdk = { IndicatorSeries: {Price: 'price', Normal: 'normal'}, registerIndicator: template => templates.push(template) };
  function load(file) {
    const full = path.resolve(root, file);
    if (cache.has(full)) return cache.get(full);
    const exports = {}; cache.set(full, exports);
    const code = ts.transpileModule(fs.readFileSync(full, 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
    const require = id => id === 'klinecharts' ? sdk : load(`${id}.ts`);
    vm.runInNewContext(code, {exports, require, Date, Map, Set, Math, Number, Object, Array, JSON}, {filename: full});
    return exports;
  }
  return {load, templates};
}
const catalog = loader().load('indicatorCatalog.ts');
const {CATALOG, defaultInstance, getIndicatorDefinition, getUnavailableReason, normalizeIndicatorPeriod, rebaseAnnualization, validateInstances, validateParams} = catalog;
const plain = value => JSON.parse(JSON.stringify(value));
const context = {hasBenchmark: false, hasTurnover: false, period: 'D', barCount: 220};

test('catalog has unique definitions, valid default parameters and inspectable formula metadata', () => {
  assert.equal(CATALOG.length, 56);
  assert.equal(new Set(CATALOG.map(definition => definition.id)).size, CATALOG.length);
  assert.equal(CATALOG.filter(definition => definition.native).length, 27);
  assert.equal(CATALOG.filter(definition => !definition.native).length, 29);
  for (const definition of CATALOG) {
    assert.equal(validateParams(definition, definition.params.map(param => param.default)), null, definition.id);
    assert.ok(definition.formula.length > 5 && definition.description.length > 5 && definition.caveat.length > 5, definition.id);
    assert.ok(definition.sources.length && definition.sources.every(source => source.startsWith('https://')), definition.id);
  }
});

test('parameter validation rejects malformed, nonfinite, out-of-range and relationally invalid values', () => {
  const atr = getIndicatorDefinition('Q_ATR');
  for (const params of [[], [NaN], [Infinity], [0], [1.5], [1001], ['14']]) assert.ok(validateParams(atr, params));
  assert.ok(validateParams(getIndicatorDefinition('MACD'), [26, 12, 9]));
  assert.ok(validateParams(getIndicatorDefinition('SMA'), [3, 4]));
  assert.ok(validateParams(getIndicatorDefinition('SAR'), [30, 2, 20]));
  assert.equal(validateParams(atr, [14]), null);
  assert.equal(validateParams(getIndicatorDefinition('Q_KELTNER'), [20, 10, 1.5]), null);
});

test('turnover and benchmark indicators are unavailable until the required real data exists', () => {
  assert.match(getUnavailableReason(getIndicatorDefinition('AVP'), context), /真实成交额/);
  assert.match(getUnavailableReason(getIndicatorDefinition('Q_BETA'), context), /基准/);
  assert.equal(getUnavailableReason(getIndicatorDefinition('AVP'), {...context, hasTurnover: true}), null);
  assert.equal(getUnavailableReason(getIndicatorDefinition('Q_CORR'), {...context, hasBenchmark: true}), null);
  assert.equal(getUnavailableReason(getIndicatorDefinition('MA'), context), null);
});

test('annualized defaults follow D/W/M observation frequency while unrelated windows remain intact', () => {
  for (const id of ['Q_HVOL', 'Q_PARKINSON', 'Q_GK', 'Q_SHARPE', 'Q_SORTINO']) {
    assert.equal(defaultInstance(id, 'D').params[1], 252);
    assert.equal(defaultInstance(id, 'W').params[1], 52);
    assert.equal(defaultInstance(id, 'M').params[1], 12);
  }
  assert.deepEqual(plain(defaultInstance('MACD', 'W').params), [12, 26, 9]);
});

test('cross-period templates rebase annual factors, preserve matching-period custom settings and do not mutate saved data', () => {
  const saved = [defaultInstance('Q_HVOL', 'W'), defaultInstance('Q_SHARPE', 'W'), defaultInstance('MACD', 'W')];
  saved[0].params[1] = 260;
  saved[1].visible = false;
  const original = plain(saved);
  const matching = rebaseAnnualization(saved, 'W', 'W');
  assert.equal(matching[0].params[1], 260);
  assert.equal(matching[1].params[1], 52);
  const daily = rebaseAnnualization(saved, 'W', 'D');
  assert.equal(daily[0].params[1], 252);
  assert.equal(daily[1].params[1], 252);
  assert.equal(daily[1].visible, false);
  assert.deepEqual(plain(daily[2].params), [12, 26, 9]);
  const monthly = rebaseAnnualization(saved, 'W', 'M');
  assert.equal(monthly[0].params[1], 12);
  assert.deepEqual(plain(saved), original);
  assert.equal(normalizeIndicatorPeriod(undefined), 'D');
  assert.equal(normalizeIndicatorPeriod('invalid'), 'D');
  assert.equal(normalizeIndicatorPeriod('W'), 'W');
});

test('stored instances cannot collide, smuggle invalid parameters or exhaust visible pane limits', () => {
  assert.equal(validateInstances(null).length, 0);
  assert.equal(validateInstances([{id: 'MA', key: 'bad key', params: [5, 10, 30, 60], visible: true}]).length, 0);
  assert.equal(validateInstances([{...defaultInstance('Q_ATR'), params: [Infinity]}]).length, 0);
  assert.equal(validateInstances([defaultInstance('MA'), defaultInstance('MA')]).length, 1);
  const duplicateKey = [defaultInstance('MA'), defaultInstance('VOL')]; duplicateKey[1].key = duplicateKey[0].key;
  assert.equal(validateInstances(duplicateKey).length, 1);
  const normal = CATALOG.filter(definition => definition.pane === 'normal').slice(0, 10).map(definition => defaultInstance(definition.id));
  assert.equal(validateInstances(normal).length, 6);
  const hidden = CATALOG.slice(0, 20).map(definition => ({...defaultInstance(definition.id), visible: false}));
  assert.equal(validateInstances(hidden).length, 12);
});

const bars = Array.from({length: 220}, (_, i) => {
  const close = 100 + i * .07 + Math.sin(i * .31) * 2;
  return {timestamp: 1700000000000 + i * 86400000, open: close - .2, high: close + 1, low: close - 1, close, volume: 1000 + i * 3};
});
const benchmark = bars.map((bar, i) => ({...bar, close: 90 + i * .11 + Math.cos(i * .23)}));

test('all custom definitions register once and dispatch to finite, bar-aligned, prefix-causal calculations', () => {
  const {load, templates} = loader(); const {registerResearchIndicators} = load('registerIndicators.ts');
  let reads = 0;
  registerResearchIndicators(() => {reads++; return {benchmark};});
  assert.equal(templates.length, 29);
  registerResearchIndicators(() => {reads++; return {benchmark};});
  assert.equal(templates.length, 29);
  assert.deepEqual(plain(templates.map(template => template.name).sort()), plain(CATALOG.filter(definition => !definition.native).map(definition => definition.id).sort()));
  for (const template of templates) {
    const full = template.calc(bars, {calcParams: template.calcParams});
    assert.equal(full.length, bars.length, template.name);
    assert.ok(full.some(row => Object.values(row).some(Number.isFinite)), `${template.name} never warms up`);
    for (const row of full) for (const value of Object.values(row)) assert.ok(Number.isFinite(value), template.name);
    const prefix = template.calc(bars.slice(0, 100), {calcParams: template.calcParams});
    assert.deepEqual(plain(prefix), plain(full.slice(0, 100)), template.name);
  }
  assert.ok(reads > 0);
});

test('registration updates its context getter after remount instead of retaining a stale benchmark', () => {
  const {load, templates} = loader(); const {registerResearchIndicators} = load('registerIndicators.ts');
  registerResearchIndicators(() => ({benchmark}));
  const relative = templates.find(template => template.name === 'Q_RS');
  assert.ok(relative.calc(bars, {calcParams: relative.calcParams}).some(row => typeof row.value === 'number'));
  registerResearchIndicators(() => ({}));
  assert.equal(templates.length, 29);
  assert.ok(relative.calc(bars, {calcParams: relative.calcParams}).every(row => row.value === undefined));
});
