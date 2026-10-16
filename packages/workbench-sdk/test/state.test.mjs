import assert from 'node:assert/strict';
import {applicationState} from '../dist/index.js';
const calls=[];
const controller=new AbortController();
const connection={request:async(method,params,signal)=>{
 calls.push({method,params,signal});
 return method==='lifecycle.setDirty'?{dirty:params.dirty}:{revision:4,schemaVersion:'2',payload:null,resetRequired:true};
}};
const state=applicationState(connection);
assert.equal((await state.load(controller.signal)).resetRequired,true);
await state.save({revision:4,schemaVersion:'2',payload:{filter:'open'},reset:true},controller.signal);
await state.setDirty(false,controller.signal);
assert.deepEqual(calls.map(c=>[c.method,c.params]),[
 ['state.load',{}],
 ['state.save',{revision:4,schemaVersion:'2',payload:{filter:'open'},reset:true}],
 ['lifecycle.setDirty',{dirty:false}],
]);
assert.ok(calls.every(c=>c.signal===controller.signal));
console.log('SDK state reset/CAS/cancellation wire contract passed');
