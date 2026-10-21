import {useEffect,useLayoutEffect,useRef,type RefObject} from 'react';
import {init,dispose,ActionType,type Chart} from 'klinecharts';
import type {Bar} from '../types';
import {toIndicatorBars,toTimestamp} from './reviewTypes';
import {findLinkedBarIndex,type ReviewPeriod} from './timeframes';
export function LinkedReviewChart({bars,mainBars,main,epoch,label,period,mainPeriod}:{bars:Bar[];mainBars:Bar[];main:RefObject<Chart|null>;epoch:number;label:string;period:ReviewPeriod;mainPeriod:ReviewPeriod}){
 const host=useRef<HTMLDivElement>(null);const chart=useRef<Chart|null>(null);const context=useRef({bars,mainBars,period,mainPeriod});context.current={bars,mainBars,period,mainPeriod};
 useEffect(()=>{
  if(!host.current)return;const instance=init(host.current,{locale:'zh-CN',timezone:'Asia/Shanghai',styles:'dark'});if(!instance)return;chart.current=instance;instance.applyNewData(toIndicatorBars(context.current.bars),false);
  instance.setPriceVolumePrecision(3,0);instance.createIndicator({name:'MA',calcParams:[5,20,60]},true,{id:'candle_pane'});
  const observer=new ResizeObserver(()=>instance.resize());observer.observe(host.current);
  return()=>{observer.disconnect();dispose(instance);chart.current=null;};
 },[]);
 useLayoutEffect(()=>{chart.current?.applyNewData(toIndicatorBars(bars),false);},[bars]);
 useEffect(()=>{
  const primary=main.current,secondary=chart.current;if(!primary||!secondary)return;
  function sync(target:Chart,sourceBars:Bar[],targetBars:Bar[],targetPeriod:ReviewPeriod,data:{dataIndex?:number}){
   if(data.dataIndex==null||!sourceBars[data.dataIndex])return;
   // Match the visible period bucket, e.g. Wednesday to the same week's candle.
   // Both charts have already been capped at the shared replay cutoff.
   const index=findLinkedBarIndex(sourceBars[data.dataIndex].date,targetBars,targetPeriod);
   if(index<0)return;const point=target.convertToPixel({timestamp:toTimestamp(targetBars[index].date),value:targetBars[index].close},{paneId:'candle_pane'}) as {x?:number;y?:number};
   if(point.x!=null)target.executeAction(ActionType.OnCrosshairChange,{x:point.x,y:point.y,paneId:'candle_pane'});
  }
  const fromMain=(data:{dataIndex?:number})=>sync(secondary,context.current.mainBars,context.current.bars,context.current.period,data);
  const fromSecond=(data:{dataIndex?:number})=>sync(primary,context.current.bars,context.current.mainBars,context.current.mainPeriod,data);
  primary.subscribeAction(ActionType.OnCrosshairChange,fromMain);secondary.subscribeAction(ActionType.OnCrosshairChange,fromSecond);
  return()=>{primary.unsubscribeAction(ActionType.OnCrosshairChange,fromMain);secondary.unsubscribeAction(ActionType.OnCrosshairChange,fromSecond);};
 },[main,epoch]);
 return <section className="review-linked"><h3>{label}<small>同一证券 · 截止日联动 · 十字光标联动</small></h3><div ref={host} aria-label="联动周期图表" className="review-linked-host"/></section>;
}
