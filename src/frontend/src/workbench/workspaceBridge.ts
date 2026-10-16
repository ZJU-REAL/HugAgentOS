import {PROTOCOL_VERSION} from '@hugagent/workbench-protocol';
import {validateMessage} from './messageBounds';
import {workspaceErrorCode} from './workspaceErrorCode';
export interface WorkspaceRoute {path:string;requestId:string}
export interface WorkspaceInfo {
 route?:WorkspaceRoute;
 protocolVersion:string; theme:'light'|'dark'; locale:string; capabilities:string[];
 context:{applicationKey:string;installId:string;packageRevision:string;instanceId?:string;origin?:{kind:'local'|'cloud';id:string};executionScope?:'local'|'cloud'};
}
export type WorkspaceHandler=(params:unknown,signal:AbortSignal)=>unknown|Promise<unknown>;
export interface WorkspaceExtension {
 handlers?:Record<string,WorkspaceHandler>; capabilities?:string[];
 /** Revoke host-only approvals at every document/session boundary. */
 reset?:()=>void;
}
export function attachWorkspaceBridge(frame:HTMLIFrameElement,initial:WorkspaceInfo,
 ready:()=>void,failed:(code:string)=>void,extension:WorkspaceExtension={}) {
 let disposed=false,port:MessagePort|undefined,nonce='',clientNonce='',active=false;
 let generation=0,lastRequestId=0,info=initial,loaded=false;
 const times:number[]=[];
 const calls=new Map<string,AbortController>();
 let handlers=new Map(Object.entries(extension.handlers??{}));
 function reset(){
  generation++;active=false;port?.close();port=undefined;
  calls.forEach(c=>c.abort());calls.clear();extension.reset?.();
  nonce='';lastRequestId=0;times.length=0;
 }
 function initialize(){
  if(disposed || port || !loaded || !frame.contentWindow || !clientNonce)return;
  const currentGeneration=++generation;
  const channel=new MessageChannel(),currentPort=channel.port1;
  const currentNonce=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
  port=currentPort;nonce=currentNonce;
  const valid=()=>!disposed && generation===currentGeneration && port===currentPort;
  const post=(value:Record<string,unknown>)=>{
   if(!valid())return;
   try{const body={...value,nonce:currentNonce};validateMessage(body);currentPort.postMessage(body);}
   catch{currentPort.postMessage({type:'result',id:value.id,ok:false,code:'invalid_result',nonce:currentNonce});}
  };
  currentPort.onmessage=e=>{
   if(!valid())return;
   const m=e.data;
   try{validateMessage(m);}catch{failed('invalid_message');return;}
   if(!m || m.nonce!==currentNonce)return;
   if(m.type==='ready'){if(m.protocolVersion!==PROTOCOL_VERSION){failed('host_incompatible');return;}active=true;ready();return;}
   if(m.type==='failed'){failed('host_incompatible');return;}
   if(m.type==='dispose'){reset();return;}
   if(m.type==='cancel' && typeof m.id==='string'){calls.get(m.id)?.abort();return;}
   if(m.type!=='request'||!active||typeof m.id!=='string'||typeof m.method!=='string')return;
   const id=m.id,requestId=Number(id);
   if(!/^[1-9][0-9]{0,15}$/.test(id)||!Number.isSafeInteger(requestId)||requestId<=lastRequestId)return;
   lastRequestId=requestId;
   const now=Date.now();
   while(times.length && now-times[0]>=10000)times.shift();
   if(times.length>=30){post({type:'result',id,ok:false,code:'rate_limited'});return;}
   times.push(now);
   if(calls.size>=8){post({type:'result',id,ok:false,code:'concurrency_limit'});return;}
   if(['__proto__','prototype','constructor'].includes(m.method)){post({type:'result',id,ok:false,code:'unknown_method'});return;}
   const handler=m.method==='host.info'?()=>info:handlers.get(m.method);
   if(!handler){post({type:'result',id,ok:false,code:'unknown_method'});return;}
   const controller=new AbortController();calls.set(id,controller);
   let completed=false;
   const settle=(value:Record<string,unknown>)=>{
    if(completed)return;completed=true;clearTimeout(timer);
    controller.signal.removeEventListener('abort',cancelled);
    if(valid()){calls.delete(id);post({type:'result',id,...value});}
   };
   const cancelled=()=>settle({ok:false,code:'cancelled'});
   const timer=setTimeout(()=>{settle({ok:false,code:'bridge_timeout'});controller.abort();},10000);
   controller.signal.addEventListener('abort',cancelled,{once:true});
   Promise.resolve().then(()=>{if(controller.signal.aborted)throw new Error('cancelled');return handler(m.params??{},controller.signal);})
    .then(data=>settle({ok:true,data}),error=>settle({ok:false,code:workspaceErrorCode(error)}));
  };
  currentPort.start();
  frame.contentWindow.postMessage({type:'workbench:init',nonce:currentNonce,clientNonce,protocolVersion:PROTOCOL_VERSION,info},'*',[channel.port2]);
 }
 const hello=(e:MessageEvent)=>{
  if(disposed||e.source!==frame.contentWindow||e.origin!=='null'||e.data?.type!=='workbench:hello')return;
  try{validateMessage(e.data);}catch{return;}
  if(e.data.protocolVersion!==PROTOCOL_VERSION || typeof e.data.clientNonce!=='string'||!/^[0-9a-f]{32}$/.test(e.data.clientNonce)){failed('host_incompatible');return;}
  if(clientNonce!==e.data.clientNonce){reset();clientNonce=e.data.clientNonce;}
  loaded=true;initialize();
 };
 const load=()=>{reset();clientNonce='';loaded=true;};
 window.addEventListener('message',hello);frame.addEventListener('load',load);
 return {suspend(){calls.forEach(controller=>controller.abort());},update(next:WorkspaceInfo){validateMessage(next);info=next;if(active)port?.postMessage({type:'update',info,nonce});},
  /** Trusted host changes routes/handlers without replacing the document channel. */
  updateExtension(next:WorkspaceExtension){
   if(disposed)return;extension=next;handlers=new Map(Object.entries(next.handlers??{}));
  },
  dispose(){if(disposed)return;port?.postMessage({type:'closed',nonce});disposed=true;reset();
   window.removeEventListener('message',hello);frame.removeEventListener('load',load);}};
}
