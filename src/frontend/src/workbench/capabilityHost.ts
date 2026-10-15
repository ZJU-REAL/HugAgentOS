import {t} from '../i18n';
import {sourceLabel} from './sourceIdentity';
import {workbenchRequest as apiRequest,workbenchFetch} from './sourceRequest';
import type {WorkspaceLaunch} from './PluginWorkspaceFrame';
import type {PlatformAdapter} from './platformAdapter';
import type {PlatformApproval} from './platformExtension';
import {pickPlatformFile,savePlatformFile} from './platformFiles';
import type {WorkspaceExtension} from './workspaceBridge';
import {workspaceActions} from './workspaceActions';
import {validateMessage} from './messageBounds';
export interface FileReference {fileId:string;name:string;size:number;mimeType?:string}
export interface HostPrompt {
 kind:'approval'|'file'|'save'|'compose'|'navigate'; detail:string;
 files?:FileReference[];
}
export type HostConfirm=(prompt:HostPrompt,signal:AbortSignal)=>Promise<string>;
interface Approval {approvalId:string;requested:string[];approved:string[];effective:string[]}
const IMPLEMENTED=['platform.openExternal','data.query','file.pick','file.save','chat.compose','navigation.open'];
function object(value:unknown):Record<string,unknown>{
 if(!value || typeof value!=='object'||Array.isArray(value))throw new Error('invalid_params');
 if(Object.keys(value).some(key=>['__proto__','prototype','constructor'].includes(key)))throw new Error('invalid_params');
 return value as Record<string,unknown>;
}
function text(value:unknown,max=256):string {
 if(typeof value!=='string'||!value||value.length>max)throw new Error('invalid_params');return value;
}
export interface CapabilityHost extends WorkspaceExtension {
 authorize:(method:string,signal:AbortSignal)=>Promise<void>;
 attachPlatform:(adapter:PlatformAdapter,approve:PlatformApproval['approve'])=>void;
}
export function createCapabilityHost(launch:WorkspaceLaunch,confirm:HostConfirm):CapabilityHost {
 let platform:{adapter:PlatformAdapter;approve:PlatformApproval['approve']}|undefined;
 const root='/v1/workbench/applications/'+encodeURIComponent(launch.applicationKey)+'/capabilities';
 const requested=new Set(launch.module.grants??[]);
 const actions=workspaceActions();
 const supported=IMPLEMENTED.filter(c=>c!=='chat.compose'&&c!=='navigation.open'
  || (c==='chat.compose'?!!actions.compose:!!actions.navigate));
 let generation=0,approval:Approval|undefined,approving:Promise<Approval>|undefined;
 const body=(extra:Record<string,unknown>,id=approval?.approvalId)=>({revision:launch.binding.packageRevision,instanceId:launch.context.instanceId,approvalId:id,...extra});
 async function call<T>(suffix:string,payload:Record<string,unknown>,signal?:AbortSignal):Promise<T>{
  const result=await apiRequest<{data:T}>(root+suffix,{method:'POST',body:JSON.stringify(payload),signal});
  return result.data;
 }
 async function grant(capability:string,signal:AbortSignal):Promise<Approval>{
  if(!requested.has(capability)||!supported.includes(capability))throw new Error('capability_denied');
  const epoch=generation;
  if(!approval){
   if(!approving)approving=(async()=>{
    const grants=[...requested].filter(c=>supported.includes(c)||c.startsWith('data_source:'));
    await confirm({kind:'approval',detail:grants.join(', ')},signal);
    const result=await call<Approval>('/approval',{revision:launch.binding.packageRevision,capabilities:grants,moduleId:launch.module.id,instanceId:launch.context.instanceId},signal);
    if(epoch!==generation||signal.aborted){void revoke(result);throw new Error('cancelled');}
    approval=result;return result;
   })().finally(()=>{if(epoch===generation)approving=undefined;});
   await approving;
  }
  if(epoch!==generation||signal.aborted||!approval?.effective.includes(capability))throw new Error('capability_denied');
  await call('/check',body({capability}),signal);
  return approval;
 }
 async function revoke(value:Approval){
  try{await apiRequest(root+'/approval/'+encodeURIComponent(value.approvalId)+'?revision='+encodeURIComponent(launch.binding.packageRevision),
   {method:'DELETE'});}catch{/* Expiry and per-operation checks remain authoritative. */}
 }
 function reference(value:FileReference):FileReference{
  return {fileId:value.fileId,name:value.name,size:value.size,...(value.mimeType?{mimeType:value.mimeType}:{})};
 }
 return {authorize:async(method,signal)=>{await grant(method,signal);},
 attachPlatform:(adapter,approve)=>{platform={adapter,approve};},capabilities:supported.filter(c=>requested.has(c)),reset(){
  generation++;const old=approval;approval=undefined;approving=undefined;if(old)void revoke(old);
 },handlers:{
  'data.query':async(params,signal)=>{
   const p=object(params),source=text(p.source,64);const approved=await grant('data.query',signal);
   if(!approved.effective.includes('data_source:'+source))throw new Error('data_source_denied');
   const result=await call('/data/query',body({sourceId:source,params:object(p.params??{})}),signal);
   validateMessage(result);return result;
  },
  'file.pick':async(params,signal)=>{
   const p=object(params);if(p.accept!==undefined)text(p.accept,200);
   await grant('file.pick',signal);
   if(p.source==='device'){
    if(!platform)throw new Error('unsupported_capability');
    return platform.approve('file.pick',t('选择设备文件并上传到')+' '+t(sourceLabel(launch.binding.origin)),()=>pickPlatformFile(platform!.adapter,{executionScope:launch.binding.executionScope,origin:launch.binding.origin.id},async s=>{await grant('file.pick',s);},async(fileId,s)=>reference(await call<FileReference>('/files/select',body({fileId}),s)),signal),signal);
   }
   if(p.source!==undefined&&p.source!=='personal')throw new Error('invalid_params');
   const result=(await apiRequest<{data:{items:{file_id?:string;name:string;size?:number;mime_type?:string}[]}}>('/v1/artifacts?scope=personal&page_size=100',{signal,headers:{'x-workbench-origin':launch.binding.origin.id}},launch.binding.executionScope)).data;
   if(signal.aborted)throw new Error('cancelled');
   const files=result.items.filter(f=>f.file_id).map(f=>({fileId:f.file_id!,name:f.name,size:f.size??0,mimeType:f.mime_type}));
   const fileId=await confirm({kind:'file',detail:'',files},signal);
   const selected=await call<FileReference>('/files/select',body({fileId}),signal);
   return reference(selected);
  },
  'file.save':async(params,signal)=>{
   const p=object(params),fileId=text(p.fileId,128);
   await grant('file.save',signal);
   const selected=await call<FileReference&{resourceUrl:string}>('/files/resource',body({fileId}),signal);
   const name=p.name===undefined?selected.name:text(p.name,200);
   if((/[\\/]/.test(name)||Array.from(name).some(c=>c.charCodeAt(0)<32)))throw new Error('invalid_params');
   await confirm({kind:'save',detail:name},signal);
   const response=await workbenchFetch(selected.resourceUrl,{signal,headers:{'X-Workbench-Approval':approval!.approvalId}} ,launch.binding.executionScope);
   if(!response.ok)throw new Error('file_access_denied');
   const blob=await response.blob();if(signal.aborted)throw new Error('cancelled');
   await grant('file.save',signal);
   if(platform&&platform.adapter.runtimeCapabilities().platform!=='web')return platform.approve('file.save',name,()=>savePlatformFile(platform!.adapter,name,blob,async s=>{await grant('file.save',s);},signal),signal);
   const url=URL.createObjectURL(blob),link=document.createElement('a');
   link.href=url;link.download=name;link.rel='noopener';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
   return {saved:true};
  },
  'chat.compose':async(params,signal)=>{
   const p=object(params),value=text(p.text,32000);await grant('chat.compose',signal);
   await confirm({kind:'compose',detail:value},signal);
   await call('/check',body({capability:'chat.compose'}),signal);
   if(signal.aborted)throw new Error('cancelled');await actions.compose!(value,signal);
   return {composed:true};
  },
  'navigation.open':async(params,signal)=>{
   const p=object(params),key=text(p.applicationKey,256),path=p.path===undefined?'':text(p.path,1000);
   await grant('navigation.open',signal);await confirm({kind:'navigate',detail:key+path},signal);
   await call('/check',body({capability:'navigation.open'}),signal);
   if(signal.aborted)throw new Error('cancelled');await actions.navigate!(key,path,signal);
   return {opened:true};
  },
 }};
}
