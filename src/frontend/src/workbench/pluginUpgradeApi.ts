import {reloadDirectory} from './catalog';
import {workbenchRequest as apiRequest} from './sourceRequest';
import {invalidateWorkbenchDirectory} from './invalidation';
export interface PluginUpgrade {
 installId:string;currentRevision:string;candidateRevision:string|null;
 status:'current'|'pending'|'rejected';addedPermissions:string[];
 stateSchemaChanges:{applicationId:string;previous:string;next:string}[];reason?:string;
}
export function upgradePath(installId:string,admin=false) {
 return '/v1/workbench/'+(admin?'admin/':'')+'plugin-upgrades/'+encodeURIComponent(installId);
}
export async function readPluginUpgrade(installId:string,token?:string,signal?:AbortSignal) {
 const response = await apiRequest<{data:PluginUpgrade}>(upgradePath(installId,!!token),{
  signal,headers:token?{Authorization:'Bearer '+token}:{},
 },installId.startsWith('plugin:local:')?'local':'cloud');
 return response.data;
}
export async function decidePluginUpgrade(value:PluginUpgrade,decision:'approve'|'reject',resetState:boolean,token?:string) {
 const result = await apiRequest<unknown>(upgradePath(value.installId,!!token)+'/decision',{
  method:'POST',headers:token?{Authorization:'Bearer '+token}:{},
  body:JSON.stringify({expectedRevision:value.currentRevision,candidateRevision:value.candidateRevision,
   decision,resetState}),
 },value.installId.startsWith('plugin:local:')?'local':'cloud');
 invalidateWorkbenchDirectory('mutation');
 await reloadDirectory();
 return result;
}
