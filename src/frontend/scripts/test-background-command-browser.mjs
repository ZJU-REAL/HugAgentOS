import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('../../.scratch/bash-background/no-command-strip-' + Date.now());
await mkdir(output, { recursive: true });
const subscriptionFailure = process.argv.includes('--subscription-failure');
const fixture = `
import React, {useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {useCommandCompletions} from './src/hooks/useCommandCompletions';
import {createStreamRecovery} from './src/hooks/streamRecovery';
import {createStreamFollowing} from './src/hooks/streamFollowing';
import {useChatStore,useAuthStore} from './src/stores';
import {MessageBubble} from './src/components/chat/MessageBubble';
const enc=new TextEncoder();
const frame=value=>'data: '+JSON.stringify(value)+'\\n\\n';
let controller;
const subscriptionFailure=${JSON.stringify(subscriptionFailure)};
window.subscriptionAttempts=0;
window.live=true;window.finished=false;window.notice='';window.subscriptions=0;window.probes=0;window.historyFailures=0;window.notificationConnections=0;window.notificationCloses=0;
Object.defineProperty(navigator,'locks',{value:undefined,configurable:true});
useAuthStore.setState({authUser:{user_id:'owner'}});
useChatStore.setState({currentUserId:'owner',currentChatId:'c',backendSessionIds:new Set(['c']),
 loadedMsgIds:new Set(['c']),activeRuns:{},sendingChatIds:new Set(),sending:false,
 store:{order:['c'],chats:{c:{id:'c',title:'Background',createdAt:1,updatedAt:1,
 messages:[{uid:'q',messageId:'q',role:'user',content:'后台完成后告诉我结果',ts:1}]}}}});
window.fetch=async (input,options={})=>{
 const url=String(input);
 const data=value=>new Response(JSON.stringify({code:0,data:value}),{headers:{'Content-Type':'application/json'}});
 if(url.includes('/v1/commands/events')){
  window.notificationConnections++;
  return new Response(new ReadableStream({start(c){
   window.notificationController=c;
   options.signal?.addEventListener('abort',()=>{window.notificationCloses++;c.error(new DOMException('Aborted','AbortError'));},{once:true});
   c.enqueue(enc.encode(frame({type:'command_runs',run_ids:window.notice?[window.notice]:[]})));
  }}),{headers:{'Content-Type':'text/event-stream'}});
 }
 if(url.includes('/v1/commands'))throw new Error('Task polling must not occur');
 if(url.includes('/active-run')){await new Promise(resolve=>setTimeout(resolve,100));return data(window.live?{run_id:'wake-1',message_id:'a',status:'running',kind:'chat',enable_thinking:false}:null);}
 if(url.includes('/messages')){if(window.notice==='wake-fast'&&window.historyFailures===0){window.historyFailures++;return new Response('temporary failure',{status:503});}return data({items:[
  {message_id:'q',role:'user',content:'后台完成后告诉我结果',created_at:'2026-10-19T00:00:00Z'},
  {message_id:'a',role:'assistant',content:window.finished?'唤醒前半后半':'',created_at:'2026-10-19T00:00:01Z'},
  ...(['wake-fast','wake-offline'].includes(window.notice)?[{message_id:'fast',role:'assistant',content:'快速完成结果',created_at:'2026-10-19T00:00:02Z'}]:[]),
  ...(window.notice==='wake-offline'?[{message_id:'offline',role:'assistant',content:'断线期间的完成结果',created_at:'2026-10-19T00:00:03Z'}]:[])
 ],pagination:{has_next:false}});}
 if(url.includes('/subscription')){
  window.subscriptionAttempts++;
  if(subscriptionFailure&&window.subscriptionAttempts===1)return new Response('unavailable',{status:503});
  window.subscriptions++;window.subscribedAt=performance.now();
  return new Response(new ReadableStream({start(c){controller=c;
   c.enqueue(enc.encode(frame({type:'run_started',run_id:'wake-1',message_id:'a',event_offset:1})));
   c.enqueue(enc.encode(frame({type:'content',delta:'唤醒前半',event_offset:2})));
  }}),{headers:{'Content-Type':'text/event-stream'}});
 }
 if(url.includes('/steers'))return data([]);
 throw new Error('Unexpected request '+url);
};
window.finish=()=>{window.live=false;window.finished=true;
 controller.enqueue(enc.encode(frame({type:'content',delta:'后半',event_offset:3})+
 frame({type:'meta',message_id:'a',event_offset:4})+'data: [DONE]\\n\\n'));controller.close();};
window.notify=id=>{window.notice=id;window.notificationController.enqueue(enc.encode(frame({type:'command_runs',run_ids:[id]})));};
window.busy=value=>{if(value)useChatStore.getState().addSendingChatId('c');else useChatStore.getState().removeSendingChatId('c');};
window.drop=()=>{window.notice='wake-offline';window.notificationController.close();};
window.logout=()=>useAuthStore.setState({authUser:null});
function App(){
 const abortControllersRef=useRef(new Map()),interruptedNoticeShownRef=useRef(new Set()),reconcilingRef=useRef(new Set()),serverRunDiscoveryRef=useRef(new Set());
 window.watchdogBusy=()=>reconcilingRef.current.size;
 const following=createStreamFollowing({generateSummary:async()=>{},generateClassification:async()=>{},syncManualTitleToBackend:()=>{}});
 const recovery=createStreamRecovery({abortControllersRef,interruptedNoticeShownRef,reconcilingRef,serverRunDiscoveryRef,
  processRegenerateStream:following.processRegenerateStream,reconcileDurableSteerQueue:async()=>{},settleQueuedMessageAfterRun:()=>{}});
 window.attach=()=>recovery.attachServerStartedRun('c');
 window.resume=()=>recovery.resumeRunIfAny('c');
 useCommandCompletions(recovery.attachServerStartedRun);
 const messages=useChatStore(state=>state.store.chats.c.messages);
 return <main>{messages.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId="c"
 send={()=>{}} exportChatRecord={async()=>{}}/>)}</main>;
}
window.state=()=>useChatStore.getState();
createRoot(document.getElementById('root')).render(<App/>);
`;
await build({ stdin: { contents: fixture, loader: 'tsx', resolveDir: process.cwd() },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}', 'process.env.NODE_ENV': '"production"' },
  loader: { '.svg': 'dataurl', '.png': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.md': 'text' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') },
  external: ['/loader.gif', '/loader-done.png'] });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://fixture.test/**', async route => {
    if (route.request().url().endsWith('/fixture.js')) {
      await route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js'), 'utf8') });
    } else {
      await route.fulfill({ contentType: 'text/html',
        body: '<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
    }
  });
  await page.goto('http://fixture.test');
  await page.waitForFunction(() => window.notificationConnections === 1);
  assert.equal(await page.evaluate(() => window.subscriptions), 0);
  await page.evaluate(() => { window.notifiedAt=performance.now(); window.notify('wake-1'); });
  await page.getByText('唤醒前半', { exact: true }).waitFor();
  assert.equal(await page.getByText('唤醒前半后半', { exact: true }).count(), 0);
  assert.equal(await page.evaluate(() => window.watchdogBusy()), 0);
  assert.ok(await page.evaluate(() => window.subscribedAt - window.notifiedAt < (window.subscriptionAttempts > 1 ? 2500 : 1000)));
  await page.evaluate(() => { void window.attach(); void window.resume(); window.notify('wake-1'); });
  assert.equal(await page.getByRole('region', { name: '后台命令' }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '查看输出' }).count(), 0);
  await page.evaluate(() => window.finish());
  await page.getByText('唤醒前半后半', { exact: true }).waitFor();
  await page.waitForFunction(() => !window.state().sendingChatIds.has('c'));
  await page.evaluate(() => { window.busy(true); window.notify('wake-fast'); });
  await page.waitForTimeout(100);
  assert.equal(await page.getByText('快速完成结果', { exact: true }).count(), 0);
  await page.evaluate(() => window.busy(false));
  await page.getByText('快速完成结果', { exact: true }).waitFor({ timeout: 20000 });
  assert.equal(await page.evaluate(() => window.subscriptions), 1);
  assert.equal(await page.evaluate(() => window.historyFailures), 1);
  assert.equal(await page.evaluate(() => window.subscriptionAttempts), subscriptionFailure ? 2 : 1);
  await page.evaluate(() => window.drop());
  await page.getByText('断线期间的完成结果', { exact: true }).waitFor({ timeout: 10000 });
  assert.ok(await page.evaluate(() => window.notificationConnections >= 3));
  await page.evaluate(() => window.logout());
  await page.waitForFunction(() => window.notificationCloses >= 2);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: resolve(output, 'result.png') });
  await writeFile(resolve(output, 'result.json'), JSON.stringify({ result: 'passed',
    checks: ['no command strip', 'push attaches the reply stream within 1 second without polling', 'partial reply before stream completion', 'single SSE subscription',
      'fast completion retries after failed history load', 'concurrent discovery without Web Locks', 'busy conversation drains on state change', 'disconnect snapshot replay', 'logout closes notification stream', 'failed output subscription remains pending until retry succeeds'] }, null, 2));
  console.log(JSON.stringify({ result: 'passed', output }));
} finally { await browser.close(); }
