import type {Bar,Dataset} from '../types';
export type ChartFill={execution_id?:string;date?:string;day:string;side:string;qty:number;price_rmb:number;price_unit?:string;fees_rmb?:number;order_id?:string};
export type ChartSignal={date:string;symbol:string;target_weight:number;signal_date?:string;execution_not_before?:string};
export type ChartSource={dataset?:Dataset;run?:{id:string;name:string;mode:string;status:string;start:string;end:string;day:string;kind?:string};snapshot?:Record<string,unknown>;symbol:string;symbols?:string[];prices:Bar[];calendar:string[];source_period:'D';price_basis:string;source_kind:'research_snapshot'|'run_snapshot';realtime:false;turnover_available?:boolean;fields:string[];fills?:ChartFill[];signals?:ChartSignal[]};
export type FactorSource={job_id:string;dataset_id:string;symbol:string;price_basis:string;source_kind:string;available_at:string;factors:string[];values:({date:string;symbol:string}&Record<string,string|number|null>)[]};
export const toTimestamp=(date:string)=>Date.parse(`${date.slice(0,10)}T00:00:00+08:00`);
export const toIndicatorBars=(bars:Bar[])=>bars.map(bar=>({timestamp:toTimestamp(bar.date),open:bar.open,high:bar.high,low:bar.low,close:bar.close,volume:bar.volume,...(bar.turnover==null?{}:{turnover:bar.turnover})}));
