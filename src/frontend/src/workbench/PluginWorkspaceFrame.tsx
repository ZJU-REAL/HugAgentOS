import {PROTOCOL_VERSION} from '@hugagent/workbench-protocol';
import {useEffect,useMemo,useRef,useState} from 'react';
import type {ModuleContribution} from '../plugin-ui/types';
import {attachWorkspaceBridge,type WorkspaceInfo,type WorkspaceExtension,type WorkspaceRoute} from './workspaceBridge';
import {t} from '../i18n';
import {identityKey,registerWorkbenchSession} from './invalidation';
import {createCapabilityHost} from './capabilityHost';
import {useWorkspaceConfirmation} from './WorkspaceCapabilityDialog';
import {combineExtensions} from './combineExtensions';
import {workspaceCapabilities} from './workspaceCapabilities';
import {usePlatformExtension} from './usePlatformExtension';
import {useRevisionLease} from './useRevisionLease';
import {DiagnosticFailure} from './DiagnosticFailure';
import {applicationText} from './externalApi';
import {packageAssetPath,sourceLabel,type WorkbenchOrigin,type WorkbenchSource} from './sourceIdentity';
export interface WorkspaceLaunch {
 applicationKey:string;revision:string;target:{kind:'plugin';moduleId:string};
 binding:{installId:string;packageRevision:string;slug:string;origin:WorkbenchOrigin;executionScope:WorkbenchSource};
 module:ModuleContribution;assetPath:string;context:WorkspaceInfo['context'];
}
export function PluginWorkspaceFrame({launch,theme,locale,onRetry,extension,onReady,onFailed,onLoading,route,ownerScope,signal,instanceId}:{
 launch:WorkspaceLaunch;theme:'light'|'dark';locale:string;onRetry:()=>void;extension?:WorkspaceExtension;
 instanceId:string;ownerScope?:string;signal?:AbortSignal;route?:WorkspaceRoute;onReady?:()=>void;onLoading?:()=>void;onFailed?:(code:string)=>void;
}){
 const frame=useRef<HTMLIFrameElement>(null);
 const {confirm,dialog}=useWorkspaceConfirmation();
 const host=useMemo(()=>createCapabilityHost(launch,confirm),[launch,confirm]);
 const platform=usePlatformExtension(host.authorize);
 host.attachPlatform(platform.adapter,platform.approve);
 const combined=useMemo<WorkspaceExtension>(()=>{
  const merged=combineExtensions(host,platform.extension,extension??{});
  return workspaceCapabilities(merged,launch.module.grants??[],extension);
 },[host,platform.extension,extension,launch.module.grants]);
 const [status,setStatus]=useState<'loading'|'ready'|'failed'>('loading');
 const [failureCode,setFailureCode]=useState<'bridge_timeout'|'page_failed'>('page_failed');
 const bridge=useRef<ReturnType<typeof attachWorkspaceBridge>|null>(null);
 const current=useRef({onReady,onFailed,onLoading});
 useEffect(()=>{current.current={onReady,onFailed,onLoading};},[onReady,onFailed,onLoading]);
 const lease=useRevisionLease(launch,()=>{
  bridge.current?.dispose();bridge.current=null;frame.current?.setAttribute('src','about:blank');
  setStatus('failed');current.current.onFailed?.('permission_denied');
 },instanceId);
 const {canInvoke}=lease;
 const guarded=useMemo<WorkspaceExtension>(()=>({...combined,
  handlers:Object.fromEntries(Object.entries(combined.handlers??{}).map(([method,handler])=>[
   method,(params:unknown,signal:AbortSignal)=>{
    if(!canInvoke())throw new Error('connection_recovering');
    return handler(params,signal);
   },
  ])),
 }),[combined,canInvoke]);
 const info=useMemo<WorkspaceInfo>(()=>({protocolVersion:PROTOCOL_VERSION,theme,locale,
  capabilities:['applications','workspace','host.info',...(combined.capabilities??[])],context:launch.context,route}),[theme,locale,launch,combined,route]);
 const initial=useRef(info),currentExtension=useRef(guarded);
 const identity=useRef(ownerScope||identityKey());
 useEffect(()=>{initial.current=info;currentExtension.current=guarded;},[info,guarded]);
 useEffect(()=>{
  if(!frame.current||!lease.ready)return;
  let ended=false;
  let timer:ReturnType<typeof setTimeout>|undefined;
  const documentFrame=frame.current;
  const clearTimer=()=>{if(timer!==undefined){clearTimeout(timer);timer=undefined;}};
  const fail=(code='bridge_timeout')=>{
   if(ended)return;ended=true;setFailureCode(code==='bridge_timeout'?'bridge_timeout':'page_failed');
   clearTimer();bridge.current?.dispose();bridge.current=null;setStatus('failed');current.current.onFailed?.(code);
  };
  const attached=attachWorkspaceBridge(documentFrame,initial.current,
   ()=>{if(!ended){clearTimer();setStatus('ready');current.current.onReady?.();}},fail,currentExtension.current);
  bridge.current=attached;
  // Each new document must handshake. A ready document cannot clear the next one's timeout.
  const documentLoaded=()=>{
   if(ended)return;
   clearTimer();setStatus('loading');current.current.onLoading?.();timer=setTimeout(fail,8000);
  };
  documentFrame.addEventListener('load',documentLoaded);
  documentLoaded();
  const revoke=()=>{ended=true;clearTimer();attached.dispose();bridge.current=null;documentFrame.setAttribute('src','about:blank');setStatus('failed');};
  const unregister=registerWorkbenchSession({identity:identity.current,applicationKey:launch.applicationKey,revision:launch.revision,revoke});
  signal?.addEventListener('abort',revoke,{once:true});if(signal?.aborted)revoke();
  return ()=>{documentFrame.removeEventListener('load',documentLoaded);unregister();signal?.removeEventListener('abort',revoke);ended=true;clearTimer();attached.dispose();bridge.current=null;};
 },[launch,host,signal,lease.ready]);
 useEffect(()=>bridge.current?.updateExtension(guarded),[guarded]);
 useEffect(()=>bridge.current?.update(info),[info]);
 useEffect(()=>{if(lease.status==='recovering')bridge.current?.suspend();},[lease.status]);
 const prefix=(import.meta.env.VITE_API_BASE_URL||'/api').replace(/\/$/,'');
 const src=prefix+packageAssetPath(launch.assetPath,launch.applicationKey)+'&instanceId='+encodeURIComponent(instanceId);
 if(status==='failed')return <DiagnosticFailure applicationKey={launch.applicationKey} code={failureCode} onRetry={onRetry}/>;
 return <section aria-label={t('插件工作区')} style={{height:'100%',minHeight:400}}>
  {dialog}{platform.dialog}
  {lease.status==='recovering'&&<p role="status">{t('连接中断，正在恢复。应用操作已暂停。')}</p>}
  {status==='loading'&&<p role="status">{t('正在加载插件模块…')}</p>}
  {lease.ready&&<iframe ref={frame} src={src} title={applicationText(launch.module.title)+' · '+t(sourceLabel(launch.binding.origin))}
   sandbox="allow-scripts" referrerPolicy="no-referrer" style={{border:0,width:'100%',height:'100%',minHeight:400}}/>}
 </section>;
}
