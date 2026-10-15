import assert from 'node:assert/strict';
import {RevisionLease,type LeaseClock,type LeaseStatus,type LeaseReceipt} from '../src/workbench/revisionLease';
import {ApiResponseError} from '../src/utils/apiError';

class Clock implements LeaseClock {
 time=0; epoch=1_800_000_000_000; next=0;
 tasks=new Map<number,{at:number;action:()=>void}>();
 wall=()=>this.epoch+this.time;
 monotonic=()=>this.time;
 schedule=(action:()=>void,delay:number)=>{
  const id=++this.next;this.tasks.set(id,{at:this.time+delay,action});
  return id as unknown as ReturnType<typeof setTimeout>;
 };
 cancel=(id:ReturnType<typeof setTimeout>)=>{this.tasks.delete(id as unknown as number);};
 async settle(){for(let i=0;i<12;i++)await Promise.resolve();}
 async advance(ms:number){
  const target=this.time+ms;
  while(true){
   const next=[...this.tasks].sort((a,b)=>a[1].at-b[1].at)[0];
   if(!next||next[1].at>target)break;
   this.time=next[1].at;this.tasks.delete(next[0]);next[1].action();await this.settle();
  }
  this.time=target;await this.settle();
 }
}
function fixture(){
 const clock=new Clock(),statuses:LeaseStatus[]=[];
 let closes=0;
 const id='isolated-instance',revision='b'.repeat(64);
 const receipt=():LeaseReceipt=>({instanceId:id,revision,expiresAt:(clock.wall()+90000)/1000});
 let renew: (signal:AbortSignal)=>Promise<LeaseReceipt> = async()=>receipt();
 const requests:string[]=[];
 const lease=new RevisionLease({instanceId:id,revision,clock,
  request:async(method,signal)=>{requests.push(method);return method==='POST'?receipt():renew(signal);},
  close:async()=>{closes++;},changed:status=>statuses.push(status),
 });
 lease.start();
 return {clock,statuses,lease,receipt,requests,get closes(){return closes;},
  renew:(fn:typeof renew)=>{renew=fn;}};
}
{
 const f=fixture();await f.clock.settle();
 assert.equal(f.lease.canInvoke(),true);
 f.renew(async()=>{throw new TypeError('offline');});
 await f.clock.advance(30000);
 assert.equal(f.statuses.at(-1),'recovering');assert.equal(f.lease.canInvoke(),false);
 f.renew(async()=>f.receipt());await f.clock.advance(2000);
 assert.equal(f.statuses.at(-1),'ready');assert.equal(f.lease.canInvoke(),true);
 f.lease.dispose();assert.equal(f.closes,1);assert.equal(f.clock.tasks.size,0);
}
for(const status of [401,403,404,409,410,422]){
 const f=fixture();await f.clock.settle();
 f.renew(async()=>{throw new ApiResponseError('denied',status,{});});
 await f.clock.advance(30000);
 assert.equal(f.statuses.at(-1),'revoked');assert.equal(f.lease.canInvoke(),false);
 const calls=f.requests.length;await f.clock.advance(100000);
 assert.equal(f.requests.length,calls);
}
{
 const f=fixture();await f.clock.settle();
 f.renew(async()=>{throw new ApiResponseError('temporary',503,{});});
 await f.clock.advance(90000);
 assert.equal(f.statuses.at(-1),'expired');assert.equal(f.lease.canInvoke(),false);
 assert.equal(f.clock.tasks.size,0);
}
{
 const f=fixture();await f.clock.settle();
 let finish!:(receipt:LeaseReceipt)=>void;
 f.renew(()=>new Promise(resolve=>{finish=resolve;}));
 await f.clock.advance(40000);
 assert.equal(f.statuses.at(-1),'recovering','hung requests are bounded');
 f.lease.dispose();finish(f.receipt());await f.clock.settle();
 assert.equal(f.statuses.at(-1),'closed','late response cannot revive disposed lease');
}
{
 const f=fixture();await f.clock.settle();
 f.renew(async()=>{throw new TypeError('offline');});
 f.clock.epoch-=3600000;
 await f.clock.advance(90000);
 assert.equal(f.statuses.at(-1),'expired','wall rollback cannot extend a confirmed lease');
}
{
 const f=fixture();await f.clock.settle();
 f.clock.epoch+=120000;
 assert.equal(f.lease.canInvoke(),false,'call-time check rejects before delayed timers run');
 assert.equal(f.statuses.at(-1),'expired');
}
{
 const f=fixture();await f.clock.settle();
 f.renew(async()=>({...f.receipt(),instanceId:'other-instance'}));
 await f.clock.advance(30000);
 assert.equal(f.statuses.at(-1),'revoked','receipt must match exact instance');
}
console.log('Revision lease: recovery, terminal authorization, expiry, request timeout, late replies, clock changes and receipt binding passed');
