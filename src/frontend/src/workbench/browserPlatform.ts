import {randomUUID} from './uuid';
import { assertActive, PLATFORM_FILE_LIMIT, safeFileName, safePlatformUrl,
 type PlatformAdapter, type RuntimeCapabilities } from './platformAdapter';
/** Browser file grants are snapshots, scoped to this adapter's identity lifetime. */
export function createBrowserPlatform(capabilities: ()=>RuntimeCapabilities): PlatformAdapter {
 const files=new Map<string,File>(),cancelPickers=new Set<()=>void>();let ended=false;
 const check=(signal?:AbortSignal)=>{assertActive(signal);if(ended)throw new Error('session_changed');};
 return {
  runtimeCapabilities:capabilities,
  async openExternal(raw,signal,revalidate){check(signal);
   const url=safePlatformUrl(raw);
   const opened=window.open('about:blank','_blank');
   if(!opened)throw new Error('popup_blocked');
   opened.opener=null;opened.document.body.textContent='Authorizing application link...';
   try{await revalidate?.();check(signal);
    if(opened.closed)throw new Error('cancelled');
    const link=opened.document.createElement('a');link.href=url;
    link.rel='noopener noreferrer';link.referrerPolicy='no-referrer';
    opened.document.body.replaceChildren(link);link.click();
   }catch(error){opened.close();throw error;}
  },
  pickFile(signal){check(signal);
   if(files.size>=64)return Promise.reject(new Error('resource_limit'));
   return new Promise((resolve,reject)=>{
    const input=document.createElement('input');input.type='file';input.hidden=true;
    const timer=setTimeout(()=>finish(undefined,new Error('bridge_timeout')),120000);
    const abort=()=>finish(undefined,new Error('cancelled'));
    const finish=(file?:File,error?:Error)=>{
     clearTimeout(timer);cancelPickers.delete(abort);signal?.removeEventListener('abort',abort);input.remove();
     if(error){reject(error);return;}try{check(signal);}catch(error){reject(error);return;}
     if(!file){resolve(null);return;}
     if(file.size>PLATFORM_FILE_LIMIT){reject(new Error('file_too_large'));return;}
     const handle=randomUUID();files.set(handle,file);resolve({handle,name:file.name,size:file.size});
    };
    input.onchange=()=>finish(input.files?.[0]);input.oncancel=()=>finish();
    cancelPickers.add(abort);signal?.addEventListener('abort',abort,{once:true});document.body.append(input);input.click();
   });
  },
  async readFile(handle,signal){check(signal);const file=files.get(handle);
   if(!file)throw new Error('permission_denied');return file;},
  async saveFile(name,content,signal){check(signal);safeFileName(name);
   if(content.size>PLATFORM_FILE_LIMIT)throw new Error('file_too_large');
   const url=URL.createObjectURL(content),link=document.createElement('a');
   try{link.href=url;link.download=name;link.rel='noopener';document.body.append(link);link.click();}
   finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}return true;},
  releaseFile(handle){files.delete(handle);},
  reset(){cancelPickers.forEach(cancel=>cancel());cancelPickers.clear();files.clear();},
  dispose(){ended=true;cancelPickers.forEach(cancel=>cancel());cancelPickers.clear();files.clear();},
 };
}
