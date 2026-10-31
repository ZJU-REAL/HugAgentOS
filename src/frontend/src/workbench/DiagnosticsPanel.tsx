import {useCallback, useEffect, useRef, useState} from 'react';
import {configFetch} from '../utils/adminApi';
import {getLang} from '../i18n';
import {HostButton, HostField, HostInput, HostMeta, HostNotice, HostSection, HostTextArea, HostToolbar} from './ui';
import {SkeletonLines} from '../components/common/LoadState';

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
const RECENT_PAGE=20;
/** Joins the fields that have a value; a missing field adds no separator. */
function facts(...values:(string|number|null|undefined|false)[]) {
  return values.filter(value=>value!==null&&value!==undefined&&value!==false&&value!=='').join(' · ');
}
export function DiagnosticsPanel({token}:{token:string}) {
  const [report,setReport]=useState<Report|null>(null);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState('');
  const [user,setUser]=useState('');
  const [context,setContext]=useState('');
  const [notice,setNotice]=useState('');
  const [probing,setProbing]=useState(false);
  const [declaration,setDeclaration]=useState('');
  const [allRecent,setAllRecent]=useState(false);
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
  const metrics=report?.metrics;
  const stats:[string,string|number][]=metrics?[
    [english?'Launch success rate':'启动成功率',metrics.launchSuccessRate===null?'—':(metrics.launchSuccessRate*100).toFixed(1)+'%'],
    [english?'Active resource instances':'活跃资源实例',metrics.activeInstances===null?(english?'Unavailable':'暂不可用'):metrics.activeInstances],
    [english?'Handshake timeouts':'握手超时',metrics.handshakeTimeouts],
    [english?'Permission denials':'权限拒绝',metrics.permissionDenials],
    [english?'Quota denials':'配额拒绝',metrics.quotaDenials],
    [english?'Release failures':'释放失败',metrics.releaseFailures],
  ]:[];
  return <HostSection level={2} title={english?'Application diagnostics':'应用诊断'}
      actions={<HostButton tone="text" onClick={()=>void load()} disabled={loading}>{english?'Refresh':'刷新'}</HostButton>}>
      <HostField label={english?'External declaration preview (JSON)':'外部应用声明预检查（JSON）'}>
        <HostTextArea value={declaration} maxLength={16000} rows={4}
          onChange={event=>setDeclaration(event.target.value)} /></HostField>
      <HostToolbar>
        <HostButton onClick={()=>void preview()} disabled={probing || !declaration}>
          {english?'Validate without saving':'校验但不保存'}</HostButton>
      </HostToolbar>
      <form className="jx-hostInlineForm" onSubmit={event=>{event.preventDefault();setContext(user.trim());}}>
        <HostField label={english?'User ID for permission check':'用于权限检查的用户 ID'}>
          <HostInput value={user} maxLength={200} onChange={event=>setUser(event.target.value)} /></HostField>
        <HostButton submit>{english?'Check access':'检查权限'}</HostButton>
      </form>
      {loading && <SkeletonLines rows={3}/>}
      {error && <HostNotice tone="error">{error}</HostNotice>}
      {notice && <HostNotice>{notice}</HostNotice>}
      {report && <>
        {report.applications.length===0 && <HostNotice>{english?'No applications':'暂无应用'}</HostNotice>}
        <ul className="jx-hostList">{report.applications.map(app=><li key={app.applicationKey} className="jx-hostList-row">
          <span className="jx-hostList-title"><code>{app.applicationKey}</code></span>
          <HostMeta>{facts(app.source,app.scope,String(app.revision),
            app.enabled?(english?'Enabled':'启用'):(english?'Disabled':'停用'),
            app.authentication,app.permissionStatus,app.availability.reason,
            app.recentFailure?.code,app.recentFailure?.diagnosticId)}</HostMeta>
          {app.source==='external' && <HostButton tone="text" size="small" disabled={probing}
            onClick={()=>void probe(app.applicationKey)}>{english?'Probe saved target':'探测已保存目标'}</HostButton>}
        </li>)}</ul>
      </>}
    {report && metrics && <>
      <HostSection title={english?'Recent events':'最近事件'}>
        {report.recent.length===0 && <HostNotice>{english?'No diagnostic events':'暂无诊断事件'}</HostNotice>}
        <ul className="jx-hostList">{(allRecent?report.recent:report.recent.slice(0,RECENT_PAGE)).map(receipt=><li key={receipt.diagnosticId} className="jx-hostList-row">
          <span className="jx-hostList-title"><code tabIndex={0}>{receipt.diagnosticId}</code></span>
          <HostMeta>{facts(receipt.timestamp,receipt.event,receipt.code,receipt.appKey,receipt.revision,receipt.scope,
            receipt.traceId&&'trace '+receipt.traceId,receipt.installId&&'install '+receipt.installId,
            receipt.instanceId&&'instance '+receipt.instanceId)}</HostMeta>
        </li>)}</ul>
        {report.recent.length>RECENT_PAGE && <HostToolbar>
          <HostButton tone="text" size="small" onClick={()=>setAllRecent(value=>!value)}>
            {allRecent?(english?'Show fewer':'收起'):(english?'Show all '+report.recent.length:'显示全部 '+report.recent.length+' 条')}</HostButton>
        </HostToolbar>}
      </HostSection>
      <HostSection title={english?'Metrics (retained event window)':'指标（保留事件窗口）'}>
        <dl className="jx-hostStats">{stats.map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
        <HostMeta>{english?'Directory timing samples (ms)':'目录耗时样本（毫秒）'}{': '}
          {metrics.directoryDurationMs.slice(0,10).join(', ') || '—'}</HostMeta>
        <ul className="jx-hostList">{metrics.events.map(metric=><li key={metric.event+metric.code} className="jx-hostList-row">
          <span className="jx-hostList-title">{metric.event}{' / '}{metric.code}</span><HostMeta>{metric.count}</HostMeta></li>)}</ul>
      </HostSection>
    </>}
  </HostSection>;
}
