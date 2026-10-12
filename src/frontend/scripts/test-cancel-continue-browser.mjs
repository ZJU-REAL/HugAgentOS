// Real HTTP/SSE fixture server is started by test_cancel_continue_e2e.py.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const [baseUrl, scenario] = process.argv.slice(2);
assert.ok(baseUrl && scenario, 'Pass fixture server URL and scenario');
const output = resolve('node_modules/.tmp/cancel-continue-browser', scenario);
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { useChatStore } from './src/stores/chatStore';
import { MessageBubble } from './src/components/chat/MessageBubble';
import { markRunCancelledByUser } from './src/hooks/chatStream';
import { createStreamFollowing } from './src/hooks/streamFollowing';
import { parseHistoryMessage } from './src/hooks/chatHistoryMessage';
import './src/styles/variables.css';
const base=${JSON.stringify(baseUrl)};
const initial=useChatStore.getState().store;
useChatStore.setState({store:{...initial,chats:{'chat-1':{id:'chat-1',title:'Resume',messages:[]}},order:['chat-1']},currentChatId:'chat-1'});
const following=createStreamFollowing({generateClassification:async()=>{},generateSummary:async()=>{},syncManualTitleToBackend:()=>{}});
let controller;
let active;
let userCancelled=false;
window.failure=null;
window.done=false;
const setMessages=messages=>useChatStore.setState(s=>({store:{...s.store,chats:{...s.store.chats,'chat-1':{...s.store.chats['chat-1'],messages}}}}));
async function send(message){
 window.done=false;
 userCancelled=false;
 controller=new AbortController();
 const rows=useChatStore.getState().store.chats['chat-1'].messages;
 const seed={uid:'pending-'+rows.length,role:'assistant',content:'',ts:Date.now(),isStreaming:true};
 setMessages([...rows,{uid:'user-'+rows.length,role:'user',content:message,ts:Date.now()},seed]);
 try{
  const response=await fetch(base+'/v1/agents/responses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:'chat-1',message,stream:true,model_name:'deepseek'}),signal:controller.signal});
  if(!response.ok)throw Error(await response.text());
  active=following.processRegenerateStream(response,'chat-1',{enableThinking:true,signal:controller.signal,seedFrom:seed});
  await active;
 }catch(e){if(!userCancelled && e.name!=='AbortError')window.failure=String(e);}
 window.done=true;
}
async function stop(){
 const result=await fetch(base+'/v1/chats/chat-1/active-run').then(r=>r.json());
 const id=result.data.run_id;
 userCancelled=true;
 markRunCancelledByUser(id);
 const response=await fetch(base+'/v1/chat-runs/'+id+'/cancel',{method:'POST'});
 if(!response.ok)throw Error(await response.text());
 controller.abort();
 if(active)await active.catch(e=>{if(!userCancelled)throw e;});
}
async function reload(){
 const response=await fetch(base+'/v1/chats/chat-1/messages').then(r=>r.json());
 setMessages(response.data.items.map(parseHistoryMessage));
 window.reloaded=true;
}
function App(){const rows=useChatStore(s=>s.store.chats['chat-1'].messages);return <>
 <button id="start" onClick={()=>send('检查服务器')}>开始</button>
 <button id="stop" onClick={()=>stop().catch(e=>window.failure=String(e))}>停止</button>
 <button id="reload" onClick={()=>reload().catch(e=>window.failure=String(e))}>刷新历史</button>
 <button id="continue" onClick={()=>send('继续')}>继续</button>
 {rows.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId="chat-1" send={()=>{}} exportChatRecord={async()=>{}}/>)}
 </>}
createRoot(document.getElementById('root')).render(<App/>);
window.snapshot=()=>useChatStore.getState().store.chats['chat-1'].messages;
`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, format: 'esm', jsx: 'automatic', outdir: output,
  define: { 'import.meta.env': JSON.stringify({ VITE_API_BASE_URL: baseUrl }), 'process.env.NODE_ENV': '"production"' },
  loader: { '.svg': 'dataurl', '.png': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.md': 'text' },
  external: ['/loader.gif', '/loader-done.png'] });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') console.error(message.text()); });
  await page.route(baseUrl + '/fixture/**', async route => {
    const path = new URL(route.request().url()).pathname.slice('/fixture'.length);
    if (path === '/') return route.fulfill({ contentType: 'text/html', body: '<link rel="stylesheet" href="/fixture/stdin.css"><div id="root"></div><script type="module" src="/fixture/stdin.js"></script>' });
    const body = await readFile(resolve(output, path.slice(1))).catch(() => null);
    return route.fulfill({ status: body ? 200 : 404, body: body ?? '', contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css' });
  });
  await page.goto(baseUrl + '/fixture/');
  await page.locator('#start').click();
  await page.waitForFunction(() => window.failure || window.snapshot().some(m => m.thinking?.some(t => t.content.includes('PRIVATE_REASONING'))), null, {timeout: 8000});
  assert.equal(await page.evaluate(() => window.failure), null);
  if (scenario !== 'thinking_only') {
    await page.waitForFunction(() => window.snapshot().some(m => m.content.includes('已检查服务器')));
  }
  await page.locator('#stop').click();
  await page.waitForFunction(() => window.done || window.failure);
  assert.equal(await page.evaluate(() => window.failure), null);
  // Cancellation acknowledges first; writer admission opens only after the
  // worker has finished persisting its partial output. Wait for that contract.
  let released=false;
  for(let attempt=0;attempt<100;attempt++){
    const result=await (await page.request.get(baseUrl+'/v1/chats/chat-1/active-run')).json();
    if(result.data===null){released=true;break;}
    await page.waitForTimeout(50);
  }
  assert.ok(released,'cancelled worker must release the writer fence');
  await page.locator('#reload').click();
  await page.waitForFunction(() => window.reloaded || window.failure);
  const reloaded = await page.evaluate(() => window.snapshot());
  const interrupted = reloaded.filter(m => m.role === 'assistant');
  assert.equal(interrupted.length, 1);
  assert.ok(interrupted[0].thinking.some(t => t.content.includes('PRIVATE_REASONING')));
  assert.ok(!interrupted[0].content.includes('PRIVATE_REASONING'));
  assert.equal(!!interrupted[0].isStreaming, false);
  await page.locator('#continue').click();
  await page.waitForFunction(() => window.done || window.failure);
  assert.equal(await page.evaluate(() => window.failure), null);
  const messages = await page.evaluate(() => window.snapshot());
  const answer = messages.filter(m => m.role === 'assistant').at(-1);
  assert.equal(answer.content, '已继续：端口检查结果已整理。');
  assert.ok(!answer.content.includes('Historical reasoning'));
  assert.ok(!answer.content.includes('PRIVATE_REASONING'));
  assert.equal(!!answer.isStreaming, false);
  await page.locator('.jx-msg').last().getByText('已继续：端口检查结果已整理。', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  await page.screenshot({ path: resolve(output, 'result.png'), fullPage: true });
  console.log('Browser HTTP/SSE cancel → reload → continue passed:', scenario);
} finally {
  await browser.close();
}
