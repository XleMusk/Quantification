import {useEffect,useLayoutEffect,useRef,useState,type RefObject} from 'react';
import {OverlayMode,type Chart,type OverlayCreate,type OverlayEvent} from 'klinecharts';
import type {Bar} from '../types';
import {toIndicatorBars} from './reviewTypes';
import {readReview,saveDrawing,type SavedDrawing} from '../pages/chartReviewState';
const clone=<T,>(value:T):T=>JSON.parse(JSON.stringify(value));
export function useReviewDrawings(chart:RefObject<Chart|null>,bars:Bar[],storageKey:string,epoch:number,period='D'){
 const [tool,setTool]=useState(''); const [color,setColor]=useState('#d8a646');const [width,setWidth]=useState(1);
 const [annotation,setAnnotation]=useState('观察位置');const [selectedId,setSelectedId]=useState('');
 const [objects,setObjects]=useState<SavedDrawing[]>([]);const [notes,setNotes]=useState('');const [storageError,setStorageError]=useState('');
 const [revision,setRevision]=useState(0);const drawings=useRef(new Map<string,SavedDrawing>());
 const history=useRef<{snapshots:SavedDrawing[][];index:number}>({snapshots:[[]],index:0});
 const restoring=useRef(false);const activeDrawing=useRef('');const notesRef=useRef('');const keyRef=useRef('');const barsRef=useRef(bars);barsRef.current=bars;
 const selected=objects.find(item=>item.id===selectedId);const count=bars.length;
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
    if (!restoring.current && origin === keyRef.current) {
      const saved = saveDrawing(overlay, barsRef.current, period !== 'D');
      drawings.current.set(overlay.id, saved);
      // A forming week/month changes its end-date timestamp as replay advances.
      // Keep the live overlay on the same bucket index as its saved geometry.
      applyStablePoints(saved); commit();
    }
    return false;
  }
  function applyStablePoints(saved: SavedDrawing) {
    if (period === 'D' || !chart.current || !saved.points?.length) return;
    const wasRestoring = restoring.current; restoring.current = true;
    try {
      // v9 setPoints copies only the outer array, and updatePointPosition may
      // attach timestamps to these point objects. Never share the saved points.
      chart.current.overrideOverlay({ id: saved.id, points: clone(saved.points) });
    } finally { restoring.current = wasRestoring; }
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
      for (const item of items) { chart.current.createOverlay({ ...clone(item), ...events() }, item.paneId); drawings.current.set(item.id, clone(item)); }
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
  useLayoutEffect(() => {
    if (!chart.current) return;
    const switching = keyRef.current !== storageKey;
    // Suppress library callbacks while replacing data/context. Each overlay also
    // carries its originating key, so a late old-context event cannot save here.
    restoring.current = true;
    try {
      if (switching) chart.current.removeOverlay();
      // Only this prefix enters the chart or its indicator calculations.
      chart.current.applyNewData(toIndicatorBars(bars), false);
      if (switching) {
        keyRef.current = storageKey;
        const saved = storageKey ? readReview(storageKey) : { drawings: [], notes: '' };
        restore(saved.drawings); notesRef.current = saved.notes; setNotes(saved.notes);
        history.current = { snapshots: [clone(saved.drawings)], index: 0 }; setRevision(value => value + 1); setStorageError('');
      }
      // In v9 applyNewData synchronously updates overlay point positions before
      // awaiting indicator calculations. Reapply index-only geometry afterward,
      // on every replay tick, not only after changing the storage context.
      for (const saved of drawings.current.values()) applyStablePoints(saved);
    } catch (failure) { restore([]); history.current = { snapshots: [[]], index: 0 }; notesRef.current = ''; setNotes(''); setRevision(value => value + 1); setStorageError((failure as Error).message); }
    finally { restoring.current = false; }
  }, [bars, storageKey, epoch, period]);
  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      if ((event.target as HTMLElement)?.matches('input, textarea, select, [contenteditable]')) return;
      if (event.key === 'Escape') cancelDrawing();
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); historyStep(event.shiftKey ? 1 : -1); }
      if ((event.key === 'Delete' || event.key === 'Backspace') && selectedId) { event.preventDefault(); chart.current?.removeOverlay(selectedId); }
    }
    window.addEventListener('keydown', keydown); return () => window.removeEventListener('keydown', keydown);
  }, [selectedId]);

 return {tool,color,width,annotation,setAnnotation,selectedId,setSelectedId,selected,objects,notes,storageError,
  chooseTool,cancelDrawing,updateSelected,changeStyle,historyStep,canUndo:history.current.index>0,canRedo:history.current.index<history.current.snapshots.length-1,
  clear:()=>{restore([]);commit();},removeSelected:()=>chart.current?.removeOverlay(selectedId),
  setNote:(value:string)=>{setNotes(value);notesRef.current=value;persist(value);},revision};
}
