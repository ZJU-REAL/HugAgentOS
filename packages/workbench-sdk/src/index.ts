/** Browser-only SDK. Protocol is bundled into each plugin's own web assets. */
export { workspaceNavigation } from './navigation.js';
import type { WorkspaceRoute } from './navigation.js';
export const SDK_VERSION = '1.0.0';
import host from '@hugagent/workbench-protocol/schemas/host.json';
export const PROTOCOL_VERSION: string = host.protocolVersion;
export interface HostContext {
 applicationKey: string; installId: string; packageRevision: string; instanceId?:string;
 origin?:{kind:'local'|'cloud';id:string};executionScope?:'local'|'cloud';
}
export interface HostInfo {
 protocolVersion: string; theme: 'light' | 'dark'; locale: string;
 context: HostContext; capabilities: string[]; route?: WorkspaceRoute;
}
import {capabilityMethods, type CapabilityMethods} from './capabilities';
export type {CapabilityMethods, FileReference} from './capabilities';
export interface Connection extends CapabilityMethods {
 readonly info: HostInfo;
 request<T = unknown>(method: string, params?: Record<string, unknown>, signal?: AbortSignal): Promise<T>;
 onUpdate(listener: (info: HostInfo) => void): () => void;
 dispose(): void;
}
import {validateMessage} from './messageBounds';

export function connect(options: {timeoutMs?: number; requiredCapabilities?: string[]} = {}): Promise<Connection> {
 return new Promise((resolve,reject) => {
  let finished=false;
  const clientNonce=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
  const hello=()=>{if(window.document.readyState==='complete')window.parent.postMessage({type:'workbench:hello',protocolVersion:PROTOCOL_VERSION,clientNonce},'*');};
  const retry=setInterval(hello,200);
  const timer=setTimeout(()=>{if(!finished){finished=true;clearInterval(retry);window.removeEventListener('message',receive);window.removeEventListener('load',hello);reject(new Error('bridge_timeout'));}},Math.max(1,Math.min(8000,options.timeoutMs ?? 8000)));
  function receive(event: MessageEvent) {
   if(finished || event.source!==window.parent || event.data?.type!=='workbench:init') return;
   const data=event.data;
   try {validateMessage(data);} catch {return;}
   const port=event.ports[0];
   if(data.clientNonce!==clientNonce)return;
   if(data.protocolVersion!==PROTOCOL_VERSION || typeof data.nonce!=='string' ||
      data.nonce.length<16 || !port || !validInfo(data.info)) return;
   if(options.requiredCapabilities?.some(c=>!data.info.capabilities.includes(c))) {
    port.postMessage({type:'failed',nonce:data.nonce,code:'host_incompatible'}); port.close();
    finish(); reject(new Error('host_incompatible')); return;
   }
   finish();
   const connection=createConnection(port,data.nonce,data.info);
   port.postMessage({type:'ready',nonce:data.nonce,protocolVersion:PROTOCOL_VERSION});
   resolve(connection);
  }
  function finish(){finished=true;clearInterval(retry);clearTimeout(timer);window.removeEventListener('message',receive);window.removeEventListener('load',hello);}
  window.addEventListener('message',receive);
  window.addEventListener('load',hello,{once:true});
  hello();
 });
}
function validInfo(value: unknown): value is HostInfo {
 const x=value as HostInfo | undefined;
 return !!x && x.protocolVersion===PROTOCOL_VERSION && ['light','dark'].includes(x.theme) && typeof x.locale==='string'
  && Array.isArray(x.capabilities) && x.capabilities.every(c=>typeof c==='string')
  && (x.route === undefined || (!!x.route && typeof x.route.path==='string' && x.route.path.length<=4096 && typeof x.route.requestId==='string' && x.route.requestId.length<=64))
  && !!x.context && ['applicationKey','installId','packageRevision'].every(
    k=>typeof (x.context as unknown as Record<string,unknown>)[k]==='string');
}
function createConnection(port: MessagePort, nonce: string, initial: HostInfo): Connection {
 let disposed=false, sequence=0, info=initial;
 const listeners=new Set<(info:HostInfo)=>void>();
 const pending=new Map<string,{resolve:(value:unknown)=>void;reject:(error:Error)=>void;clear:()=>void}>();
 const send=(message:Record<string,unknown>)=>{
  if(disposed) throw new Error('session_closed');
  validateMessage(message);
  const body={...message,nonce};
  validateMessage(body);
  port.postMessage(body);
 };
 port.onmessage=event=>{
  const data=event.data;
  try {validateMessage(data);} catch {return;}
  if(disposed || data?.nonce!==nonce) return;
  if(data.type==='update' && validInfo(data.info)){info=data.info;listeners.forEach(fn=>fn(info));return;}
  if(data.type==='closed'){dispose();return;}
  if(data.type!=='result' || typeof data.id!=='string') return;
  const call=pending.get(data.id); if(!call)return;
  pending.delete(data.id);call.clear();
  if(data.ok===true)call.resolve(data.data);else call.reject(new Error(typeof data.code==='string'?data.code:'bridge_error'));
 };
 port.start();
 const ownerWindow=window;
 ownerWindow.addEventListener('pagehide',dispose,{once:true});
 function dispose(){
  if(disposed)return;
  try{send({type:'dispose'});}catch{/* Local cleanup must still run after port failure. */}finally{
   disposed=true;ownerWindow.removeEventListener('pagehide',dispose);port.close();listeners.clear();
   pending.forEach(call=>{call.clear();call.reject(new Error('session_closed'));});pending.clear();
  }
 }
 function request<T>(method:string,params:Record<string,unknown>={},signal?:AbortSignal):Promise<T>{
   if(disposed)return Promise.reject(new Error('session_closed'));
   if(signal?.aborted)return Promise.reject(new Error('cancelled'));
   if(pending.size>=8)return Promise.reject(new Error('concurrency_limit'));
   const id=String(++sequence);
   return new Promise<T>((resolve,reject)=>{
    const cancel=()=>cancelRequest('cancelled');
    const timer=setTimeout(()=>cancelRequest('bridge_timeout'),10000);
    function cancelRequest(code:string){
     if(!pending.has(id))return;
     settle(code);
     try{send({type:'cancel',id});}catch{/* Cancellation must settle even if the port has closed. */}
    }
    function clear(){clearTimeout(timer);signal?.removeEventListener('abort',cancel);}
    function settle(code:string){pending.delete(id);clear();reject(new Error(code));}
    pending.set(id,{resolve:v=>resolve(v as T),reject,clear});
    signal?.addEventListener('abort',cancel,{once:true});
    try{send({type:'request',id,method,params});}catch(error){pending.delete(id);clear();reject(error);}
   });
  }
 return {get info(){return info;},onUpdate(fn){listeners.add(fn);return ()=>listeners.delete(fn);},dispose,request,...capabilityMethods(request)};
}

export {resources} from './resources';

export {applicationState,type ApplicationSnapshot} from './state';


export {applicationSource} from './source';

export {runtimeCapabilities,openExternal,type RuntimeCapabilities} from './platform.js';

export { updateBadge, showNotification } from './activity';
