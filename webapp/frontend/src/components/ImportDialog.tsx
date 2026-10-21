import { useEffect, useRef, useState } from 'react';
import { Download, Upload, X } from 'lucide-react';
import { api } from '../api';
import type { Dataset } from '../types';
import { ErrorMessage } from './Common';

export function ImportDialog({ onClose, onImported }: { onClose: () => void; onImported: (dataset: Dataset) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { dialog.current?.showModal(); }, []);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    const body = new FormData(event.currentTarget);
    const calendar = body.get('calendar_file') as File;
    if (!calendar?.size) body.delete('calendar_file');
    try { onImported(await api<Dataset>('/datasets', { method: 'POST', body })); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <dialog ref={dialog} onCancel={onClose} className="import-dialog">
    <div className="dialog-heading"><h2>导入数据</h2><button className="icon-button" aria-label="关闭导入" title="关闭" onClick={onClose} disabled={busy}><X size={18} /></button></div>
    <form onSubmit={submit}>
      <label>数据集名称<input name="name" required maxLength={80} placeholder="例如：主板日线 · 2024" /></label>
      <div className="form-columns"><label>数据类型<select name="kind" required defaultValue="historical"><option value="historical">历史行情</option><option value="synthetic">合成数据</option></select></label>
        <label>价格口径<select name="adjustment" required defaultValue="unknown"><option value="unknown">未声明</option><option value="raw">不复权</option><option value="forward">前复权</option><option value="backward">后复权</option><option value="synthetic">合成价格</option></select></label></div>
      <label>数据来源<input name="source" required maxLength={200} placeholder="数据供应商 / 文件出处" /></label>
      <label>行情 CSV<input type="file" name="file" accept=".csv" required /></label>
      <div className="field-note">date, symbol, open, high, low, close, volume · UTF-8 · 最大 12 MB</div>
      <label>交易日历 CSV（可选）<input type="file" name="calendar_file" accept=".csv" /></label>
      <div className="field-note">date 单列。未提供时，日历标记为“观测日期并集”。</div>
      {error && <ErrorMessage message={error} />}
      <div className="dialog-actions"><a className="text-link" href="/api/datasets/example.csv" download><Download size={15} />合成示例 CSV</a><button className="primary" disabled={busy} type="submit"><Upload size={16} />{busy ? '导入中' : '确认导入'}</button></div>
    </form>
  </dialog>;
}
