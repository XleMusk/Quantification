import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { init, dispose, registerOverlay, OverlayMode, type Chart, type OverlayCreate, type OverlayEvent } from 'klinecharts';
import { Download, ExternalLink, Eye, LockKeyhole, MousePointer2, Pause, Play, Redo2, RotateCcw, SkipForward, Trash2, Undo2, UnlockKeyhole } from 'lucide-react';
import { api } from '../api';
import { adjustmentNames, fmt, type Bar, type Dataset, type Preview } from '../types';
import { DRAWING_TOOLS, createReplaySession, readLocal, readReview, readReplayCheckpoint, replayCheckpointKey, reviewKey, saveDrawing, tradingViewSymbol, type ReplayCheckpoint, type SavedDrawing } from './chartReviewState';
import './ChartReview.css';

registerOverlay({
  name: 'reviewRectangle', totalStep: 3,
  needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates }) => coordinates.length < 2 ? [] : [{
    type: 'rect', attrs: { x: Math.min(coordinates[0].x, coordinates[1].x), y: Math.min(coordinates[0].y, coordinates[1].y), width: Math.abs(coordinates[1].x - coordinates[0].x), height: Math.abs(coordinates[1].y - coordinates[0].y) },
  }],
});
const toolLabels = new Map<string, string>(DRAWING_TOOLS);
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));

export function ChartReview() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [datasetId, setDatasetId] = useState('');
  const [symbol, setSymbol] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  const [storageError, setStorageError] = useState('');
  const [loading, setLoading] = useState(true);
  const [epoch, setEpoch] = useState(0);
  const [tool, setTool] = useState('');
  const [color, setColor] = useState('#d8a646');
  const [width, setWidth] = useState(1);
  const [annotation, setAnnotation] = useState('观察位置');
  const [selectedId, setSelectedId] = useState('');
  const [objects, setObjects] = useState<SavedDrawing[]>([]);
  const [notes, setNotes] = useState('');
  const [, setRevision] = useState(0);
  const [replay, setReplay] = useState(false);
  const [replaySession, setReplaySession] = useState('');
  const [savedReplay, setSavedReplay] = useState<ReplayCheckpoint | null>(null);
  const [startDate, setStartDate] = useState('');
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);
  const [ma, setMa] = useState(true);
  const [volume, setVolume] = useState(true);
  const [macd, setMacd] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<Chart | null>(null);
  const drawings = useRef(new Map<string, SavedDrawing>());
  const history = useRef<{ snapshots: SavedDrawing[][]; index: number }>({ snapshots: [[]], index: 0 });
  const restoring = useRef(false);
  const activeDrawing = useRef('');
  const notesRef = useRef('');
  const keyRef = useRef('');
  const barsRef = useRef<Bar[]>([]);
  const dataset = datasets.find(item => item.id === datasetId);
  const bars = useMemo(() => preview?.prices ?? [], [preview]);
  const storageKey = preview ? reviewKey(preview.dataset, preview.symbol, replay ? replaySession : '') : '';
  const count = replay ? Math.min(cursor, bars.length) : bars.length;
  barsRef.current = bars.slice(0, count);
  const currentBar = bars[count - 1];
  const selected = objects.find(item => item.id === selectedId);
  const tv = dataset?.kind === 'historical' ? tradingViewSymbol(symbol) : null;

  function persist(nextNotes = notesRef.current) {
    if (!keyRef.current || restoring.current) return;
    try {
      localStorage.setItem(keyRef.current, JSON.stringify({ version: 1, drawings: [...drawings.current.values()], notes: nextNotes }));
      setStorageError('');
    } catch { setStorageError('浏览器存储不可用或空间已满，当前画线仍在内存中；请导出备份'); }
  }
  function commit() {
    const next = clone([...drawings.current.values()]);
    const state = history.current;
    if (JSON.stringify(next) !== JSON.stringify(state.snapshots[state.index])) {
      state.snapshots = [...state.snapshots.slice(0, state.index + 1), next].slice(-80);
      state.index = state.snapshots.length - 1;
    }
    setObjects(next); setRevision(value => value + 1); persist();
  }
  function captured({ overlay }: OverlayEvent, origin = keyRef.current) {
    if (!restoring.current && origin === keyRef.current) { drawings.current.set(overlay.id, saveDrawing(overlay, barsRef.current)); commit(); }
    return false;
  }
  function events(): Partial<OverlayCreate> {
    const origin = keyRef.current;
    return {
      onDrawEnd: event => { if (origin === keyRef.current && !restoring.current) { captured(event, origin); activeDrawing.current = ''; setTool(''); setSelectedId(event.overlay.id); } return false; },
      onPressedMoveEnd: event => captured(event, origin),
      onSelected: ({ overlay }) => { if (origin === keyRef.current && !restoring.current) setSelectedId(overlay.id); return false; },
      onRemoved: ({ overlay }) => { if (!restoring.current && origin === keyRef.current) { drawings.current.delete(overlay.id); setSelectedId(value => value === overlay.id ? '' : value); commit(); } return false; },
    };
  }
  function restore(items: SavedDrawing[]) {
    if (!chart.current) return;
    restoring.current = true;
    try {
      chart.current.removeOverlay(); drawings.current.clear(); activeDrawing.current = '';
      for (const item of items) { chart.current.createOverlay({ ...item, ...events() }, item.paneId); drawings.current.set(item.id, clone(item)); }
    } finally { restoring.current = false; }
    setSelectedId(''); setTool(''); setObjects(clone(items));
  }
  function historyStep(delta: number) {
    const state = history.current;
    const index = state.index + delta;
    if (index < 0 || index >= state.snapshots.length) return;
    state.index = index; restore(state.snapshots[index]); setRevision(value => value + 1); persist();
  }
  function cancelDrawing() {
    if (activeDrawing.current) chart.current?.removeOverlay(activeDrawing.current);
    activeDrawing.current = ''; setTool('');
  }
  function chooseTool(name: string) {
    cancelDrawing();
    if (!name || !chart.current || !count) return;
    const id = chart.current.createOverlay({
      name, mode: OverlayMode.WeakMagnet, extendData: annotation,
      styles: { line: { color, size: width }, text: { color }, rect: { color: `${color}20`, borderColor: color, borderSize: width } }, ...events(),
    });
    if (typeof id === 'string') { activeDrawing.current = id; setTool(name); }
  }
  function updateSelected(patch: Partial<OverlayCreate>) {
    if (!selectedId || !chart.current) return;
    chart.current.overrideOverlay({ id: selectedId, ...patch });
    const overlay = chart.current.getOverlayById(selectedId);
    if (overlay) captured({ overlay });
  }
  function changeStyle(nextColor: string, nextWidth: number) {
    setColor(nextColor); setWidth(nextWidth);
    if (selected) updateSelected({ styles: { ...selected.styles, line: { color: nextColor, size: nextWidth }, text: { color: nextColor }, rect: { color: `${nextColor}20`, borderColor: nextColor, borderSize: nextWidth } } });
  }
  async function exportFile(kind: 'png' | 'json') {
    try {
      let blob: Blob;
      if (kind === 'png') {
        const picture = chart.current?.getConvertPictureUrl(true, 'png', '#141a1f');
        if (!picture) return;
        blob = await (await fetch(picture)).blob();
      } else {
        blob = new Blob([JSON.stringify({ dataset_id: datasetId, symbol, period: 'D', adjustment: dataset?.adjustment, mode: replay ? 'replay' : 'analysis', replay_as_of: currentBar?.date, version: 1, drawings: [...drawings.current.values()], notes }, null, 2)], { type: 'application/json' });
      }
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = `review_${symbol}_${currentBar?.date.slice(0, 10) ?? 'empty'}.${kind}`;
      document.body.append(anchor); anchor.click(); anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setError('导出失败，请重试或使用浏览器截图'); }
  }
  function beginReplay() {
    const index = bars.findIndex(bar => bar.date.slice(0, 10) >= startDate);
    if (index < 0) return;
    cancelDrawing(); setPlaying(false); setCursor(index + 1); setReplaySession(createReplaySession()); setReplay(true);
  }
  function resumeReplay() {
    if (!preview) return;
    const saved = readReplayCheckpoint(preview.dataset, preview.symbol, bars);
    if (!saved) { setSavedReplay(null); return; }
    cancelDrawing(); setPlaying(false); setStartDate(saved.startDate); setCursor(saved.cursor); setReplaySession(saved.session); setReplay(true);
  }

  useEffect(() => {
    let current = true;
    api<Dataset[]>('/datasets').then(items => {
      if (!current) return;
      setDatasets(items);
      const item = items.find(value => value.id === readLocal('quant.review.dataset.v1')) ?? items[0];
      if (item) { setDatasetId(item.id); const saved = readLocal('quant.review.symbol.v1'); setSymbol(item.symbols.includes(saved ?? '') ? saved! : item.symbols[0] ?? ''); }
      else setLoading(false);
    }).catch(failure => { if (current) { setError(failure.message); setLoading(false); } });
    return () => { current = false; };
  }, []);
  useEffect(() => {
    if (!dataset || !dataset.symbols.includes(symbol)) return;
    const abort = new AbortController(); setLoading(true); setPreview(null); setError(''); setReplay(false); setPlaying(false);
    try { localStorage.setItem('quant.review.dataset.v1', dataset.id); localStorage.setItem('quant.review.symbol.v1', symbol); } catch { /* local preference only */ }
    api<Preview>(`/datasets/${dataset.id}/preview?symbol=${encodeURIComponent(symbol)}`, { signal: abort.signal }).then(value => {
      if (abort.signal.aborted) return;
      const invalid = value.prices.some(bar => !Number.isFinite(Date.parse(bar.date)) || ![bar.open, bar.high, bar.low, bar.close].every(price => Number.isFinite(price) && price > 0) || bar.high < Math.max(bar.open, bar.close, bar.low) || bar.low > Math.min(bar.open, bar.close, bar.high) || !Number.isFinite(bar.volume) || bar.volume < 0);
      if (invalid) throw new Error('数据包含无效日期、OHLC 或成交量，未绘图；请到数据中心检查');
      setPreview(value); setCursor(value.prices.length); setStartDate(value.prices[Math.min(29, value.prices.length - 1)]?.date.slice(0, 10) ?? '');
    }).catch(failure => { if (!abort.signal.aborted) setError(failure.message); }).finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [datasetId, symbol, datasets]);
  useEffect(() => {
    if (!host.current) return;
    const instance = init(host.current, { locale: 'zh-CN', timezone: 'Asia/Shanghai', styles: 'dark' });
    if (!instance) { setError('图表初始化失败'); return; }
    chart.current = instance;
    instance.setStyles({ grid: { horizontal: { color: '#273038' }, vertical: { color: '#242d35' } }, candle: { bar: { upColor: '#e45e62', downColor: '#39aa83', upBorderColor: '#e45e62', downBorderColor: '#39aa83', upWickColor: '#e45e62', downWickColor: '#39aa83' } } });
    instance.setOffsetRightDistance(65); instance.setPriceVolumePrecision(2, 0);
    const observer = new ResizeObserver(() => instance.resize()); observer.observe(host.current);
    setEpoch(value => value + 1);
    return () => { observer.disconnect(); dispose(instance); chart.current = null; keyRef.current = ''; };
  }, []);
  useLayoutEffect(() => {
    if (!chart.current) return;
    const switching = keyRef.current !== storageKey;
    // Suppress library callbacks while replacing data/context. Each overlay also
    // carries its originating key, so a late old-context event cannot save here.
    restoring.current = true;
    try {
      if (switching) chart.current.removeOverlay();
      // Only this prefix enters the chart or its indicator calculations.
      chart.current.applyNewData(bars.slice(0, count).map(bar => ({ timestamp: Date.parse(`${bar.date.slice(0, 10)}T00:00:00+08:00`), open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume })), false);
      if (switching) {
        keyRef.current = storageKey;
        const saved = storageKey ? readReview(storageKey) : { drawings: [], notes: '' };
        restore(saved.drawings); notesRef.current = saved.notes; setNotes(saved.notes);
        history.current = { snapshots: [clone(saved.drawings)], index: 0 }; setRevision(value => value + 1); setStorageError('');
      }
    } catch (failure) { restore([]); history.current = { snapshots: [[]], index: 0 }; notesRef.current = ''; setNotes(''); setRevision(value => value + 1); setStorageError((failure as Error).message); }
    finally { restoring.current = false; }
  }, [bars, count, storageKey, epoch]);
  useEffect(() => { setSavedReplay(preview ? readReplayCheckpoint(preview.dataset, preview.symbol, bars) : null); }, [preview, bars]);
  useEffect(() => {
    if (!preview || !replay || !replaySession || !cursor) return;
    const saved: ReplayCheckpoint = { version: 1, session: replaySession, startDate, cursor };
    try {
      const previous = readReplayCheckpoint(preview.dataset, preview.symbol, bars);
      localStorage.setItem(replayCheckpointKey(preview.dataset, preview.symbol), JSON.stringify(saved));
      if (previous && previous.session !== replaySession) localStorage.removeItem(reviewKey(preview.dataset, preview.symbol, previous.session));
      setSavedReplay(saved);
    } catch { setStorageError('回放进度保存失败，请导出本次画线与笔记'); }
  }, [preview, bars, replay, replaySession, startDate, cursor]);
  useEffect(() => {
    if (!chart.current) return;
    if (ma) chart.current.createIndicator('MA', false, { id: 'candle_pane' }); else chart.current.removeIndicator('candle_pane', 'MA');
    if (volume) chart.current.createIndicator('VOL', false, { id: 'review_volume', height: 105 }); else chart.current.removeIndicator('review_volume', 'VOL');
    if (macd) chart.current.createIndicator('MACD', false, { id: 'review_macd', height: 105 }); else chart.current.removeIndicator('review_macd', 'MACD');
  }, [ma, volume, macd, epoch]);
  useEffect(() => {
    if (!playing || !replay) return;
    const timer = window.setInterval(() => setCursor(value => Math.min(value + 1, bars.length)), 1000 / speed);
    return () => window.clearInterval(timer);
  }, [playing, replay, speed, bars.length]);
  useEffect(() => { if (count >= bars.length) setPlaying(false); }, [count, bars.length]);
  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      if ((event.target as HTMLElement)?.matches('input, textarea, select, [contenteditable]')) return;
      if (event.key === 'Escape') cancelDrawing();
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); historyStep(event.shiftKey ? 1 : -1); }
      if ((event.key === 'Delete' || event.key === 'Backspace') && selectedId) { event.preventDefault(); chart.current?.removeOverlay(selectedId); }
    }
    window.addEventListener('keydown', keydown); return () => window.removeEventListener('keydown', keydown);
  }, [selectedId]);

  return <section className="chart-review">
    <header className="review-heading"><div><h1>图表复盘</h1><span>本地行情 · KLineCharts · 日线</span></div><div className="review-actions"><button onClick={() => exportFile('png')} disabled={!count}><Download size={14} />图表截图</button><button onClick={() => exportFile('json')} disabled={!preview}>导出复盘</button>{tv ? <a className="button" href={`https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tv)}`} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} />TradingView</a> : <span className="review-external-note">当前代码无已核实的 TradingView 映射</span>}</div></header>
    <div className="review-source"><label>数据集<select aria-label="复盘数据集" value={datasetId} onChange={event => { setDatasetId(event.target.value); setSymbol(datasets.find(item => item.id === event.target.value)?.symbols[0] ?? ''); }}>{datasets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>证券<select aria-label="复盘证券" value={symbol} onChange={event => setSymbol(event.target.value)}>{dataset?.symbols.map(item => <option key={item}>{item}</option>)}</select></label><span>{dataset ? dataset.kind === 'synthetic' ? '合成教学数据' : '历史数据' : '尚未选择数据'} · {dataset ? adjustmentNames[dataset.adjustment] ?? dataset.adjustment : '—'} · 当前最多 800 根</span></div>
    {error && <div className="review-message review-error" role="alert">{error}</div>}{storageError && <div className="review-message" role="status">{storageError}</div>}
    {!loading && !datasets.length && <div className="review-message">暂无数据集。请先到<a href="#data">数据中心</a>导入行情，或使用明确标识的合成教学数据。</div>}
    <div className="review-toolbar"><button aria-label="选择或结束画线" aria-pressed={!tool} onClick={cancelDrawing}><MousePointer2 size={15} />选择</button><select aria-label="画线工具" value={tool} disabled={!count} onChange={event => chooseTool(event.target.value)}><option value="">选择画线工具</option>{DRAWING_TOOLS.map(([name, label]) => <option value={name} key={name}>{label}</option>)}</select><label className="review-color">颜色<input aria-label="画线颜色" type="color" value={color} onChange={event => changeStyle(event.target.value, width)} /></label><label>线宽<select aria-label="画线宽度" value={width} onChange={event => changeStyle(color, Number(event.target.value))}>{[1, 2, 3, 4].map(value => <option value={value} key={value}>{value}px</option>)}</select></label><button aria-label="撤销画线" disabled={history.current.index <= 0} onClick={() => historyStep(-1)}><Undo2 size={15} /></button><button aria-label="重做画线" disabled={history.current.index >= history.current.snapshots.length - 1} onClick={() => historyStep(1)}><Redo2 size={15} /></button><button onClick={() => { restore([]); commit(); }} disabled={!objects.length}><Trash2 size={14} />清空画线</button><span className="review-toolbar-spacer" /><label><input type="checkbox" checked={ma} onChange={event => setMa(event.target.checked)} />MA</label><label><input type="checkbox" checked={volume} onChange={event => setVolume(event.target.checked)} />VOL</label><label><input type="checkbox" checked={macd} onChange={event => setMacd(event.target.checked)} />MACD</label></div>
    <div className="review-workspace"><div className="review-canvas-pane"><div className="review-ohlc"><strong>{symbol || '未选择证券'}</strong><span>{currentBar?.date.slice(0, 10) ?? '—'}</span>{currentBar && <><span>开 {fmt(currentBar.open)}</span><span>高 {fmt(currentBar.high)}</span><span>低 {fmt(currentBar.low)}</span><span>收 {fmt(currentBar.close)}</span><span>量 {fmt(currentBar.volume, 0)}</span></>}{loading && <span>正在读取数据…</span>}</div><div className="review-chart-host" ref={host} aria-label="K线复盘图表" /><div className="review-chart-caption">{tool ? `${toolLabels.get(tool)}：在图表内点击定位，Esc 结束当前绘制` : '拖动平移 · 滚轮缩放 · 点击画线选中 · Ctrl/Cmd+Z 撤销'}<span>{replay ? `回放中：仅载入 ${count} 根` : `已载入 ${count} 根日线`} · {storageError ? '保存异常' : '本地保存'}</span></div></div>
    <aside className="review-inspector"><h2>复盘记录</h2><div className="review-meta"><span>价格口径</span><strong>{dataset ? adjustmentNames[dataset.adjustment] ?? dataset.adjustment : '—'}</strong><span>数据来源</span><strong>{dataset?.source ?? '—'}</strong><span>显示截止</span><strong>{currentBar?.date.slice(0, 10) ?? '—'}</strong><span>图形数量</span><strong>{objects.length}</strong></div><label className="review-annotation">标注内容<input aria-label="标注内容" value={annotation} maxLength={100} onChange={event => { setAnnotation(event.target.value); if (selected?.name === 'simpleAnnotation') updateSelected({ extendData: event.target.value }); }} /></label><h3>画线对象</h3><div className="review-object-list">{objects.length ? objects.map(item => <button key={item.id} className={item.id === selectedId ? 'selected' : ''} onClick={() => setSelectedId(item.id)}><span>{toolLabels.get(item.name) ?? item.name}</span><small>{item.lock ? '锁定' : item.visible === false ? '隐藏' : item.points?.[0]?.value?.toFixed(2)}</small></button>) : <p>选择工具后，在图表上点击绘制。</p>}</div><div className="review-object-actions"><button disabled={!selected} onClick={() => updateSelected({ lock: !selected?.lock })}>{selected?.lock ? <UnlockKeyhole size={14} /> : <LockKeyhole size={14} />}{selected?.lock ? '解锁' : '锁定'}</button><button disabled={!selected} onClick={() => updateSelected({ visible: selected?.visible === false })}><Eye size={14} />{selected?.visible === false ? '显示' : '隐藏'}</button><button disabled={!selected} aria-label="删除选中画线" onClick={() => chart.current?.removeOverlay(selectedId)}><Trash2 size={14} /></button></div><label className="review-notes">复盘笔记<textarea aria-label="复盘笔记" value={notes} maxLength={10000} placeholder="记录判断、触发条件与执行复盘…" onChange={event => { setNotes(event.target.value); notesRef.current = event.target.value; persist(event.target.value); }} /></label><p className="review-storage-note">画线与笔记按数据快照、证券、日线及价格口径保存在本浏览器。截图或 JSON 可用于备份。</p></aside></div>
    <footer className="review-replay"><strong>逐根回放</strong><label>起点<input aria-label="回放起始日期" type="date" value={startDate} min={bars[0]?.date.slice(0, 10)} max={bars.at(-1)?.date.slice(0, 10)} disabled={replay || !count} onChange={event => setStartDate(event.target.value)} /></label>{!replay ? <><button className="review-primary" disabled={!count || !startDate} onClick={beginReplay}><Play size={14} />开始回放</button>{savedReplay && <button onClick={resumeReplay} title={`从第 ${savedReplay.cursor} 根恢复，保留该会话画线与笔记`}>恢复上次回放</button>}</> : <><button onClick={() => setPlaying(value => !value)} disabled={count >= bars.length}>{playing ? <Pause size={14} /> : <Play size={14} />}{playing ? '暂停' : '播放'}</button><button disabled={playing || count >= bars.length} onClick={() => setCursor(value => Math.min(value + 1, bars.length))}><SkipForward size={14} />下一根</button><button onClick={beginReplay}><RotateCcw size={14} />重新开始</button><button onClick={() => { setPlaying(false); setReplay(false); }}>退出回放</button></>}<label>速度<select aria-label="回放速度" value={speed} onChange={event => setSpeed(Number(event.target.value))}>{[1, 2, 4, 8].map(value => <option value={value} key={value}>{value} 根/秒</option>)}</select></label><span>{count} / {bars.length}</span><small>{replay ? '进度与标注本地保存；重新开始会替换上次回放。普通分析记录独立保留。' : '仅手动恢复上次回放；新回放独立标注，逐根揭示行情。'}</small></footer>
  </section>;
}
