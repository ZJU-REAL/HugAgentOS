import assert from 'node:assert/strict';
import {MessageChannel} from 'node:worker_threads';
import {connect,PROTOCOL_VERSION} from '../dist/index.js';
const info={protocolVersion:PROTOCOL_VERSION,theme:'light',locale:'en',context:{applicationKey:'plugin:test:1:app',installId:'i',packageRevision:'1'},capabilities:['data.query','file.pick','file.save','chat.compose','navigation.open']};
async function session(loading=false){
 const window=new EventTarget(); window.document={readyState:loading?'loading':'complete'}; globalThis.window=window;
 const messages=[]; const channel=new MessageChannel(); let hellos=0;
 window.parent={postMessage(hello){hellos++;queueMicrotask(()=>{
 const event=new Event('message'); Object.assign(event,{source:window.parent,data:{type:'workbench:init',clientNonce:hello.clientNonce,protocolVersion:PROTOCOL_VERSION,nonce:'01234567890123456789',info},ports:[channel.port1]});
 window.dispatchEvent(event);
 });}};
 channel.port2.on('message',message=>messages.push(message));
 if(loading)setTimeout(()=>{assert.equal(hellos,0);window.document.readyState='complete';window.dispatchEvent(new Event('load'));},30);
 const connection=await connect();
 const reply=data=>channel.port2.postMessage({type:'result',nonce:'01234567890123456789',...data});
 return {connection,messages,reply,close(){connection.dispose(); channel.port2.close();}};
}
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));
{
 const s=await session();
 await assert.rejects(s.connection.request('data.query',{value:new ArrayBuffer(1)}),/invalid_message/);
 await assert.rejects(s.connection.request('data.query',{value:new Uint8Array(1)}),/invalid_message/);
 await assert.rejects(s.connection.request('data.query',{value:new Blob(['x'])}),/invalid_message/);
 const value={};Object.defineProperty(value,'boom',{enumerable:true,get(){throw Error('getter_executed');}});
 await assert.rejects(s.connection.request('data.query',{value}),/invalid_message/);
 const recursive={};recursive.self=recursive;
 await assert.rejects(s.connection.request('data.query',{recursive}),/invalid_message/);
 s.close();
}
console.log('SDK rejects non-JSON bridge payloads');

{
 const s=await session();
 const pending=s.connection.query('files',{limit:2});
 await tick();
 const request=s.messages.find(m=>m.type==='request');
 assert.equal(request.method,'data.query');
 assert.deepEqual(request.params,{source:'files',params:{limit:2}});
 s.reply({id:request.id,ok:true,data:{items:[]}});
 assert.deepEqual(await pending,{items:[]});
 const controller=new AbortController();
 const aborted=s.connection.composeChat({text:'draft'},controller.signal);
 controller.abort();
 await assert.rejects(aborted,/cancelled/);
 await tick();
 assert.ok(s.messages.some(m=>m.type==='cancel'));
 const active=s.connection.pickFile({});
 await tick();
 const pick=s.messages.findLast(m=>m.type==='request');
 s.reply({id:pick.id,ok:true,data:new Uint8Array(4)});
 await tick();
 s.reply({id:pick.id,ok:true,data:{fileId:'owned'}});
 assert.deepEqual(await active,{fileId:'owned'});
 s.close();
}
console.log('SDK capability requests preserve parameters, cancellation, and inbound bounds');

{
 const s=await session();
 let deep={}; for(let i=0;i<20;i++)deep={child:deep};
 await assert.rejects(s.connection.request('data.query',{deep}),/invalid_message/);
 await assert.rejects(s.connection.request('data.query',{huge:'a'.repeat(262145)}),/message_too_large/);
 await assert.rejects(s.connection.request('data.query',{huge:Array(5000).fill(0)}),/invalid_message/);
 await assert.rejects(s.connection.request('data.query',{bad:Object.create({inherited:true})}),/invalid_message/);
 const pending=s.connection.request('host.info');
 globalThis.window.dispatchEvent(new Event('pagehide'));
 await assert.rejects(pending,/session_closed/);
 await assert.rejects(s.connection.request('host.info'),/session_closed/);
 s.close();
}
console.log('SDK rejects excessive payloads and closes on document exit');

{
 const s=await session();
 for(const [method,invoke,params,result] of [
  ['file.save',()=>s.connection.saveFile({fileId:'owned',name:'report.txt'}),{fileId:'owned',name:'report.txt'},{saved:true}],
  ['navigation.open',()=>s.connection.openApplication({applicationKey:'builtin:chat',path:'/'}),{applicationKey:'builtin:chat',path:'/'},{opened:true}],
  ['chat.compose',()=>s.connection.composeChat({text:'draft only'}),{text:'draft only'},{composed:true}],
 ]) {
  const promise=invoke(); await tick();
  const request=s.messages.findLast(m=>m.type==='request');
  assert.equal(request.method,method); assert.deepEqual(request.params,params);
  s.reply({id:request.id,ok:true,data:result}); assert.deepEqual(await promise,result);
 }
 const controller=new AbortController();
 const previous=s.connection.request('host.info',{},controller.signal); await tick();
 const old=s.messages.findLast(m=>m.type==='request'); controller.abort();
 await assert.rejects(previous,/cancelled/);
 const current=s.connection.request('host.info'); await tick();
 const active=s.messages.findLast(m=>m.type==='request');
 s.reply({id:old.id,ok:true,data:{stale:true}});
 s.reply({id:active.id,ok:true,data:{fresh:true}});
 assert.deepEqual(await current,{fresh:true});
 s.close();
}
{
 const s=await session();
 const requests=Array.from({length:8},()=>s.connection.request('host.info'));
 const outcomes=Promise.allSettled(requests);
 await assert.rejects(s.connection.request('host.info'),/concurrency_limit/);
 s.close(); assert.ok((await outcomes).every(r=>r.status==='rejected' && /session_closed/.test(r.reason.message)));
}
console.log('SDK helper semantics, stale cancellation replies, and concurrency cap passed');

{
 const s=await session(); const abort=new AbortController(); abort.abort();
 await assert.rejects(s.connection.query('files',{},abort.signal),/cancelled/);
 s.close();
}
{
 const s=await session();
 const timedOut=s.connection.query('files');
 await assert.rejects(timedOut,/bridge_timeout/);
 await tick();
 assert.ok(s.messages.some(m=>m.type==='cancel'));
 const pending=s.connection.request('host.info'); const rejected=assert.rejects(pending,/session_closed/);
 s.close(); await rejected;
 await assert.rejects(s.connection.composeChat({text:'late'}),/session_closed/);
}
console.log('SDK pre-abort, timeout cancellation, and disposed requests passed');

{const s=await session(true);s.close();}
console.log('SDK waits for real document completion before first hello');
