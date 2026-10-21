import { AlertTriangle, LoaderCircle } from 'lucide-react';
import { statusNames } from '../types';

export function Status({ value }: { value: string }) { return <span className={`status ${value}`}><i />{statusNames[value] || value}</span>; }
export function Warning({ children }: { children: React.ReactNode }) { return <div className="warning"><AlertTriangle size={17} /><div>{children}</div></div>; }
export function Loading() { return <div className="empty"><LoaderCircle className="spin" size={20} />加载中</div>; }
export function ErrorMessage({ message }: { message: string }) { return <div role="alert" className="error"><AlertTriangle size={17} />{message}</div>; }
