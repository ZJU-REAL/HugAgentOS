import {safePlatformUrl,type PlatformAdapter} from './platformAdapter';
/** The host renders approval UI. It must call perform in the trusted click
 * callback so browser activation survives; plugin-supplied gesture is ignored. */
export interface PlatformApproval {
 authorize(method:string,signal:AbortSignal):Promise<void>;
 approve(method:string,detail:string,perform:()=>Promise<unknown>,signal:AbortSignal):Promise<unknown>;
}
export function createPlatformExtension(adapter:PlatformAdapter,approval:PlatformApproval) {
 return {capabilities:['platform.runtimeCapabilities',...(adapter.runtimeCapabilities().openExternal?['platform.openExternal']:[])],
  handlers:{
   'platform.runtimeCapabilities':()=>adapter.runtimeCapabilities(),
   'platform.openExternal':async(params:unknown,signal:AbortSignal)=>{
    const raw=params as {url?:unknown};
    if(!raw || typeof raw.url!=='string')throw new Error('invalid_params');
    const url=safePlatformUrl(raw.url);
    await approval.authorize('platform.openExternal',signal);
    if(signal.aborted)throw new Error('cancelled');
    return approval.approve('platform.openExternal',url,()=>adapter.openExternal(url,signal,()=>approval.authorize('platform.openExternal',signal)),signal);
   },
  },
  reset:()=>adapter.reset(),
 };
}
