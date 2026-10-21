import { useEffect, useRef, useState } from 'react';
import { api, post } from '../api';
import { fmt } from '../types';
import { ErrorMessage } from './Common';
import { parseShanghaiMinute } from '../charting/intraday';

type Result = {status:string; reason?:string; reasons?:string[]; filled_qty?:number;unfilled_qty?:number;total_fees_rmb?:number;
 fills?:{timestamp:string|number;qty:number;price_rmb:number;fees_rmb:number}[];[key:string]:unknown};
const shanghaiInput = (value:string) => {
 const stamp=Date.parse(value);return Number.isFinite(stamp)?new Date(stamp+8*60*60*1000).toISOString().slice(0,16):'';
};
export function MinuteExecutionPreview({minuteDatasetId,symbol,asOf,price}:{minuteDatasetId:string;symbol:string;asOf:string;price:number|undefined}){
 const [runs,setRuns]=useState<{id:string;name:string;mode:string}[]>([]),[source,setSource]=useState('');
 const [side,setSide]=useState('buy'),[qty,setQty]=useState(100),[limit,setLimit]=useState(0),[available,setAvailable]=useState(''),[cash,setCash]=useState(100000);
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[result,setResult]=useState<Result|null>(null);
 const context=JSON.stringify([minuteDatasetId,symbol,asOf,side,qty,limit,available,cash,source]);
 const latestContext=useRef(context),request=useRef(0);
 const availableInput=useRef<HTMLInputElement>(null);
 latestContext.current=context;
 useEffect(()=>()=>{request.current+=1;},[]);
 useEffect(()=>{const abort=new AbortController();api<{runs:{id:string;name:string;mode:string}[]}>('/trading/overview',{signal:abort.signal}).then(value=>{if(!abort.signal.aborted){const saved=value.runs.filter(run=>run.mode==='replay');setRuns(saved);setSource(saved[0]?.id||'');}}).catch(()=>{});return()=>abort.abort();},[]);
 useEffect(()=>{setResult(null);setError('');},[minuteDatasetId,symbol,asOf,side,qty,limit,available,cash,source]);
 useEffect(()=>{setLimit(price??0);},[minuteDatasetId,symbol,price]);
 useEffect(()=>{setAvailable(shanghaiInput(asOf));},[minuteDatasetId,symbol,asOf]);
 async function calculate(){const plannedAt=parseShanghaiMinute(availableInput.current?.value||available);if(plannedAt==null){setError('请选择有效的上海分钟时间');return;}const submittedContext=context,requestId=++request.current;setBusy(true);setError('');setResult(null);try{
  const body={minute_dataset_id:minuteDatasetId,symbol,side,qty,limit_price:limit,
   available_at:new Date(plannedAt).toISOString(),as_of:asOf,
   daily_feed_run_id:source,...(side==='sell'?{run_id:source}:{initial_cash:cash})};
  const value=await post<Result>('/minute-execution/preview',body);
  if(request.current===requestId&&latestContext.current===submittedContext)setResult(value);
 }catch(error){if(request.current===requestId&&latestContext.current===submittedContext)setError((error as Error).message);}finally{if(request.current===requestId)setBusy(false);}}
 return <section className="minute-execution-preview"><h3>成交推演</h3><p className="subtle">用保存的分钟行情和同一账本规则测算。仅生成临时副本结果。</p>
 <div className="form-grid"><label>规则与证券资料来源<select aria-label="分钟推演来源账户" value={source} onChange={event=>setSource(event.target.value)}>{!runs.length&&<option value="">暂无保存账户</option>}{runs.map(run=><option key={run.id} value={run.id}>{run.name}</option>)}</select></label>
 <label>方向<select aria-label="分钟推演方向" value={side} onChange={event=>setSide(event.target.value)}><option value="buy">买入测算</option><option value="sell">卖出测算 · 复制所选账户</option></select></label>
 <label>数量（股）<input aria-label="分钟推演数量" type="number" min={1} max={1000000} step={100} value={qty} onChange={event=>setQty(Number(event.target.value))}/></label>
 <label>限价（元）<input aria-label="分钟推演限价" type="number" min={0.01} step={0.01} value={limit} onChange={event=>setLimit(Number(event.target.value))}/></label>
 <label>计划可用时间（上海）<input ref={availableInput} aria-label="分钟计划可用时间" type="datetime-local" value={available} onInput={event=>setAvailable(event.currentTarget.value)} onChange={event=>setAvailable(event.target.value)}/></label>
 {side==='buy'&&<label>测算资金（元）<input aria-label="分钟推演资金" type="number" min={1000} max={10000000} step={1000} value={cash} onChange={event=>setCash(Number(event.target.value))}/></label>}</div>
 <p className="subtle">只使用图表截止时间以内、计划可用后的完整分钟。行情与证券资料须对应；卖出还需匹配上一交易日的账户状态。</p>
 <button disabled={busy||!source||!minuteDatasetId||!symbol||!asOf||!(limit>0)||!(qty>0)} onClick={()=>void calculate()}>{busy?'测算中…':'生成成交推演'}</button>{error&&<ErrorMessage message={error}/>}
 {result&&<div role="status"><p>{result.status==='available'||result.status==='completed'?'推演完成':'当前资料不足或未成交'}{result.reason?` · ${result.reason}`:''}</p>{result.reasons?.map(reason=><p key={reason} className="subtle">{reason}</p>)}
 {result.filled_qty!=null&&<p>成交 {fmt(result.filled_qty,0)} 股 · 未成交 {fmt(result.unfilled_qty,0)} 股 · 费用 {fmt(result.total_fees_rmb)} 元</p>}
 {!!result.fills?.length&&<div className="table-scroll"><table className="compact"><thead><tr><th>分钟标签</th><th>股数</th><th>价格（元）</th><th>费用（元）</th></tr></thead><tbody>{result.fills.map((fill,index)=><tr key={index}><td>{String(fill.timestamp)}</td><td>{fmt(fill.qty,0)}</td><td>{fmt(fill.price_rmb)}</td><td>{fmt(fill.fees_rmb)}</td></tr>)}</tbody></table></div>}</div>}
 </section>;
}
