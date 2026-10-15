import {randomUUID} from './uuid';
import {assertActive, encodeBlob, safeFileName, safePlatformUrl,
 type PlatformAdapter,type PlatformBootstrap,type PlatformFile,type RuntimeCapabilities} from './platformAdapter';
export function createNativePlatform(boot:PlatformBootstrap,capabilities:()=>RuntimeCapabilities):PlatformAdapter {
 let disposed=false;
 const pending=new Set<AbortController>(),handles=new Set<string>();
 async function request<T>(params:unknown,signal?:AbortSignal):Promise<T>{
  assertActive(signal);if(disposed)throw new Error('session_changed');
  const controller=new AbortController();pending.add(controller);
  const requestId=randomUUID().replaceAll('-','');
  controller.signal.addEventListener('abort',()=>{
   void fetch('/__desktop/workbench/platform',{method:'POST',keepalive:true,
    headers:{'Content-Type':'application/json','X-Workbench-Native':boot.key},
    body:JSON.stringify({op:'cancel',requestId})}).catch(()=>{/* native epoch/expiry fences old state */});
  },{once:true});
  const cancel=()=>controller.abort();signal?.addEventListener('abort',cancel,{once:true});
  try {
   const response=await fetch('/__desktop/workbench/platform',{method:'POST',signal:controller.signal,
    headers:{'Content-Type':'application/json','X-Workbench-Native':boot.key},body:JSON.stringify({...params as Record<string,unknown>,requestId})});
   if(disposed)throw new Error('session_changed');assertActive(signal);
   if(!response.ok){let code='bridge_error';try{code=(await response.json()).code || code;}catch{/* no body */}
    throw new Error(/^[a-z_]{1,64}$/.test(code)?code:'bridge_error');}
   return await response.json() as T;
  } finally {pending.delete(controller);signal?.removeEventListener('abort',cancel);}
 }
 const release=(handle:string)=>{
  handles.delete(handle);
  void fetch('/__desktop/workbench/platform',{method:'POST',keepalive:true,
   headers:{'Content-Type':'application/json','X-Workbench-Native':boot.key},
   body:JSON.stringify({op:'releaseFile',handle})}).catch(()=>{/* native expiry fences unreachable grants */});
 };
 return {runtimeCapabilities:capabilities,
  async openExternal(url,signal,revalidate){await revalidate?.();assertActive(signal);await request({op:'openExternal',url:safePlatformUrl(url)},signal);},
  async pickFile(signal){
   const result=await request<PlatformFile|null>({op:'pickFile'},signal);
   if(result)handles.add(result.handle);return result;
  },
  async readFile(handle,signal){
   if(!handles.has(handle))throw new Error('permission_denied');
   const result=await request<{content:string}>({op:'readFile',handle},signal);
   const bytes=Uint8Array.from(atob(result.content),c=>c.charCodeAt(0));return new Blob([bytes]);
  },
  async saveFile(name,content,signal){
   safeFileName(name);const encoded=await encodeBlob(content);assertActive(signal);
   const result=await request<{saved:true}|null>({op:'saveFile',name,content:encoded},signal);return !!result?.saved;
  },
  releaseFile:release,
  reset(){for(const handle of handles)release(handle);pending.forEach(c=>c.abort());pending.clear();handles.clear();},
  dispose(){for(const handle of handles)release(handle);disposed=true;pending.forEach(c=>c.abort());pending.clear();handles.clear();},
 };
}
