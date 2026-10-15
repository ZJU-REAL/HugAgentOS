import {randomUUID} from '../uuid';
/** Bounded instances. Only user activation creates work; catalog entries never do. */
export interface InstanceIdentity {
 userId:string; applicationKey:string; installId:string; packageRevision:string;
 executionScope:string; origin:string; workspace:string;
}
export type InstanceStatus='unloaded'|'loading'|'ready'|'suspended'|'failed'|'closed';
export interface ApplicationInstance<T> {
 id:string; instanceId:string; identity:InstanceIdentity; status:InstanceStatus;
 dirty:boolean; lastUsed:number; value?:T; error?:unknown; signal:AbortSignal;
}
export function instanceKey(value:InstanceIdentity) {
 return JSON.stringify([value.userId,value.applicationKey,value.installId,value.packageRevision,value.executionScope,value.origin,value.workspace]);
}
type Entry<T>=ApplicationInstance<T>&{controller:AbortController;load:(signal:AbortSignal,instanceId:string)=>Promise<T>;keepAlive:boolean;prior:InstanceStatus;documentController?:AbortController;contentKey:string};
interface PoolOptions<T>{
 confirmDiscard:(instance:ApplicationInstance<T>)=>boolean|Promise<boolean>;
 dispose:(instance:ApplicationInstance<T>)=>void;inactiveLimit?:number;
}
export class InstancePool<T> {
 private readonly options:PoolOptions<T>;
 private entries=new Map<string,Entry<T>>();
 private listeners=new Set<()=>void>();
 private current:string|undefined;
 private clock=0;
 private epoch=0;
 private queue:Promise<unknown>=Promise.resolve();
 constructor(options:PoolOptions<T>){this.options=options;}
 snapshot=():readonly ApplicationInstance<T>[]=>[...this.entries.values()];
 subscribe=(listener:()=>void)=>{this.listeners.add(listener);return ()=>{this.listeners.delete(listener);};};
 get currentId(){return this.current;}
 private emit(){this.listeners.forEach(fn=>fn());}
 private serialize<R>(operation:()=>Promise<R>):Promise<R>{
  const result=this.queue.then(operation,operation);
  this.queue=result.catch(()=>undefined);
  return result;
 }
 activate(identity:InstanceIdentity,load:Entry<T>['load'],keepAlive=true,contentKey=''):Promise<boolean>{
  const epoch=this.epoch;
  return this.serialize(()=>epoch===this.epoch?this.open(identity,load,keepAlive,contentKey):Promise.resolve(false));
 }
 private async discard(entry:Entry<T>){
  if(!entry.dirty)return true;
  if(!await this.options.confirmDiscard(entry))return false;
  // Re-check membership after asynchronous native/host confirmation.
  return this.entries.get(entry.id)===entry;
 }
 private async open(identity:InstanceIdentity,load:Entry<T>['load'],keepAlive:boolean,contentKey=''){
  const epoch=this.epoch;
  const id=instanceKey(identity);
  const previous=this.current ? this.entries.get(this.current) : undefined;
  if(previous?.id===id && previous.contentKey===contentKey)return true;
  if(previous?.dirty){
   if(!await this.discard(previous))return false;
   this.remove(previous);
  }
  if(previous && this.entries.has(previous.id) && !previous.keepAlive)this.remove(previous);
  let entry=this.entries.get(id);
  if(!entry){
   const maximum=1+Math.max(0,Math.min(this.options.inactiveLimit??2,2));
   while(this.entries.size>=maximum){
    const victim=[...this.entries.values()].sort((a,b)=>a.lastUsed-b.lastUsed)[0];
    if(!await this.discard(victim))return false;
    this.remove(victim);
   }
  }
  if(entry && entry.contentKey!==contentKey && entry.dirty){
   if(!await this.discard(entry))return false;
   entry.dirty=false;
  }
  if(epoch!==this.epoch)return false;
  const active=this.current ? this.entries.get(this.current) : undefined;
  if(active){active.prior=active.status;active.status='suspended';}
  if(entry){
   this.current=id;entry.status=entry.prior==='loading'?'loading':entry.prior==='failed'?'failed':'ready';
   entry.lastUsed=++this.clock;
   if(entry.contentKey!==contentKey){entry.contentKey=contentKey;this.loadDocument(entry,load);}
   this.emit();return true;
  }
  const controller=new AbortController();
  entry={id,instanceId:randomUUID(),identity:{...identity},status:'loading',prior:'loading',
   dirty:false,lastUsed:++this.clock,controller,signal:controller.signal,load,keepAlive,contentKey};
  this.entries.set(id,entry);this.current=id;this.emit();
  this.loadDocument(entry,load);
  return true;
 }
 private loadDocument(entry:Entry<T>,load:Entry<T>['load']){
  entry.documentController?.abort();
  const documentController=new AbortController();
  const abort=()=>documentController.abort();
  entry.controller.signal.addEventListener('abort',abort,{once:true});
  entry.documentController=documentController;entry.load=load;
  entry.value=undefined;entry.error=undefined;entry.prior='loading';
  if(entry.status!=='suspended')entry.status='loading';
  this.emit();
  // A document load cannot hold up navigation to other applications.
  void Promise.resolve().then(()=>load(documentController.signal,entry.instanceId)).then(value=>{
   if(documentController.signal.aborted || this.entries.get(entry.id)!==entry)return;
   entry.value=value;this.emit();
  },error=>{
   if(documentController.signal.aborted || this.entries.get(entry.id)!==entry)return;
   entry.error=error;entry.prior='failed';
   if(entry.status!=='suspended')entry.status='failed';
   this.emit();
  }).finally(()=>entry.controller.signal.removeEventListener('abort',abort));
 }
 replaceCurrent(load:Entry<T>['load'],contentKey=''):Promise<boolean>{
  return this.serialize(async()=>{
   const entry=this.current?this.entries.get(this.current):undefined;
   if(!entry || !await this.discard(entry))return false;
   if(this.entries.get(entry.id)!==entry)return false;
   entry.dirty=false;entry.contentKey=contentKey;this.loadDocument(entry,load);
   return true;
  });
 }
 markLoading(id:string){
  const entry=this.entries.get(id);if(!entry || entry.error!==undefined)return;
  entry.prior='loading';if(entry.status!=='suspended')entry.status='loading';this.emit();
 }
 markReady(id:string){
  const entry=this.entries.get(id);if(!entry)return;
  entry.prior='ready';if(entry.status!=='suspended')entry.status='ready';this.emit();
 }
 markFailed(id:string,error:unknown){
  const entry=this.entries.get(id);if(!entry)return;
  entry.error=error;entry.prior='failed';if(entry.status!=='suspended')entry.status='failed';this.emit();
 }
 setDirty(id:string,dirty:boolean){
  const entry=this.entries.get(id);if(!entry)throw new Error('session_closed');
  entry.dirty=dirty;this.emit();
 }
 close(id:string):Promise<boolean>{
  return this.serialize(async()=>{
   const entry=this.entries.get(id);if(!entry)return true;
   if(!await this.discard(entry))return false;
   this.remove(entry);this.emit();return true;
  });
 }
 retryCurrent(load?:Entry<T>['load']):Promise<boolean>{
  return this.serialize(async()=>{
   const entry=this.current ? this.entries.get(this.current) : undefined;
   if(!entry)return false;
   if(!await this.discard(entry))return false;
   this.remove(entry);
   return this.open(entry.identity,load??entry.load,entry.keepAlive,entry.contentKey);
  });
 }
 private remove(entry:Entry<T>){
  if(this.entries.get(entry.id)!==entry)return;
  this.entries.delete(entry.id);if(this.current===entry.id)this.current=undefined;
  entry.status='closed';entry.controller.abort();entry.documentController?.abort();
  // Teardown cannot be vetoed by dirty. The host owns ports and resource leases.
  try{this.options.dispose(entry);}catch{
   console.error('workbench_instance_cleanup_failed',entry.instanceId);
  }
 }
 revoke(predicate:(entry:ApplicationInstance<T>)=>boolean){
  let changed=false;
  for(const entry of [...this.entries.values()])if(predicate(entry)){this.remove(entry);changed=true;}
  if(changed)this.emit();
 }
 deactivate():Promise<boolean>{
  const epoch=this.epoch;
  return this.serialize(async()=>{
   if(epoch!==this.epoch)return false;
   const active=this.current?this.entries.get(this.current):undefined;
   if(active?.dirty){
    if(!await this.discard(active))return false;
    this.remove(active);
   }
   if(active && this.entries.has(active.id)){
    if(!active.keepAlive)this.remove(active);
    else{active.prior=active.status;active.status='suspended';}
   }
   this.current=undefined;
   const maximum=Math.max(0,Math.min(this.options.inactiveLimit??2,2));
   while(this.entries.size>maximum){
    const victim=[...this.entries.values()].sort((a,b)=>a.lastUsed-b.lastUsed)[0];
    if(!await this.discard(victim))return false;
    this.remove(victim);
   }
   this.emit();return true;
  });
 }
 clear(){this.epoch++;this.revoke(()=>true);}
}
