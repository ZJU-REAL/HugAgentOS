import {authFetch,getApiUrl} from '../api';
import {assertActive,safeFileName,type PlatformAdapter} from './platformAdapter';
export interface PlatformFileReference {fileId:string;name:string;size:number;mimeType?:string}
export interface PlatformFileScope {executionScope:'local'|'cloud';origin:string}
/** Device selection is uploaded only after explicit host approval. The
 * broker then selects it with the same owner and exact installation checks. */
export async function pickPlatformFile(
 adapter:PlatformAdapter,scope:PlatformFileScope,
 authorize:(signal:AbortSignal)=>Promise<void>,
 select:(fileId:string,signal:AbortSignal)=>Promise<PlatformFileReference>,signal:AbortSignal,
):Promise<PlatformFileReference|null> {
 // Caller has already checked the grant before showing its host confirmation.
 // Start the chooser before any await to retain the confirmation button activation.
 assertActive(signal);
 if(!['local','cloud'].includes(scope.executionScope)||!/^[0-9a-f]{32}$/.test(scope.origin))throw new Error('invalid_params');
 const selected=await adapter.pickFile(signal);if(!selected)return null;
 try {
  await authorize(signal);assertActive(signal);
  const blob=await adapter.readFile(selected.handle,signal);
  await authorize(signal);assertActive(signal);
  const form=new FormData();form.append('file',blob,safeFileName(selected.name));
  const response=await authFetch(getApiUrl()+'/v1/file/upload',{method:'POST',body:form,signal,
   headers:{'X-Hugagent-Target':scope.executionScope,'X-Workbench-Origin':scope.origin}});
  if(!response.ok)throw new Error('file_access_denied');
  const payload=await response.json() as {data?:{file_id?:unknown}};
  const id=payload.data?.file_id;
  if(typeof id!=='string'||!id||id.length>128)throw new Error('invalid_result');
  await authorize(signal);assertActive(signal);
  return await select(id,signal);
 } finally {adapter.releaseFile(selected.handle);}
}
export async function savePlatformFile(adapter:PlatformAdapter,name:string,blob:Blob,
 authorize:(signal:AbortSignal)=>Promise<void>,signal:AbortSignal):Promise<{saved:boolean}> {
 safeFileName(name);await authorize(signal);assertActive(signal);
 return {saved:await adapter.saveFile(name,blob,signal)};
}
