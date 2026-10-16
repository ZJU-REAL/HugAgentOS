import assert from 'node:assert/strict';
import {createResourceExtension,type ResourceRequest} from '../src/workbench/resourceExtension';
import {randomUUID} from '../src/workbench/uuid';
const ids:string[]=[];
const request:ResourceRequest=async <T>(path:string,init:RequestInit)=>{
 if(init.method==='DELETE')ids.push(path.split('/').at(-1)!);
 return {resource:{resource_id:'opened-worker',status:'active',expires_at:90}} as T;
};
const binding={applicationKey:'plugin:cloud:'+ 'a'.repeat(32)+':'+ 'b'.repeat(64)+':20261014T000000.000000Z:app',
 instanceId:randomUUID(),installId:'exact-install',revision:'r1',executionScope:'cloud' as const,
 origin:{kind:'cloud' as const,id:'a'.repeat(32)},moduleId:'workspace'};
assert.match(binding.instanceId,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
const signal=new AbortController().signal;
const foreground=createResourceExtension({...binding,backgroundResources:async()=>[]},request);
await foreground.extension.handlers['resource.open']({},signal);
await foreground.setActive(false);
assert.deepEqual(ids,['opened-worker'],'suspension must stop a foreground worker');
await assert.rejects(()=>foreground.extension.handlers['resource.command']({resourceId:'opened-worker',id:'command',action:'ping'},signal),/instance_closed/);
await foreground.dispose();
ids.length=0;
const approved=createResourceExtension({...binding,backgroundResources:async()=>['opened-worker']},request);
await approved.extension.handlers['resource.open']({},signal);
await approved.setActive(false);
assert.deepEqual(ids,[],'only server-projected background resource may survive suspension');
await assert.rejects(()=>approved.extension.handlers['resource.lease']({resourceId:'opened-worker'},signal),/instance_closed/);
await approved.dispose();assert.deepEqual(ids,['opened-worker'],'close still stops actual approved background resource');
let resolveOpen!:(value:unknown)=>void;
const racing:ResourceRequest=async <T>(path:string,init:RequestInit)=>{
 if(init.method==='POST')return await new Promise(resolve=>{resolveOpen=resolve;}) as T;
 ids.push(path.split('/').at(-1)!);return {} as T;
};
ids.length=0;
const late=createResourceExtension({...binding,backgroundResources:async()=>[]},racing);
const opening=late.extension.handlers['resource.open']({},signal);
await late.setActive(false);
resolveOpen({resource:{resource_id:'late-worker',status:'active',expires_at:90}});
await assert.rejects(()=>opening,/instance_closed/);
assert.deepEqual(ids,['late-worker'],'late response cannot retain an inactive worker');
await late.dispose();
console.log('Retained runtime resource boundary: foreground stop, independent background projection, inactive command/lease denial, late-create cleanup, cryptographic UUID passed');
