import {useCallback, useEffect, useRef, useState} from 'react';
import {configFetch} from '../utils/adminApi';
import {getLang} from '../i18n';

interface ApplicationDiagnostic {
  applicationKey:string; source:string; revision:string|number; enabled:boolean;
  installId:string|null; scope:string; authentication:string; permissionStatus:string;
  availability:{available:boolean;reason:string|null};
  recentFailure?:{diagnosticId:string;code:string;timestamp:string}|null;
}
interface Receipt {
  diagnosticId:string;appKey:string|null;traceId:string;code:string;event:string;
  revision:string;installId:string|null;instanceId:string|null;scope:string;timestamp:string;
}
interface Report {
  applications:ApplicationDiagnostic[];recent:Receipt[];
  metrics:{window:string;events:{event:string;code:string;count:number}[];
    activeInstances:number|null;activeInstancesStatus:string;launchSuccessRate:number|null;
    handshakeTimeouts:number;permissionDenials:number;quotaDenials:number;releaseFailures:number;
    directoryDurationMs:number[]};
}
const base='/v1/workbench/admin/diagnostics';
export function DiagnosticsPanel({token}:{token:string}) {
  const [report,setReport]=useState<Report|null>(null);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState('');
  const [user,setUser]=useState('');
  const [context,setContext]=useState('');
  const [notice,setNotice]=useState('');
  const [probing,setProbing]=useState(false);
  const [declaration,setDeclaration]=useState('');
  const request=useRef<AbortController|null>(null);
  const action=useRef<AbortController|null>(null);
  const english=getLang()==='en';
  const load=useCallback(async()=>{
    request.current?.abort();
    const controller=new AbortController();request.current=controller;
    setLoading(true);setError('');setReport(null);
    try {
      const result=await configFetch(token,base+(context?'?user_id='+encodeURIComponent(context):''),
        {signal:controller.signal});
      if(!controller.signal.aborted)setReport(result.data as Report);
    } catch {
      if(!controller.signal.aborted)setError(english?'Diagnostics unavailable or access denied.':'无法读取诊断或没有权限。');
    } finally {if(!controller.signal.aborted)setLoading(false);}
  },[token,context,english]);
  useEffect(()=>{setProbing(false);setNotice('');void load();return()=>{request.current?.abort();action.current?.abort();};},[load]);
  async function probe(key:string) {
    if(probing)return;
    const controller=new AbortController();action.current=controller;
    setProbing(true);setNotice('');
    try {
      const result=await configFetch(token,base+'/probe/'+encodeURIComponent(key),
        {method:'POST',body:'{}',signal:controller.signal});
      if(!controller.signal.aborted)setNotice('HTTP '+String(result.data.status));
    } catch {
      if(!controller.signal.aborted)setNotice(english?'Probe failed or target denied.':'探测失败或目标被安全规则拒绝。');
    } finally {if(!controller.signal.aborted)setProbing(false);}
  }
  async function preview() {
    if(probing)return;
    const controller=new AbortController();action.current=controller;
    setProbing(true);setNotice('');
    try {
      const value:unknown=JSON.parse(declaration);
      await configFetch(token,base+'/preview',
        {method:'POST',body:JSON.stringify({declaration:value}),signal:controller.signal});
      if(!controller.signal.aborted)setNotice(english?'Configuration is valid. No changes saved.':'配置有效。未保存任何变更。');
    } catch {
      if(!controller.signal.aborted)setNotice(english?'Invalid configuration or access denied.':'配置无效或没有权限。');
    } finally {if(!controller.signal.aborted)setProbing(false);}
  }
  return <section aria-label={english?'Application diagnostics':'应用诊断'} style={{margin:'24px 0'}}>
    <h2>{english?'Application diagnostics':'应用诊断'}</h2>
    <label>{english?'External declaration preview (JSON)':'外部应用声明预检查（JSON）'}
      <textarea value={declaration} maxLength={16000} rows={4}
        onChange={event=>setDeclaration(event.target.value)} /></label>
    <button type="button" onClick={()=>void preview()} disabled={probing || !declaration}>
      {english?'Validate without saving':'校验但不保存'}</button>
    <form onSubmit={event=>{event.preventDefault();setContext(user.trim());}}>
      <label>{english?'User ID for permission check':'用于权限检查的用户 ID'}
        <input value={user} maxLength={200} onChange={event=>setUser(event.target.value)} /></label>
      <button type="submit">{english?'Check access':'检查权限'}</button>
    </form>
    <button type="button" onClick={()=>void load()} disabled={loading}>{english?'Refresh':'刷新'}</button>
    {loading && <p role="status">{english?'Loading…':'正在加载…'}</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {report && <>
      {report.applications.length===0 && <p>{english?'No applications':'暂无应用'}</p>}
      <ul>{report.applications.map(app=><li key={app.applicationKey}>
        <code>{app.applicationKey}</code>{' · '}{app.source}{' · '}{app.scope}{' · '}{String(app.revision)}
        {' · '}{app.enabled?(english?'Enabled':'启用'):(english?'Disabled':'停用')}
        {' · '}{app.authentication}{' · '}{app.permissionStatus}{' · '}{app.availability.reason}
        {app.recentFailure && <span>{' · '}{app.recentFailure.code}{' · '}{app.recentFailure.diagnosticId}</span>}
        {app.source==='external' && <button type="button" disabled={probing}
          onClick={()=>void probe(app.applicationKey)}>{english?'Probe saved target':'探测已保存目标'}</button>}
      </li>)}</ul>
      <h3>{english?'Recent events':'最近事件'}</h3>
      {report.recent.length===0 && <p>{english?'No diagnostic events':'暂无诊断事件'}</p>}
      <ul>{report.recent.map(receipt=><li key={receipt.diagnosticId}>
        <code tabIndex={0}>{receipt.diagnosticId}</code>{' · '}{receipt.timestamp}{' · '}{receipt.event}
        {' · '}{receipt.code}{' · '}{receipt.appKey}{' · '}{receipt.revision}{' · '}{receipt.scope}
        {' · trace '}{receipt.traceId}{' · install '}{receipt.installId}{' · instance '}{receipt.instanceId}
      </li>)}</ul>
      <h3>{english?'Metrics (retained event window)':'指标（保留事件窗口）'}</h3>
      <dl>
        <dt>{english?'Launch success rate':'启动成功率'}</dt>
        <dd>{report.metrics.launchSuccessRate===null?'—':(report.metrics.launchSuccessRate*100).toFixed(1)+'%'}</dd>
        <dt>{english?'Active resource instances':'活跃资源实例'}</dt>
        <dd>{report.metrics.activeInstances===null?(english?'Unavailable':'暂不可用'):report.metrics.activeInstances}</dd>
        <dt>{english?'Handshake timeouts':'握手超时'}</dt><dd>{report.metrics.handshakeTimeouts}</dd>
        <dt>{english?'Permission denials':'权限拒绝'}</dt><dd>{report.metrics.permissionDenials}</dd>
        <dt>{english?'Quota denials':'配额拒绝'}</dt><dd>{report.metrics.quotaDenials}</dd>
        <dt>{english?'Release failures':'释放失败'}</dt><dd>{report.metrics.releaseFailures}</dd>
        <dt>{english?'Directory timing samples (ms)':'目录耗时样本（毫秒）'}</dt>
        <dd>{report.metrics.directoryDurationMs.slice(0,10).join(', ') || '—'}</dd>
      </dl>
      <ul>{report.metrics.events.map(metric=><li key={metric.event+metric.code}>
        {metric.event}{' / '}{metric.code}{': '}{metric.count}</li>)}</ul>
    </>}
  </section>;
}
