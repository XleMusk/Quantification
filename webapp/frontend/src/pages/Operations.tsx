import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { ErrorMessage, Loading, Warning } from '../components/Common';
import './Operations.css';

type Evidence = {file: string | null; status: string; error: string | null; day: string | null; saved_status: string | null};
type View = {
 checked_at:string; clock:{latest_due_day:string|null;latest_closed_day:string|null;market_phase:string;supplier_due_time:string;calendar_status:string};
 progress:{data_status:string;system_source_day:string|null;paper_status:string};
 workers:{factor:{online:boolean;last_seen:string|null};account:{online:boolean;last_seen:string|null}};
 evidence:Record<string,Evidence>; profiles:{id:string;source_day:string|null;publication_day:string|null;missing_publication_day:string|null;publication_status:string|null}[];
 steps:{id:string;status:string|null;error:string|null;recovered:boolean|null}[];
 accounts:{id:string;name:string;mode:string;status:string;execution_day:string|null;sessions:number;error:string|null;market_version:unknown;market_version_status:string|null}[];
 recoveries:Evidence[];warnings:string[];
};
const names:Record<string,string>={latest_close:'最近收盘记录',due_close:'应到日期收盘记录',system_head:'系统保存进度',operation:'最近生产过程',settlement:'最近账户结算'};
const states:Record<string,string>={due_close_complete_research_snapshot:'应到行情已保存',due_close_missing:'应到行情尚缺',due_close_failed:'来源采集失败',calendar_unknown:'交易日历待核对',due_close_incomplete_or_unknown:'行情尚不完整',daily_routes_completed:'已保存完整过程',daily_routes_partial:'部分路径完成',source_ready_system_lagging:'行情已到，发表进度落后',due_source_unavailable:'等待来源',missing:'无保存记录',saved:'已保存',unreadable:'记录不可读',preclose:'盘中',closed_waiting_supplier:'收盘后等待供应商',close_due:'检查应到行情',non_trading_day:'非交易日'};
Object.assign(states,{unknown:'待核对',paper:'日终纸面',replay:'历史回放',running:'运行中',paused:'已暂停',completed:'已完成',failed:'失败',queued:'等待处理',unsupported_feed_type:'此来源未绑定',legacy_unbound:'旧记录未绑定',daily_receipt_missing_or_stale:'当日生产记录缺失或过期'});
const text=(value:string|null|undefined)=>value?states[value]||value:'—';
export function Operations(){
 const [value,setValue]=useState<View|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const refresh=useCallback(async()=>{setBusy(true);try{setValue(await api<View>('/operations'));setError('');}catch(error){setError((error as Error).message);}finally{setBusy(false);}},[]);
 useEffect(()=>{void refresh();const timer=setInterval(()=>void refresh(),30000);return()=>clearInterval(timer);},[refresh]);
 return <section className="operations"><header className="page-header"><h1>运行状态</h1><button onClick={()=>void refresh()} disabled={busy}><RefreshCw size={14} className={busy?'spin':''}/>刷新状态</button></header>{error&&<ErrorMessage message={error}/>} {!value?<Loading/>:<>
 <p className="subtle">上海时间 {value.checked_at} · {text(value.clock.market_phase)} · 来源应到时点 {value.clock.supplier_due_time}</p>
 <div className="metrics"><div><span>应到行情日期</span><strong className="metric-text">{value.clock.latest_due_day||'日历待核对'}</strong></div><div><span>数据状态</span><strong className="metric-text">{text(value.progress.data_status)}</strong></div><div><span>系统来源进度</span><strong className="metric-text">{text(value.progress.system_source_day)}</strong></div><div><span>生产过程</span><strong className="metric-text">{text(value.progress.paper_status)}</strong></div></div>
 <div className="operations-workers">{Object.entries(value.workers).map(([key,worker])=><div key={key}><i className={worker.online?'online':''}/><strong>{key==='factor'?'因子计算':'账户推进'}进程 {worker.online?'在线':'离线'}</strong><span>最近心跳 {worker.last_seen||'尚无记录'}</span></div>)}</div>
 <h2>来源与保存记录</h2><div className="table-scroll"><table><thead><tr><th>记录</th><th>日期</th><th>状态</th><th>原始状态 / 问题</th></tr></thead><tbody>{Object.entries(value.evidence).map(([key,item])=><tr key={key}><td title={item.file||''}>{names[key]||key}</td><td>{text(item.day)}</td><td>{text(item.status)}</td><td>{item.error||text(item.saved_status)}</td></tr>)}</tbody></table></div>
 <h2>各路径进度</h2><div className="table-scroll"><table><thead><tr><th>路径</th><th>来源日期</th><th>已记录发表日期</th><th>缺失发表</th><th>发表状态</th></tr></thead><tbody>{value.profiles.map(item=><tr key={item.id}><td>{item.id}</td><td>{text(item.source_day)}</td><td>{text(item.publication_day)}</td><td>{text(item.missing_publication_day)}</td><td>{text(item.publication_status)}</td></tr>)}</tbody></table>{!value.profiles.length&&<p className="subtle">尚无可读取的路径进度</p>}</div>
 <h2>账户与行情版本</h2><div className="table-scroll"><table><thead><tr><th>账户</th><th>模式 / 状态</th><th>执行日期</th><th>已保存交易日</th><th>逐日行情版本</th><th>问题</th></tr></thead><tbody>{value.accounts.map(item=><tr key={item.id}><td>{item.name}</td><td>{text(item.mode)} / {text(item.status)}</td><td>{text(item.execution_day)}</td><td>{item.sessions}</td><td>{item.market_version?'已记录行情引用':item.market_version_status==='legacy_unbound'?'旧记录未绑定':text(item.market_version_status)}</td><td>{item.error||'—'}</td></tr>)}</tbody></table></div>
 {value.steps.length>0&&<><h2>最近生产步骤</h2><div className="table-scroll"><table><thead><tr><th>步骤</th><th>状态</th><th>恢复记录</th><th>问题</th></tr></thead><tbody>{value.steps.map((step,index)=><tr key={`${step.id}:${index}`}><td>{step.id}</td><td>{text(step.status)}</td><td>{step.recovered===true?'使用已保存结果':'—'}</td><td>{step.error||'—'}</td></tr>)}</tbody></table></div></>}
 {value.recoveries.length>0&&<><h2>已保存恢复记录</h2>{value.recoveries.map(item=><p key={item.file} className="subtle">{item.day} · {text(item.saved_status)} · {item.error||item.file}</p>)}</>}
 {value.warnings.map(item=><Warning key={item}>{item}</Warning>)}
 </>}</section>;
}
