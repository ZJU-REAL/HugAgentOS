import { useEffect } from 'react';
import { create } from 'zustand';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import type { ExternalApplication } from './externalApi';
import {setHostedWorkbenchOrigin} from './sourceRequest';
import {sourceDirectory, projectedSource, type SourceDirectory} from './sourceCatalog';
import {directoryPollDelay} from './directoryTransport';
import {workbenchRequest} from './sourceRequest';
import {identityKey,reconcileWorkbenchSessions,subscribeDirectoryInvalidation} from './invalidation';
import {sourceTargets, type WorkbenchOrigin, type WorkbenchSource, type SourceFailure} from './sourceIdentity';

export interface CatalogItem extends Omit<ExternalApplication, 'revision'> {
  revision: number | string;
  legacy?: boolean;
  origin?: WorkbenchOrigin;
  binding?: {installId:string;packageRevision:string;slug:string;executionScope:WorkbenchSource;origin:WorkbenchOrigin};
  availability: { available: boolean; reason: string | null };
  navigation: { defaultPinned: boolean; forced: boolean };
}
export interface LayoutPolicy { revision: number; defaultPinned: string[]; forced: string[]; hidden?: string[] }
interface CatalogState {
  userId?: string; contextId: string; items: CatalogItem[]; policy: LayoutPolicy;
  loading: boolean; error: string; sourceErrors: SourceFailure[]; identity:string; revision:string;
}
const emptyPolicy: LayoutPolicy = { revision: 0, defaultPinned: [], forced: [] };
const useDirectory = create<CatalogState>(() => ({
  identity:'',revision:'',contextId:'', items: [], policy: emptyPolicy, loading: true, error: '', sourceErrors:[],
}));
let request: AbortController | undefined;
let owner = '';
let consumers = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let unsubscribe: (()=>void) | undefined;
let failures=0;
let events:AbortController|undefined;
let invalidationUnsubscribe:(()=>void)|undefined;
let snapshots: Partial<Record<WorkbenchSource, SourceDirectory>> = {};
function context() {
  const mode=useDeploymentModeStore.getState();
  const userId=useAuthStore.getState().authUser?.user_id;
  return {mode,userId, id:identityKey()};
}
function failure(source:WorkbenchSource, error:unknown):SourceFailure {
  const value=error as {message?:string;status?:number;payload?:{detail?:{code?:string}}};
  const code=value.payload?.detail?.code || (value.status===401 ? 'authentication_required' :
    value.message?.includes('execution_plane_not_ready') ? 'execution_plane_not_ready' :
    value.message?.includes('origin_mismatch') ? 'origin_mismatch' : 'origin_unavailable');
  return {source,code};
}
export async function reloadDirectory() {
  const current=context();
  request?.abort();
  const controller = new AbortController();
  request = controller;
  if(owner!==current.id) {
    owner=current.id; snapshots={}; events?.abort();events=undefined;failures=0; setHostedWorkbenchOrigin();
    useDirectory.setState({userId:current.userId,contextId:current.id,identity:identityKey(),revision:'',items:[],policy:emptyPolicy,loading:true,error:'',sourceErrors:[]});
  } else useDirectory.setState({loading:true});
  if(!current.userId || useAuthStore.getState().loggingOut){
   events?.abort();events=undefined;clearTimeout(timer);snapshots={};
   useDirectory.setState({items:[],policy:emptyPolicy,loading:false,error:'',sourceErrors:[],revision:''});return;
  }
  const sources=sourceTargets(current.mode);
  const results=await Promise.allSettled(sources.map(source=>sourceDirectory(source,controller.signal,snapshots[source])));
  if(controller.signal.aborted || context().id!==current.id)return;
  const errors:SourceFailure[]=[];
  const items:CatalogItem[]=[];
  results.forEach((result,index)=>{
    const source=sources[index];
    if(result.status==='fulfilled')snapshots[source]=result.value;
    else {
      errors.push(failure(snapshots[source]?.origin.kind ?? source,result.reason));
      const error=result.reason as {status?:number};
      if((error.status && error.status<500) || errors.at(-1)?.code==='origin_mismatch') delete snapshots[source];
    }
    const snapshot=snapshots[source];
    if(!snapshot)return;
    const unavailable=result.status==='rejected';
    for(const item of snapshot.items) {
      if(sources.length>1 && source==='local' && item.declaration.target.kind!=='plugin')continue;
      if(unavailable && item.declaration.target.kind!=='plugin')continue;
      const projected=projectedSource(item,typeof navigator==='undefined'||navigator.onLine);
      items.push(unavailable ? {...projected,availability:{available:false,reason:errors.at(-1)!.code}} : projected);
    }
  });
  const primary=snapshots[sources[0]];
  const visible=new Set(items.map(item=>item.applicationKey));
  const policy=primary ? {
    ...primary.policy,
    defaultPinned:[...new Set(sources.flatMap(source=>snapshots[source]?.policy.defaultPinned || []))].filter(key=>visible.has(key)),
    forced:[...new Set(sources.flatMap(source=>snapshots[source]?.policy.forced || []))].filter(key=>visible.has(key)),
  } : emptyPolicy;
  reconcileWorkbenchSessions(items);
  useDirectory.setState({userId:current.userId,contextId:current.id,identity:identityKey(),revision:JSON.stringify(sources.map(source=>snapshots[source]?.revision)),items,policy,loading:false,
    error:errors.length===sources.length?'应用目录加载失败':'',sourceErrors:errors});
  failures=errors.length?failures+1:0;
  clearTimeout(timer);if(consumers)timer=setTimeout(()=>void reloadDirectory(),directoryPollDelay(failures));
  if(!events && !errors.length){
   const watcher=new AbortController();events=watcher;
   void Promise.allSettled(sources.map(async source=>{
    while(!watcher.signal.aborted && context().id===current.id){
     const revision=snapshots[source]?.revision;if(!revision)return;
     const result=await workbenchRequest<{data:{revision:string}}>('/v1/workbench/catalog/events?revision='+encodeURIComponent(revision),{signal:watcher.signal},source);
     if(result?.data?.revision && result.data.revision!==revision)await reloadDirectory();
    }
   })).finally(()=>{if(events===watcher)events=undefined;});
  }
}
export function useApplicationDirectory() {
  const userId = useAuthStore(s => s.authUser?.user_id);
  const mode=useDeploymentModeStore(s=>s.provisionMode);
  const server=useDeploymentModeStore(s=>s.serverBase);
  const state = useDirectory();
  useEffect(() => {
    const first = consumers++ === 0;
    if (first) {
      window.addEventListener('focus', reloadDirectory);
      window.addEventListener('online', reloadDirectory);
      window.addEventListener('offline', reloadDirectory);
      invalidationUnsubscribe=subscribeDirectoryInvalidation(()=>void reloadDirectory());
      unsubscribe=useDeploymentModeStore.subscribe((next,previous)=>{
        if(next.localReady!==previous.localReady || next.localBase!==previous.localBase)void reloadDirectory();
      });
    }
    if (first || owner !== context().id) void reloadDirectory();
    return () => {
      if (--consumers === 0) {
        window.removeEventListener('focus', reloadDirectory);
        window.removeEventListener('online', reloadDirectory);
        window.removeEventListener('offline', reloadDirectory);
        clearTimeout(timer); events?.abort();events=undefined;invalidationUnsubscribe?.(); unsubscribe?.(); unsubscribe=undefined;
        request?.abort(); request = undefined;
      }
    };
  }, [userId,mode,server]);
  return state.contextId===context().id ? state : {
    userId,identity:identityKey(),revision:'',contextId:context().id,items:[],policy:emptyPolicy,loading:true,error:'',sourceErrors:[],
  };
}
