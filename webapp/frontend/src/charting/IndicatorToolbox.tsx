import { useMemo, useState } from 'react';
import { BookOpen, Check, Eye, EyeOff, Plus, Search, Star, Trash2 } from 'lucide-react';
import { CAPABILITY_REQUIREMENTS, CATALOG, CATEGORIES, DEFAULT_INDICATORS, TOOLBOX_PRESETS, annualBarsForPeriod, defaultInstance, getIndicatorDefinition, getUnavailableReason, normalizeIndicatorPeriod, rebaseAnnualization, validateInstances, validateParams, type IndicatorContext, type IndicatorDefinition, type IndicatorInstance, type IndicatorPeriod } from './indicatorCatalog';
import './IndicatorToolbox.css';

type SavedTemplate = { id: string; name: string; instances: IndicatorInstance[]; period: IndicatorPeriod; legacyPeriod?: boolean };
const periodName = (period: IndicatorPeriod) => period === 'W' ? '周线' : period === 'M' ? '月线' : '日线';
const FAVORITE_KEY = 'quant.indicators.favorites.v1';
const TEMPLATE_KEY = 'quant.indicators.templates.v1';
function readFavorites(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(FAVORITE_KEY) ?? 'null');
    return value?.version === 1 && Array.isArray(value.ids) ? value.ids.filter((id: unknown): id is string => typeof id === 'string' && !!getIndicatorDefinition(id)).slice(0, CATALOG.length) : [];
  } catch { return []; }
}
function readTemplates(): SavedTemplate[] {
  try {
    const value = JSON.parse(localStorage.getItem(TEMPLATE_KEY) ?? 'null');
    if (value?.version !== 1 || !Array.isArray(value.templates)) return [];
    return value.templates.slice(0, 20).flatMap((raw: Partial<SavedTemplate>) => {
      if (!raw || typeof raw.id !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(raw.id) || typeof raw.name !== 'string' || !raw.name.trim() || raw.name.length > 40) return [];
      const instances = validateInstances(raw.instances);
      const knownPeriod = raw.period === 'D' || raw.period === 'W' || raw.period === 'M';
      return instances.length ? [{ id: raw.id, name: raw.name, instances, period: normalizeIndicatorPeriod(raw.period), legacyPeriod: raw.legacyPeriod === true || !knownPeriod }] : [];
    });
  } catch { return []; }
}
function withinLimits(instances: IndicatorInstance[]): string | null {
  if (instances.length > 12) return '每组最多 12 个指标，请先移除其他指标';
  if (instances.filter(item => item.visible && getIndicatorDefinition(item.id)?.pane === 'normal').length > 6) return '同时最多显示 6 个副图，可先隐藏其他副图';
  return null;
}
function DefinitionDetails({ definition }: { definition: IndicatorDefinition }) {
  return <details className="indicator-explanation"><summary><BookOpen size={12} />公式与口径</summary><div><p>{definition.description}</p><p className="indicator-formula">{definition.formula}</p><p>{definition.caveat}</p><div className="indicator-sources">{definition.sources.map((source, index) => <a href={source} target="_blank" rel="noopener noreferrer" key={source}>来源 {index + 1}</a>)}</div></div></details>;
}
function ParameterEditor({ instance, definition, onApply }: { instance: IndicatorInstance; definition: IndicatorDefinition; onApply: (params: number[]) => void }) {
  const [draft, setDraft] = useState(() => instance.params.map(String));
  const [error, setError] = useState('');
  if (!definition.params.length) return <span className="indicator-no-params">无可调参数</span>;
  function apply() {
    if (draft.some(value => !value.trim())) { setError('参数不能为空'); return; }
    const params = draft.map(Number); const problem = validateParams(definition, params);
    if (problem) { setError(problem); return; }
    setError(''); onApply(params);
  }
  return <div className="indicator-parameter-editor"><div className="indicator-parameter-grid">{definition.params.map((spec, index) => <label key={spec.key}>{spec.label}<input aria-label={`${definition.id} ${spec.label}`} type="number" min={spec.min} max={spec.max} step={spec.step} value={draft[index]} onChange={event => { const next = [...draft]; next[index] = event.target.value; setDraft(next); setError(''); }} /></label>)}</div><button className="indicator-apply" onClick={apply}>应用参数</button>{error ? <p className="indicator-inline-error" role="alert">{error}</p> : null}</div>;
}
export type IndicatorToolboxProps = { instances: IndicatorInstance[]; onChange: (next: IndicatorInstance[]) => void; context: IndicatorContext };
export function IndicatorToolbox({ instances, onChange, context }: IndicatorToolboxProps) {
  const [view, setView] = useState<'catalog' | 'selected' | 'templates'>('catalog');
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('全部');
  const [favorites, setFavorites] = useState(readFavorites);
  const [templates, setTemplates] = useState(readTemplates);
  const [templateName, setTemplateName] = useState('');
  const [message, setMessage] = useState('');
  const favoriteSet = useMemo(() => new Set(favorites), [favorites]);
  const selectedIds = new Set(instances.map(instance => instance.id));
  const matching = useMemo(() => {
    const query = search.trim().toLowerCase();
    return CATALOG.filter(definition => (category === '全部' || category === '收藏' ? category !== '收藏' || favoriteSet.has(definition.id) : definition.category === category)
      && (!query || `${definition.id} ${definition.name} ${definition.description}`.toLowerCase().includes(query)));
  }, [category, search, favoriteSet]);
  const availableCount = CATALOG.filter(definition => !getUnavailableReason(definition, context)).length;
  function change(next: IndicatorInstance[]) {
    const problem = withinLimits(next);
    if (problem) { setMessage(problem); return; }
    setMessage(''); onChange(next);
  }
  function add(definition: IndicatorDefinition) {
    const unavailable = getUnavailableReason(definition, context);
    if (unavailable) { setMessage(unavailable); return; }
    if (selectedIds.has(definition.id)) return;
    change([...instances, defaultInstance(definition.id, context.period)]);
  }
  function toggleFavorite(id: string) {
    const next = favoriteSet.has(id) ? favorites.filter(value => value !== id) : [...favorites, id];
    setFavorites(next);
    try { localStorage.setItem(FAVORITE_KEY, JSON.stringify({ version: 1, ids: next })); } catch { setMessage('收藏仅保留在本次页面，浏览器存储不可用'); }
  }
  function applyTemplate(saved: IndicatorInstance[], sourcePeriod = normalizeIndicatorPeriod(context.period), legacyPeriod = false) {
    const targetPeriod = normalizeIndicatorPeriod(context.period);
    const rebased = rebaseAnnualization(saved, sourcePeriod, targetPeriod);
    const unavailable: string[] = [];
    const next = rebased.flatMap(instance => {
      const definition = getIndicatorDefinition(instance.id)!;
      if (getUnavailableReason(definition, context)) { unavailable.push(definition.name); return []; }
      return [{ ...defaultInstance(instance.id, context.period), params: [...instance.params], visible: instance.visible }];
    });
    const problem = withinLimits(next);
    if (problem) { setMessage(problem); return; }
    const notes = ['已应用指标组合'];
    if (legacyPeriod) notes.push('旧模板未记录周期，按日线解释，请核对每年柱数');
    if (sourcePeriod !== targetPeriod) notes.push(`从${periodName(sourcePeriod)}切换到${periodName(targetPeriod)}，年化指标的每年柱数已设为 ${annualBarsForPeriod(targetPeriod)}`);
    if (unavailable.length) notes.push(`缺少数据的指标未加载：${unavailable.join('、')}`);
    onChange(next); setView('selected'); setMessage(notes.join('；'));
  }
  function saveTemplates(next: SavedTemplate[]) {
    setTemplates(next);
    try { localStorage.setItem(TEMPLATE_KEY, JSON.stringify({ version: 1, templates: next })); return true; }
    catch { setMessage('模板仅保留在本次页面，浏览器存储不可用'); return false; }
  }
  function saveCurrent() {
    if (!templateName.trim()) { setMessage('请填写模板名称'); return; }
    if (!instances.length) { setMessage('请先选择指标'); return; }
    if (templates.length >= 20) { setMessage('最多保存 20 组模板，请先删除一组'); return; }
    const next = [...templates, { id: `template_${Date.now().toString(36)}`, name: templateName.trim().slice(0, 40), instances: validateInstances(instances), period: normalizeIndicatorPeriod(context.period) }];
    if (saveTemplates(next)) setMessage('模板已保存在本浏览器');
    setTemplateName('');
  }
  return <section className="indicator-toolbox" aria-label="指标研究工具箱">
    <header className="indicator-toolbox-heading"><div><h2>指标研究工具箱</h2><span>{CATALOG.length} 项定义 · 当前可用 {availableCount} 项</span></div><span className="indicator-count">{instances.length} / 12</span></header>
    <p className="indicator-toolbox-intro">原生指标与公开公式扩展，可独立选择、调参及组合。统计指标是分析工具，不是私有基金策略的复刻。</p>
    <div className="indicator-toolbox-tabs" role="tablist" aria-label="指标工具箱视图">{[['catalog', '选择指标'], ['selected', `已选 ${instances.length}`], ['templates', '组合模板']].map(([id, label]) => <button role="tab" aria-selected={view === id} key={id} onClick={() => { setView(id as typeof view); setMessage(''); }}>{label}</button>)}</div>
    {message ? <div className="indicator-toolbox-message" role="status">{message}</div> : null}
    {view === 'catalog' ? <><div className="indicator-catalog-filters"><label className="indicator-search"><Search size={14} /><input aria-label="搜索指标" placeholder="搜索名称、代码或分析用途" value={search} onChange={event => setSearch(event.target.value)} /></label><select aria-label="指标分类" value={category} onChange={event => setCategory(event.target.value)}>{['全部', '收藏', ...CATEGORIES].map(value => <option key={value}>{value}</option>)}</select></div><div className="indicator-catalog-list"><p className="indicator-match-count">{matching.length} 项匹配 · 点击星标收藏</p>{matching.map(definition => {
      const unavailable = getUnavailableReason(definition, context); const added = selectedIds.has(definition.id);
      return <article className={`indicator-catalog-item ${unavailable ? 'unavailable' : ''}`} key={definition.id}><div className="indicator-item-heading"><button className={`indicator-favorite ${favoriteSet.has(definition.id) ? 'active' : ''}`} aria-label={`${favoriteSet.has(definition.id) ? '取消收藏' : '收藏'} ${definition.id}`} aria-pressed={favoriteSet.has(definition.id)} onClick={() => toggleFavorite(definition.id)}><Star size={14} /></button><div><strong>{definition.name}</strong><span>{definition.id} · {definition.native ? '原生' : '公开公式扩展'} · {definition.pane === 'price' ? '主图' : '副图'}</span></div><button className="indicator-add" aria-label={`添加 ${definition.name}`} disabled={!!unavailable || added || instances.length >= 12} onClick={() => add(definition)}>{added ? <Check size={14} /> : <Plus size={14} />}{added ? '已选' : '添加'}</button></div><p className="indicator-purpose">{definition.description}</p>{unavailable ? <p className="indicator-unavailable">{unavailable}</p> : null}<DefinitionDetails definition={definition} /></article>;
    })}{!matching.length ? <p className="indicator-empty">没有匹配的指标，试试名称、代码或其他分类。</p> : null}<details className="indicator-capabilities"><summary>其他专业工具的数据要求</summary>{CAPABILITY_REQUIREMENTS.map(item => <p key={item.name}><strong>{item.name}</strong><span>{item.requirement}</span></p>)}</details></div></> : null}
    {view === 'selected' ? <div className="indicator-selected-list"><div className="indicator-selected-caption"><span>主图叠加 / 最多 6 个可见副图</span><button onClick={() => applyTemplate(DEFAULT_INDICATORS)}>恢复基础组合</button></div>{instances.map(instance => {
      const definition = getIndicatorDefinition(instance.id)!; const unavailable = getUnavailableReason(definition, context);
      return <article className="indicator-selected-item" key={instance.key}><div className="indicator-item-heading"><div><strong>{definition.name}</strong><span>{definition.id} · {definition.pane === 'price' ? '主图叠加' : '独立副图'}</span></div><button aria-label={`${instance.visible ? '隐藏' : '显示'} ${definition.name}`} aria-pressed={instance.visible} onClick={() => { if (!instance.visible && unavailable) { setMessage(unavailable); return; } change(instances.map(value => value.key === instance.key ? { ...value, visible: !value.visible } : value)); }}>{instance.visible ? <Eye size={14} /> : <EyeOff size={14} />}</button><button aria-label={`移除 ${definition.name}`} onClick={() => change(instances.filter(value => value.key !== instance.key))}><Trash2 size={14} /></button></div>{unavailable ? <p className="indicator-unavailable">{unavailable}</p> : null}<ParameterEditor key={`${instance.key}:${instance.params.join(',')}`} instance={instance} definition={definition} onApply={params => change(instances.map(value => value.key === instance.key ? { ...value, params } : value))} /><DefinitionDetails definition={definition} /></article>;
    })}{!instances.length ? <p className="indicator-empty">当前没有指标，请到“选择指标”添加。</p> : null}</div> : null}
    {view === 'templates' ? <div className="indicator-templates"><h3>研究组合</h3><p>应用模板会替换当前指标组合。缺少基准或成交额时，只加载其中可用的指标。</p><div className="indicator-preset-grid">{TOOLBOX_PRESETS.map(preset => <button key={preset.id} onClick={() => applyTemplate(preset.ids.map(id => defaultInstance(id, context.period)))}><strong>{preset.name}</strong><span>{preset.ids.map(id => getIndicatorDefinition(id)!.name.split(' ')[0]).join(' · ')}</span></button>)}</div><h3>保存当前配置</h3><label className="indicator-template-name">模板名称<input aria-label="指标模板名称" value={templateName} maxLength={40} placeholder="例如：我的趋势观察" onChange={event => setTemplateName(event.target.value)} /></label><button className="indicator-save-template" onClick={saveCurrent}>保存为模板</button><h3>我的模板</h3>{templates.map(template => <div className="indicator-saved-template" key={template.id}><button onClick={() => applyTemplate(template.instances, template.period, template.legacyPeriod)}><strong>{template.name}</strong><span>{template.instances.length} 项指标 · {periodName(template.period)}{template.legacyPeriod ? '（旧记录推定）' : ''}</span></button><button aria-label={`删除模板 ${template.name}`} onClick={() => saveTemplates(templates.filter(value => value.id !== template.id))}><Trash2 size={14} /></button></div>)}{!templates.length ? <p className="indicator-empty">尚无自定义模板。</p> : null}<p className="indicator-local-note">收藏、模板保存在当前浏览器，清理站点存储后需重新设置。</p></div> : null}
  </section>;
}
