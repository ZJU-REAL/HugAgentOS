import {build} from 'esbuild';
import {mkdir,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';

const scenario=process.argv[2] || 'reconnect';
const output=resolve('node_modules/.tmp/stream-reconnect-browser');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {useUIStore} from './src/stores/uiStore';
import {useChatStore} from './src/stores/chatStore';
import {MessageBubble} from './src/components/chat/MessageBubble';
import {markRunCancelledByUser} from './src/hooks/chatStream';
import {createStreamFollowing} from './src/hooks/streamFollowing';
import './src/styles/variables.css';
const scenario=${JSON.stringify(scenario)};
const inline=scenario==='inline-phase';
const ac=new AbortController();
window.cancel=()=>{markRunCancelledByUser('run-1');ac.abort();};
const enc=new TextEncoder();
const frame=e=>'data: '+JSON.stringify(e)+'\\n\\n';
const initial=useChatStore.getState().store;
useChatStore.setState({store:{...initial,chats:{c:{id:'c',title:'Reconnect',messages:[
 {uid:'question',messageId:'question',role:'user',content:'跑二十家企业',ts:1},
 {uid:'answer',messageId:'answer',role:'assistant',content:'',ts:1000,isStreaming:true}
]}},order:['c']},currentChatId:'c'});
function App(){const rows=useChatStore(s=>s.store.chats.c.messages);return <div>{rows.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId="c" send={()=>{}} exportChatRecord={async()=>{}}/>)}</div>}
createRoot(document.getElementById('root')).render(<App/>);
window.followCount=0;window.completed=false;window.live=true;
let finalController;
window.fetch=async input=>{
 const url=String(input);
 if(url.includes('/active-run')) return new Response(JSON.stringify({code:0,data:window.live?{run_id:'run-1',message_id:'answer',status:'running',enable_thinking:true,kind:'chat'}:null}),{headers:{'Content-Type':'application/json'}});
 if(url.includes('/messages')) return new Response(JSON.stringify({code:0,data:{items:[
 {message_id:'question',role:'user',content:'跑二十家企业',created_at:'2026-10-09T00:00:00Z'},
 {message_id:'answer',role:'assistant',content:'前 11 个批次完成。继续核对。',created_at:'2026-10-09T00:00:01Z',
  in_flight:window.live?{run_id:'run-1',event_offset:4}:undefined,
  metadata:{segments:[{type:'text',text:'前 11 个批次完成。继续核对。'}]},
  tool_calls:[{tool_id:'child',tool_name:'call_subagent',status:window.live?'running':'success',result:window.live?undefined:'完成'}]}
 ]}}),{headers:{'Content-Type':'application/json'}});
 if(url.includes('/stream/run-1')){
  window.followCount++;
  window.offsets=(window.offsets||[]).concat(new URL(url,location.href).searchParams.get('from'));
  if(scenario==='forbidden' && window.followCount===2)return new Response('forbidden',{status:403});
  return new Response(new ReadableStream({start(c){
   if(window.followCount===1){c.enqueue(enc.encode(frame({type:'content',delta:'继续核对。',event_offset:4})));c.close();}
   else if(scenario==='forbidden'){c.error(new Error('unused'));}
   else {
    c.enqueue(enc.encode(frame({type:'run_snapshot',event_offset:4,state:{
      run_id:'run-1',message_id:'answer',started_at:1000,event_offset:4,terminal:false,signals:scenario==='controls'?{
      'user_question::request-1':{type:'user_question',request_id:'request-1',questions:[
        {id:'q1',question:'继续吗？',options:[{label:'继续',description:'继续核对'}]}
      ]},
      'plan_update::':{type:'plan_update',title:'核对企业',steps:[{title:'下一批',status:'in_progress'}]}
    }:{},

      blocks:inline?[{kind:'text',content:'</think>前 11 个批次完成。继续核对。'},{kind:'tool',index:0},{kind:'phase'}]:[{kind:'protocol',structured:true},...(scenario==='no-reasoning'?[]:[{kind:'thinking',content:'准备下一批',structured:true}]),
              {kind:scenario==='replacement'?'answer':'text',content:'前 11 个批次完成。继续核对。'},{kind:'tool',index:0}],
      tools:[{id:'child',name:'call_subagent',status:'running',timestamp:1000,input:{}}]
    }})));
    finalController=c;
   }
  }}),{headers:{'Content-Type':'text/event-stream'}});
 }
 return new Response(JSON.stringify({code:0,data:{items:[]}}),{headers:{'Content-Type':'application/json'}});
};
const following=createStreamFollowing({generateClassification:async()=>{},generateSummary:async()=>{},syncManualTitleToBackend:()=>{}});
const response=new Response(new ReadableStream({start(c){
 c.enqueue(enc.encode(frame({type:'run_started',run_id:'run-1',message_id:'answer',started_at:1000,event_offset:1})
 +frame({type:'thinking',structured_reasoning:!inline,delta:scenario==='no-reasoning'?'':'准备下一批',event_offset:2})
 +frame({type:'content',delta:(inline?'</think>':'')+'前 11 个批次完成。',event_offset:3})
 +frame({type:'tool_call',tool_id:'child',tool_name:'call_subagent',started_at:1000,event_offset:3})));
 c.close();
}}),{headers:{'Content-Type':'text/event-stream'}});
following.processRegenerateStream(response,'c',{enableThinking:true,signal:ac.signal,seedFrom:{uid:'answer',messageId:'answer',role:'assistant',content:'',ts:1000}})
 .then(outcome=>{window.completed=true;window.aborted=outcome.aborted;window.settled=outcome.settled}).catch(e=>window.failure=String(e));
window.snapshot=()=>({questions:useUIStore.getState().pendingUserQuestions.c,plan:useChatStore.getState().planProgress.c,messages:useChatStore.getState().store.chats.c.messages,completed:window.completed,follows:window.followCount,offsets:window.offsets,failure:window.failure,aborted:window.aborted,settled:window.settled});
window.finish=()=>{
 window.live=false;
 finalController.enqueue(enc.encode((inline?frame({type:'content',delta:'内部推理</think>下一批完成。',event_offset:5}):'')+frame({type:'tool_result',tool_id:'child',tool_name:'call_subagent',result:'完成',status:'success',event_offset:5})
 +frame({type:'meta',message_id:'answer',duration_ms:60000,event_offset:6})+'data: [DONE]\\n\\n'));
 finalController.close();
};
window.ready=true;
`,resolveDir:process.cwd(),loader:'tsx'},bundle:true,format:'esm',jsx:'automatic',outdir:output,
 define:{'import.meta.env':'{}','process.env.NODE_ENV':'"production"'},
 loader:{'.svg':'dataurl','.png':'dataurl','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.md':'text'},
 external:['/loader.gif','/loader-done.png']});
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try{
 const page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://stream.test/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/')return route.fulfill({contentType:'text/html',body:'<div id="root"></div><script type="module" src="/stdin.js"></script>'});
  const body=await readFile(resolve(output,path.slice(1))).catch(()=>null);
  return route.fulfill({status:body?200:404,body:body??'',contentType:path.endsWith('.js')?'text/javascript':'text/css'});
 });
 await page.goto('http://stream.test/');
 await page.waitForFunction(()=>window.ready);
 await page.waitForTimeout(1500);
 const live=await page.evaluate(()=>window.snapshot());
 assert.equal(live.completed,false,'transport EOF must not finish a running task');
 assert.equal(live.follows,2,'both broken connections should be resumed');
 assert.deepEqual(live.offsets,[null,null], 'native reconnect captures an aligned state');
 const answer=live.messages.filter(m=>m.role==='assistant');
 assert.equal(answer.length,1);
 assert.equal(answer[0].messageId,'answer');
 assert.equal(answer[0].content,'前 11 个批次完成。继续核对。');
 assert.equal(answer[0].isStreaming,true);
 if(scenario==='no-reasoning')assert.equal(answer[0].thinking,undefined,'empty protocol marker must keep answer text out of thinking');
 assert.equal(answer[0].durationMs,undefined);
 assert.equal(answer[0].toolCalls[0].status,'running');
 assert.equal(await page.locator('.jx-msg').count(),2);
 if(scenario==='controls'){
  assert.equal(live.questions.length,1,'pending question must survive snapshot installation');
  assert.equal(live.plan.title,'核对企业');
 }

 if(scenario==='forbidden'){
  assert.match(live.failure,/Run subscription disconnected/);
  assert.equal(live.messages.at(-1).isStreaming,true);
  assert.equal(live.messages.at(-1).toolCalls[0].status,'running');
  assert.deepEqual(errors,[]);
  console.log('Real browser permanent-error checks passed');
 }else if(scenario==='cancel'){
  await page.evaluate(()=>window.cancel());
  await page.waitForFunction(()=>window.completed);
  const cancelled=await page.evaluate(()=>window.snapshot());
  assert.equal(cancelled.aborted,true);
  assert.equal(cancelled.messages.at(-1).isStreaming,false);
  assert.equal(cancelled.messages.filter(m=>m.role==='assistant').length,1);
  assert.deepEqual(errors,[]);
  console.log('Real browser cancellation checks passed');
  process.exitCode=0;
 }else{
 await page.evaluate(()=>window.finish());
 await page.waitForFunction(()=>window.completed);
 const done=await page.evaluate(()=>window.snapshot());
 assert.equal(done.messages.filter(m=>m.role==='assistant').length,1);
 assert.equal(!!done.messages.at(-1).isStreaming,false);
 assert.equal(done.settled,true,'task terminal must permit completion follow-ups');
 if(scenario==='inline-phase'){
  assert.equal(done.messages.at(-1).content,'前 11 个批次完成。继续核对。下一批完成。');
  assert.ok(done.messages.at(-1).thinking.some(t=>t.content.includes('内部推理')));
 }
 if(scenario!=='gap')assert.equal(done.messages.at(-1).durationMs,60000);
 assert.equal(done.messages.at(-1).toolCalls[0].status,'success');
 assert.deepEqual(errors,[]);
 console.log('Real browser '+scenario+' checks passed: one reply, cursor preserved, no false completion');
 }
}finally{await browser.close();}
