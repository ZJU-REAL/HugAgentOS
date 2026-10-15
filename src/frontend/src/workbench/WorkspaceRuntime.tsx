import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {useLocation,useNavigationType} from 'react-router';
import {getLang,t} from '../i18n';
import {useUIStore} from '../stores/uiStore';
import {systemPrefersDark} from '../theme';
import {navigateTo,pathForPanel} from '../routing/navigation';
import {useApplicationDirectory,type CatalogItem} from './catalog';
import {workbenchRequest} from './sourceRequest';
import {applicationRoute,openWorkspaceApplication,ownApplicationPath} from './workspaceNavigation';
import {PluginWorkspaceFrame,type WorkspaceLaunch} from './PluginWorkspaceFrame';
import {WorkspaceInstances,type InstanceActions} from './lifecycle/WorkspaceInstances';
import type {ApplicationInstance,InstanceIdentity} from './lifecycle/instancePool';
import type {StateExtension} from './stateExtension';
import type {WorkspaceExtension,WorkspaceRoute} from './workspaceBridge';
import {identityKey,createSessionRegistration} from './invalidation';
import {createWorkspaceResourceExtension} from './resourceApi';
import {ActivityWorkspace} from './ActivityWorkspace';
import {applicationText} from './externalApi';
import {SourceStatus} from './SourceStatus';
import {sourceLabel} from './sourceIdentity';
import {combineExtensions} from './combineExtensions';
import {WorkspaceRouteHeader} from './WorkspaceRouteHeader';
import {selectedWorkspaceView} from './workspaceView';

interface RuntimeDocument {launch:WorkspaceLaunch;item:CatalogItem}
/** Mounted beside the keyed business panel, across all builtin and application routes. */
export function WorkspaceRuntime(){
 const location=useLocation();const navigationType=useNavigationType();const catalog=useApplicationDirectory();
 const route=applicationRoute(location.pathname,location.search,location.hash);
 const item=catalog.items.find(value=>value.applicationKey===route?.applicationKey);
 const ownerScope=identityKey();
 const routes=useRef(new Map<string,WorkspaceRoute>());
 const routeKey=route?.applicationKey;
 const currentRoute=useMemo<WorkspaceRoute>(()=>({path:route?.path??'/',requestId:typeof location.state?.workspaceRequestId==='string'?location.state.workspaceRequestId:'host:'+location.key}),[route?.path,location.key,location.state]);
 useEffect(()=>{if(routeKey)routes.current.set(routeKey,currentRoute);},[routeKey,currentRoute]);
 const views=item?.declaration.views??[];
 const view=selectedWorkspaceView(views,route?.path??'/');
 const binding=item?.binding;
 const active=item?.availability.available&&item.declaration.target.kind==='plugin'&&binding&&catalog.userId?{
  identity:{userId:catalog.userId,applicationKey:item.applicationKey,installId:binding.installId,packageRevision:binding.packageRevision,executionScope:binding.executionScope,origin:binding.origin.id,workspace:'main'},
  keepAlive:item.declaration.lifecycle?.keepAlive,stateSchemaVersion:item.declaration.lifecycle?.stateSchemaVersion,moduleId:view?.moduleId||item.declaration.target.moduleId,
 }:null;
 const register=useMemo(()=>createSessionRegistration(ownerScope),[ownerScope]);
 const load=useCallback(async(identity:InstanceIdentity,signal:AbortSignal,instanceId:string,moduleId?:string):Promise<RuntimeDocument>=>{
  const item=catalog.items.find(value=>value.applicationKey===identity.applicationKey);
  if(!item?.binding)throw new Error('application_unavailable');
  const response=await workbenchRequest<{data:WorkspaceLaunch}>('/v1/workbench/applications/'+encodeURIComponent(identity.applicationKey)+'/launch?moduleId='+encodeURIComponent(moduleId??'')+'&instanceId='+encodeURIComponent(instanceId),{method:'POST',body:'{}',signal},item.binding.executionScope);
  const launch=response.data;
  if(launch.applicationKey!==identity.applicationKey||launch.context.applicationKey!==identity.applicationKey||launch.binding.installId!==identity.installId||launch.binding.packageRevision!==identity.packageRevision||launch.binding.origin.id!==identity.origin||launch.binding.executionScope!==identity.executionScope||launch.module.id!==moduleId||launch.module.surface!=='workspace')throw new Error('installation_binding_invalid');
  return {launch:{...launch,context:{...launch.context,instanceId}},item};
 },[catalog.items]);
 return <><SourceStatus failures={catalog.sourceErrors}/><WorkspaceInstances active={active} ownerScope={ownerScope} load={load}
  validate={entry=>catalog.loading||!!catalog.items.find(value=>value.applicationKey===entry.identity.applicationKey&&value.availability.available&&value.binding?.installId===entry.identity.installId&&value.binding.origin.id===entry.identity.origin)}
  register={register} onDispose={()=>{}} onBlocked={current=>{
   if(current)openWorkspaceApplication(current.applicationKey,routes.current.get(current.applicationKey)?.path??'/');
  }} render={(entry,state,actions)=><RuntimeFrame entry={entry} state={state} actions={actions} ownerScope={ownerScope}
   active={entry.identity.applicationKey===route?.applicationKey&&entry.status!=='suspended'}
   hostNavigation={navigationType==='POP'||typeof location.state?.workspaceRequestId!=='string'} route={entry.identity.applicationKey===route?.applicationKey?currentRoute:routes.current.get(entry.identity.applicationKey)??{path:'/',requestId:'host:initial'}} />}/></>;
}
function RuntimeFrame({entry,state,actions,route,ownerScope,active,hostNavigation}:{active:boolean;hostNavigation:boolean;entry:ApplicationInstance<RuntimeDocument>;state:StateExtension;actions:InstanceActions;route:WorkspaceRoute;ownerScope:string}){
 const [resourceId,setResourceId]=useState<string|undefined>();
 const {launch,item}=entry.value!;const themeMode=useUIStore(value=>value.themeMode);
 const resources=useMemo(()=>createWorkspaceResourceExtension({applicationKey:launch.applicationKey,instanceId:entry.instanceId,installId:launch.binding.installId,revision:launch.revision,executionScope:launch.binding.executionScope,origin:launch.binding.origin,moduleId:launch.module.id,signal:entry.signal,onResourceChange:setResourceId}),[launch,entry]);
 useEffect(()=>()=>{queueMicrotask(()=>void resources.dispose());},[resources]);
 useEffect(()=>{void resources.setActive(entry.status!=='suspended');},[resources,entry.status]);
 const activity=useMemo(()=>({applicationKey:launch.applicationKey,instanceId:entry.instanceId,moduleId:launch.module.id,binding:{...launch.binding,revision:launch.revision}}),[launch,entry]);
 const seen=useRef(new Set<string>());
 const navigation=useMemo<WorkspaceExtension>(()=>({capabilities:['workspace.navigate'],handlers:{'workspace.navigate':(params,signal)=>{
  if(signal.aborted||entry.signal.aborted)throw new Error('cancelled');
  if(entry.status==='suspended')throw new Error('instance_inactive');
  if(!params||typeof params!=='object'||Array.isArray(params))throw new Error('invalid_navigation');
  const value=params as {path?:unknown;requestId?:unknown;replace?:unknown};const path=ownApplicationPath(value.path);
  if(typeof value.requestId!=='string'||!/^[a-zA-Z0-9:_-]{1,64}$/.test(value.requestId)||(value.replace!==undefined&&typeof value.replace!=='boolean'))throw new Error('invalid_navigation');
  if(seen.current.has(value.requestId)||value.requestId===route.requestId)return route;
  seen.current.add(value.requestId);if(seen.current.size>128)seen.current.delete(seen.current.values().next().value!);
  openWorkspaceApplication(item.applicationKey,path,value.replace===true,value.requestId);return {path,requestId:value.requestId};
 }}}),[entry,item.applicationKey,route]);
 const title=applicationText(item.declaration.title)+' · '+t(sourceLabel(launch.binding.origin));
 return <section aria-label={title} style={{height:'100%'}}>
  <button type="button" onClick={()=>void actions.close().then(ok=>{if(ok)navigateTo(pathForPanel('app_center'));})}>{t('关闭')}</button>
  <WorkspaceRouteHeader active={active} hostNavigation={hostNavigation} title={title} path={route.path}
   views={(item.declaration.views??[]).map(view=>({...view,title:applicationText(view.title)}))}
   label={t('应用二级导航')} onNavigate={path=>openWorkspaceApplication(item.applicationKey,path)}/>
  <ActivityWorkspace context={activity} declared={[...(item.declaration.permissions??[])]} resourceId={resourceId} render={activityExtension=><PluginWorkspaceFrame
   launch={launch} instanceId={entry.instanceId} signal={entry.signal} ownerScope={ownerScope} route={route} onReady={actions.ready} onLoading={actions.loading} onFailed={actions.failed} onRetry={actions.retry}
   theme={themeMode==='dark'||(themeMode==='system'&&systemPrefersDark())?'dark':'light'} locale={getLang()}
   extension={combineExtensions(state,navigation,...(launch.module.resource?[resources.extension]:[]),activityExtension)} />} />
 </section>;
}
