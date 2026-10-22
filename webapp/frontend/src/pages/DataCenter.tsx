import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowRight, CheckCircle2, Database, RefreshCw, Search, Upload } from 'lucide-react';
import { api, post } from '../api';
import { adjustmentNames, dateText, fmt, type Dataset, type Preview, type StockDBInventoryItem, type StockDBSource } from '../types';
import { Chart } from '../components/Chart';
import { ErrorMessage, Loading, Warning } from '../components/Common';
import { ImportDialog } from '../components/ImportDialog';
import './DataCenter.css';

const fieldNames: Record<string, string> = {
  date: '日期', symbol: '证券', open: '开盘', high: '最高', low: '最低', close: '收盘', volume: '成交量',
  amount: '成交额', turnover_rate_pct: '换手率', pre_close: '前收盘', pe_ttm: '市盈率 TTM', pb: '市净率',
  total_mv: '总市值', float_mv: '流通市值', is_st: 'ST 标记', name: '名称', pct_chg: '来源涨跌幅',
  amplitude: '振幅', vol_ratio: '量比', float_share: '流通股本', total_share: '总股本',
};
const unitNames: Record<string, string> = { CNY: '元', share: '股', percent: '%', ratio: '倍' };
const baseFields = ['date', 'symbol', 'open', 'high', 'low', 'close', 'volume'];

export function DataCenter({ datasets, selected, select, reload }: { datasets: Dataset[]; selected: string; select: (id: string) => void; reload: () => Promise<void> }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [symbol, setSymbol] = useState('');
  const [search, setSearch] = useState('');
  const [tab, setTab] = useState('preview');
  const [error, setError] = useState('');
  const [previewError, setPreviewError] = useState('');
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const [stockdb, setStockdb] = useState<StockDBSource | null>(null);
  const [sourceError, setSourceError] = useState('');
  const [loadingSource, setLoadingSource] = useState(true);
  const [registering, setRegistering] = useState('');
  const dataset = datasets.find(d => d.id === selected);
  const selectedSymbol = dataset?.symbols.includes(symbol) ? symbol : dataset?.symbols[0] || '';
  const filteredSymbols = useMemo(() => {
    const query = search.trim().toUpperCase();
    return dataset?.symbols.filter(value => value.includes(query)) || [];
  }, [dataset, search]);
  const shownSymbols = filteredSymbols.slice(0, 100);
  if (selectedSymbol && !shownSymbols.includes(selectedSymbol)) shownSymbols.unshift(selectedSymbol);
  const fields = dataset?.fields || baseFields;
  const sourceStatus = stockdb?.service.status;

  const refreshSource = useCallback(async () => {
    setLoadingSource(true); setSourceError('');
    try { setStockdb(await api<StockDBSource>('/data-sources/stockdb')); }
    catch (error) { setStockdb(null); setSourceError((error as Error).message); }
    finally { setLoadingSource(false); }
  }, []);
  useEffect(() => { void refreshSource(); }, [refreshSource]);
  useEffect(() => { setSymbol(''); setSearch(''); }, [selected]);
  useEffect(() => {
    if (!selected || !selectedSymbol) { setPreview(null); return; }
    let current = true; setPreview(null); setPreviewError('');
    api<Preview>(`/datasets/${selected}/preview?symbol=${encodeURIComponent(selectedSymbol)}`)
      .then(value => { if (current) setPreview(value); })
      .catch(error => { if (current) setPreviewError(error.message); });
    return () => { current = false; };
  }, [selected, selectedSymbol]);

  async function sync() {
    setBusy(true); setError('');
    try { const value = await post<Dataset>('/datasets/sync-cache'); await reload(); select(value.id); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  async function registerInventory(item: StockDBInventoryItem) {
    if (item.registered_dataset_id && datasets.some(value => value.id === item.registered_dataset_id)) {
      select(item.registered_dataset_id); return;
    }
    setRegistering(item.id); setSourceError('');
    try {
      const value = await post<Dataset>('/data-sources/stockdb/register', { inventory_id: item.id });
      await reload(); select(value.id);
      await refreshSource();
    } catch (error) { setSourceError((error as Error).message); }
    finally { setRegistering(''); }
  }
  return <div className="data-center">
    <header className="page-header"><h1>数据中心</h1><div className="actions"><button onClick={sync} disabled={busy}><RefreshCw size={15} className={busy ? 'spin' : ''} />同步 BaoStock 缓存</button><button className="primary" onClick={() => setImporting(true)}><Upload size={15} />导入 CSV</button></div></header>
    {error && <ErrorMessage message={error} />}
    <section className="stockdb-panel" aria-label="StockDB 本机数据源">
      <div className="stockdb-heading"><div><Database size={16} /><h2>STOCKDB <span>本机日线库存</span></h2></div><div className="stockdb-source-actions"><span className={`stockdb-service ${sourceStatus || 'unavailable'}`}><i />{sourceStatus === 'online' ? '本机服务在线' : sourceStatus === 'offline' ? '本机服务离线' : loadingSource ? '读取本机状态' : '服务状态不可用'}</span><button onClick={() => void refreshSource()} disabled={loadingSource || !!registering}><RefreshCw size={13} className={loadingSource ? 'spin' : ''} />刷新库存</button></div></div>
      <div className="stockdb-description"><span>已保存版本 <strong>{stockdb ? stockdb.inventory.length : '—'}</strong></span><span>服务连接与已保存行情分别显示；选择版本后，图表和因子研究共用该数据集。</span></div>
      {stockdb?.service.message && <p className="source-message">{stockdb.service.message}</p>}
      {sourceError && <ErrorMessage message={sourceError} />}
      {!stockdb && loadingSource ? <Loading /> : stockdb && stockdb.inventory.length > 0 ? <div className="table-scroll"><table className="stockdb-inventory"><thead><tr><th>保存版本</th><th>覆盖区间</th><th>证券 / 交易日</th><th>实际日线</th><th>字段 / 缺失</th><th>操作</th></tr></thead><tbody>{stockdb.inventory.map(item => <tr key={item.id}>
        <td><strong>{item.name}</strong><small>{item.format}</small></td><td>{item.start || '—'}<br />{item.end || '—'}</td><td>{fmt(item.symbols, 0)} 个证券<br /><small>{fmt(item.sessions, 0)} 个交易日</small></td><td className="numeric">{fmt(item.rows, 0)}</td>
        <td><span title={item.fields.map(field => fieldNames[field] || field).join('、')}>{item.fields.length} 个字段</span>{item.requested_symbols != null && <small>请求 {fmt(item.requested_symbols, 0)} · 缺失 {fmt(item.missing_symbols, 0)}</small>}{item.warnings.length > 0 && <details><summary>{item.warnings.length} 条数据说明</summary><div>{item.warnings.map(warning => <p key={warning}>{warning}</p>)}</div></details>}</td>
        <td><button className={item.registered_dataset_id === selected ? 'inventory-selected' : ''} disabled={!!registering} onClick={() => void registerInventory(item)}>{registering === item.id ? <><RefreshCw size={13} className="spin" />保存中</> : item.registered_dataset_id === selected ? <><CheckCircle2 size={13} />已选用</> : item.registered_dataset_id ? '选用数据集' : '保存并选用'}</button></td>
      </tr>)}</tbody></table></div> : stockdb && <p className="source-message">尚未发现可注册的日线保存版本。服务在线后，也需先保存日线快照或研究库存。</p>}
      {registering && <p className="source-message" role="status">正在保存所选版本，保留实际字段和来源；全市场库存可能需要较长时间。</p>}
      {stockdb?.warnings.map(warning => <p key={warning} className="source-message">{warning}</p>)}
    </section>
    <div className="metrics"><div><span>已注册数据集</span><strong>{datasets.length}</strong></div><div><span>所选证券</span><strong>{dataset?.symbols.length ?? '—'}</strong></div><div><span>价格口径</span><strong className="metric-text">{dataset ? adjustmentNames[dataset.adjustment] || dataset.adjustment : '—'}</strong></div><div><span>保存行情截止</span><strong className="metric-text">{dataset?.end ?? '—'}</strong></div></div>
    <div className="table-scroll"><table className="dataset-table"><thead><tr><th>数据集名称</th><th>行数</th><th>证券数量</th><th>数据类型</th><th>数据来源</th><th>截止日期</th></tr></thead><tbody>
      {datasets.map(d => <tr key={d.id} className={d.id === selected ? 'selected' : ''}><td><button className="row-select" onClick={() => select(d.id)} aria-pressed={d.id === selected}><span className="radio-dot" />{d.name}</button></td><td className="numeric">{fmt(d.rows, 0)}</td><td>{d.symbols.length}</td><td><span className={`source-tag ${d.kind}`}>{d.kind === 'historical' ? '历史数据' : '合成数据'}</span></td><td>{d.source}</td><td className="numeric">{d.end}</td></tr>)}
    </tbody></table></div>
    {!datasets.length && <div className="empty"><Database size={26} />暂无数据集</div>}
    {dataset && <section className="dataset-detail"><div className="dataset-detail-heading"><h2>数据集详情 <span>{dataset.name}</span></h2><div className="actions"><a className="button" href="#chart-review">图表复盘<ArrowRight size={13} /></a><a className="button" href="#research">因子研究<ArrowRight size={13} /></a></div></div>
      <div className="tabs"><button onClick={() => setTab('preview')} className={tab === 'preview' ? 'active' : ''}>数据预览</button><button onClick={() => setTab('quality')} className={tab === 'quality' ? 'active' : ''}>质量与字段</button></div>
      {tab === 'preview' ? <>
        <div className="data-symbol-controls"><label className="symbol-search"><Search size={14} /><input aria-label="搜索证券代码" placeholder="搜索证券代码" value={search} onChange={event => setSearch(event.target.value)} /></label><label className="inline-field">证券代码<select aria-label="证券代码" value={selectedSymbol} onChange={e => setSymbol(e.target.value)}>{shownSymbols.map(value => <option key={value}>{value}</option>)}</select></label><span className="subtle">匹配 {filteredSymbols.length} / {dataset.symbols.length}{filteredSymbols.length > 100 ? ' · 显示前 100 项' : ''}</span></div>
        {previewError ? <ErrorMessage message={previewError} /> : !preview ? <Loading /> : <>
          <div className="data-split"><div className="chart-section"><h3>{preview.recent[0]?.name || selectedSymbol} · 收盘价 <small>{adjustmentNames[dataset.adjustment]}</small></h3><Chart x={preview.prices.map(p => dateText(p.date))} values={preview.prices.map(p => p.close)} label="收盘价" /><p className="subtle">最近 {preview.prices.length} 条已保存观测 · 完整日线在图表复盘查看</p></div>
          <div className="table-scroll"><table className="compact"><thead><tr><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th><th>成交量（股）</th>{fields.includes('amount') && <th>成交额（元）</th>}{fields.includes('turnover_rate_pct') && <th>换手率（%）</th>}</tr></thead><tbody>{preview.recent.map(p => <tr key={p.date}><td>{dateText(p.date)}</td><td>{fmt(p.open)}</td><td>{fmt(p.high)}</td><td>{fmt(p.low)}</td><td>{fmt(p.close)}</td><td>{fmt(p.volume, 0)}</td>{fields.includes('amount') && <td>{fmt(p.amount, 0)}</td>}{fields.includes('turnover_rate_pct') && <td>{fmt(p.turnover_rate_pct)}</td>}</tr>)}</tbody></table></div></div>
          {fields.some(field => ['pre_close', 'pe_ttm', 'pb', 'total_mv', 'float_mv', 'is_st'].includes(field)) && <div className="table-scroll dataset-extended"><table className="compact"><thead><tr><th>日期</th>{fields.includes('pre_close') && <th>来源前收盘（元）</th>}{fields.includes('pe_ttm') && <th>PE TTM（倍）</th>}{fields.includes('pb') && <th>PB（倍）</th>}{fields.includes('total_mv') && <th>总市值（元）</th>}{fields.includes('float_mv') && <th>流通市值（元）</th>}{fields.includes('is_st') && <th>ST 标记</th>}</tr></thead><tbody>{preview.recent.slice(0, 5).map(p => <tr key={p.date}><td>{dateText(p.date)}</td>{fields.includes('pre_close') && <td>{fmt(p.pre_close)}</td>}{fields.includes('pe_ttm') && <td>{fmt(p.pe_ttm)}</td>}{fields.includes('pb') && <td>{fmt(p.pb)}</td>}{fields.includes('total_mv') && <td>{fmt(p.total_mv, 0)}</td>}{fields.includes('float_mv') && <td>{fmt(p.float_mv, 0)}</td>}{fields.includes('is_st') && <td>{p.is_st == null ? '—' : p.is_st ? '是' : '否'}</td>}</tr>)}</tbody></table></div>}
        </>}
      </> : <div className="quality"><div className="quality-summary"><CheckCircle2 size={19} /><strong>{dataset.quality.invalid_rows ? '存在数据异常' : '基础字段校验通过'}</strong></div><div className="dataset-quality-grid"><dl><dt>OHLC / 成交量异常</dt><dd>{fmt(dataset.quality.invalid_rows, 0)}</dd><dt>零成交量记录</dt><dd>{fmt(dataset.quality.zero_volume_rows, 0)}</dd><dt>日期 × 证券缺失格点</dt><dd>{fmt(dataset.quality.missing_grid_rows, 0)}</dd>{dataset.requested_daily_rows != null && <><dt>来源请求日线</dt><dd>{fmt(dataset.requested_daily_rows, 0)}</dd><dt>来源未返回日线</dt><dd>{fmt(dataset.source_missing_daily_rows, 0)} · 原因未推定</dd></>}{(dataset.omitted_suspended_rows_without_prices ?? 0) > 0 && <><dt>停牌且缺必要字段</dt><dd>{fmt(dataset.omitted_suspended_rows_without_prices, 0)} · 保留原记录</dd></>}{(dataset.omitted_unknown_status_rows_without_prices ?? 0) > 0 && <><dt>状态未知且缺字段</dt><dd>{fmt(dataset.omitted_unknown_status_rows_without_prices, 0)} · 保留原记录</dd></>}<dt>交易日数量</dt><dd>{fmt(dataset.quality.sessions, 0)}</dd><dt>日历来源</dt><dd>{dataset.quality.calendar_kind === 'provided' ? '随数据提供，未独立验证' : '观测日期并集'}</dd><dt>覆盖区间</dt><dd>{dataset.start} 至 {dataset.end}</dd><dt>历史信息时点</dt><dd>{dataset.pit_status === 'not_verified' ? '估值 / 状态字段尚未核验历史可得性' : '以来源说明为准'}</dd>{dataset.fingerprint && <><dt>数据版本</dt><dd className="mono" title={dataset.fingerprint}>{dataset.fingerprint.slice(0, 20)}</dd></>}</dl><div className="table-scroll"><table className="compact"><thead><tr><th>保存字段</th><th>单位</th><th>非空覆盖</th></tr></thead><tbody>{fields.map(field => <tr key={field}><td title={field}>{fieldNames[field] || field}</td><td>{unitNames[dataset.field_units?.[field] || ''] || dataset.field_units?.[field] || '—'}</td><td>{dataset.field_coverage?.[field] == null ? '—' : `${fmt(dataset.field_coverage[field] * 100, 2)}%`}</td></tr>)}</tbody></table></div></div></div>}
      <div className="warnings">{dataset.warnings.map(text => <Warning key={text}>{text}</Warning>)}</div>
    </section>}
    {importing && <ImportDialog onClose={() => setImporting(false)} onImported={async d => { setImporting(false); await reload(); select(d.id); }} />}
  </div>;
}
