import {useCallback,useEffect,useRef,useState} from 'react';
import {workbenchRequest} from './sourceRequest';
import {RevisionLease,type LeaseReceipt,type LeaseStatus} from './revisionLease';
import type {WorkspaceLaunch} from './PluginWorkspaceFrame';

export function useRevisionLease(launch:WorkspaceLaunch,onRevoked:()=>void,instanceId:string) {
 const {applicationKey,revision,binding:{origin,executionScope}} = launch;
 const callback=useRef(onRevoked);
 useEffect(()=>{callback.current=onRevoked;},[onRevoked]);
 const current=useRef<RevisionLease|null>(null);
 const identity=JSON.stringify([applicationKey,revision,origin.id,executionScope,instanceId]);
 const [state,setState]=useState<{identity:string;status:LeaseStatus}>({identity,status:'creating'});
 useEffect(()=>{
  let ended=false;
  const path='/v1/workbench/applications/'+encodeURIComponent(applicationKey)+'/revision-leases';
  const body=JSON.stringify({revision,instanceId});
  const request=(method:string,suffix='',signal?:AbortSignal)=>
   workbenchRequest<{data:LeaseReceipt}>(path+suffix,{method,body,signal},executionScope);
  const lease=new RevisionLease({
   instanceId,revision,
   request:async(method,signal)=>(await request(method,'',signal)).data,
   close:()=>request('POST','/close'),
   changed:status=>{
    if(ended)return;
    setState({identity,status});
    if(status==='revoked'||status==='expired')callback.current();
   },
  });
  current.current=lease;lease.start();
  window.addEventListener('focus',lease.check);
  document.addEventListener('visibilitychange',lease.check);
  return ()=>{
   ended=true;current.current=null;lease.dispose();
   window.removeEventListener('focus',lease.check);
   document.removeEventListener('visibilitychange',lease.check);
  };
 },[applicationKey,revision,origin.id,executionScope,instanceId,identity]);
 const canInvoke=useCallback(()=>current.current?.canInvoke()??false,[]);
 const status=state.identity===identity?state.status:'creating';
 return {
  ready:['ready','recovering'].includes(status),status,instanceId,
  canInvoke,
 };
}
