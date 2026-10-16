import {DiagnosticFailure} from '../DiagnosticFailure';
import {workspaceFailureCode} from '../workspaceFailureCode';
import {Component,useEffect,useMemo,type ReactNode} from 'react';
import {t} from '../../i18n';
import {createStateExtension,type StateExtension} from '../stateExtension';
import {InstancePool,instanceKey,type ApplicationInstance,type InstanceIdentity} from './instancePool';

export interface ActiveApplication {
 identity:InstanceIdentity; keepAlive?:boolean; stateSchemaVersion?:string; moduleId?:string;
}
export interface InstanceActions {loading?:()=>void;ready:()=>void;failed:(error:unknown)=>void;retry:()=>void;close:()=>Promise<boolean>}
interface Props<T> {
 /** This host stays mounted across route changes. Null selects a builtin page. */
 active:ActiveApplication|null;
 ownerScope:string;
 load:(identity:InstanceIdentity,signal:AbortSignal,instanceId:string,moduleId?:string)=>Promise<T>;
 render:(instance:ApplicationInstance<T>,extension:StateExtension,actions:InstanceActions)=>ReactNode;
 onDispose:(instance:ApplicationInstance<T>)=>void;
 onBlocked:(current:InstanceIdentity|undefined)=>void;
 confirmDiscard?:(instance:ApplicationInstance<T>)=>boolean|Promise<boolean>;
 /** Loading is not revocation. Validate only against authoritative catalog evidence. */
 validate:(instance:ApplicationInstance<T>)=>boolean;
 register?:(instance:ApplicationInstance<T>,revoke:()=>void)=>()=>void;
}
export function WorkspaceInstances<T>(props:Props<T>){
 return <OwnedWorkspaceInstances key={props.ownerScope} {...props}/>;
}
/** The pool has a lifetime independent of the active route and React render. */
class OwnedWorkspaceInstances<T> extends Component<Props<T>,{entries:readonly ApplicationInstance<T>[]}>{
 state={entries:[] as readonly ApplicationInstance<T>[]};
 private schemas=new Map<string,string|undefined>();
 private mounted=false;
 private unsubscribe:(()=>void)|undefined;
 private pool=new InstancePool<T>({
  confirmDiscard:entry=>this.props.confirmDiscard?.(entry)??window.confirm(t('此页面有未保存内容。是否放弃更改并离开？')),
  dispose:entry=>this.props.onDispose(entry),
 });
 private beforeUnload=(event:BeforeUnloadEvent)=>{
  if(this.pool.snapshot().some(entry=>entry.dirty)){event.preventDefault();event.returnValue='';}
 };
 componentDidMount(){
  this.mounted=true;
  this.unsubscribe=this.pool.subscribe(()=>this.setState({entries:this.pool.snapshot()}));
  window.addEventListener('beforeunload',this.beforeUnload);
  this.activate();
 }
 componentDidUpdate(previous:Props<T>){
  this.pool.revoke(entry=>!this.props.validate(entry));
  const before=previous.active?instanceKey(previous.active.identity):'';
  const after=this.props.active?instanceKey(this.props.active.identity):'';
  if(before!==after)this.activate();
  else if(previous.active?.moduleId!==this.props.active?.moduleId)this.activate(true);
 }
 componentWillUnmount(){
  this.mounted=false;this.unsubscribe?.();this.pool.clear();
  window.removeEventListener('beforeunload',this.beforeUnload);
 }
 private activate(replaceDocument=false){
  const active=this.props.active;
  if(active)this.schemas.set(instanceKey(active.identity),active.stateSchemaVersion);
  const load=this.props.load;
  const loader=active?(signal:AbortSignal,id:string)=>load(active.identity,signal,id,active.moduleId):undefined;
  const operation=active && loader
   ?replaceDocument?this.pool.replaceCurrent(loader,active.moduleId):this.pool.activate(active.identity,loader,active.keepAlive!==false,active.moduleId)
   :this.pool.deactivate();
  void operation.then(ok=>{
   if(!ok && this.mounted){
    const current=this.pool.snapshot().find(entry=>entry.id===this.pool.currentId);
    this.props.onBlocked(current?.identity);
   }
  });
 }
 render(){
  return <>{this.state.entries.map(entry=><RetainedInstance key={entry.instanceId} entry={entry}
   pool={this.pool} schemaVersion={this.schemas.get(entry.id)} render={this.props.render} register={this.props.register}/>)}</>;
 }
}
function RetainedInstance<T>({entry,pool,schemaVersion,render,register}:{
 entry:ApplicationInstance<T>;pool:InstancePool<T>;schemaVersion:string|undefined;render:Props<T>['render'];register:Props<T>['register'];
}){
 useEffect(()=>register?.(entry,()=>pool.revoke(value=>value.id===entry.id)),[entry,pool,register]);
 const extension=useMemo(()=>createStateExtension(entry.identity,schemaVersion,
  dirty=>pool.setDirty(entry.id,dirty),()=>!entry.signal.aborted,entry.instanceId),[entry,pool,schemaVersion]);
 const actions:InstanceActions={
  loading:()=>pool.markLoading(entry.id),ready:()=>pool.markReady(entry.id),failed:error=>pool.markFailed(entry.id,error),
  retry:()=>{if(pool.currentId===entry.id)void pool.retryCurrent();},
  close:()=>pool.close(entry.id),
 };
 return <section hidden={entry.id!==pool.currentId} aria-label={entry.identity.applicationKey}
  data-instance-id={entry.instanceId} data-instance-status={entry.status} style={{height:'100%'}}>
  {entry.error!==undefined && entry.status!=='closed' && <DiagnosticFailure applicationKey={entry.identity.applicationKey} error={entry.error} code={workspaceFailureCode(entry.error)} onRetry={actions.retry}/>}
  {entry.error===undefined && entry.value===undefined && <p role="status">{t('正在加载插件模块…')}</p>}
  {entry.value!==undefined && entry.error===undefined && render(entry,extension,actions)}
 </section>;
}
