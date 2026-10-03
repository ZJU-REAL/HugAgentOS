// Real React/Zustand message tree; deterministic stream updates, no external LLM.
import {build} from 'esbuild';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const output=resolve('../../.git/performance-172');
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
const results={};
try {
 for(const variant of ['before','after']) {
  const source=variant==='before'?resolve(output,'before/src/frontend'):process.cwd();
  const dir=resolve(output,`stream-${variant}`);await mkdir(dir,{recursive:true});
  await build({stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {flushSync} from 'react-dom';
import {useChatStore} from './src/stores/chatStore';
import {MessageBubble} from './src/components/chat/MessageBubble';
import './src/styles/variables.css';
import './src/styles/chat.css';
const noop=()=>{};const exportChat=async()=>{};
const messages=Array.from({length:300},(_,i)=>({uid:'m'+i,messageId:'m'+i,role:'assistant',isMarkdown:true,ts:0,content:'**Message '+i+'** '+ 'readable text '.repeat(15)+' [source](cite:e1)',citations:i===0?[{id:'e1',title:'Original source',url:'https://example.com'}]:undefined}));
const store=useChatStore.getState().store;
useChatStore.setState({store:{...store,chats:{c:{id:'c',title:'Performance',messages}},currentChatId:'c'}});
function App(){const rows=useChatStore(s=>s.store.chats.c.messages);return <div id="list">{rows.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId="c" send={noop} exportChatRecord={exportChat}/>)}</div>}
flushSync(()=>createRoot(document.getElementById('root')).render(<App/>));
window.ready=true;
window.measure=async()=>{
 const samples=[];
 for(let i=0;i<40;i++) {
  await new Promise(requestAnimationFrame);
  const start=performance.now();
  flushSync(()=>useChatStore.setState(s=>{const chat=s.store.chats.c;const rows=chat.messages.slice();rows[rows.length-1]={...rows.at(-1),content:rows.at(-1).content+' token',isStreaming:true};return {store:{...s.store,chats:{...s.store.chats,c:{...chat,messages:rows}}}}}));
  document.getElementById('list').getBoundingClientRect();
  samples.push(performance.now()-start);
 }
 return samples;
};`,resolveDir:source,loader:'tsx'},bundle:true,format:'esm',jsx:'automatic',outdir:dir,nodePaths:[resolve('node_modules')],define:{'import.meta.env':'{}','process.env.NODE_ENV':'"production"'},loader:{'.svg':'dataurl','.png':'dataurl','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.md':'text'},external:['/loader.gif','/loader-done.png']});
  const page=await browser.newPage({viewport:{width:1280,height:800}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://stream.test/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/') return route.fulfill({contentType:'text/html',body:'<link rel="stylesheet" href="/stdin.css"><style>#root{height:760px;overflow:auto}#list{width:850px;margin:auto}</style><div id="root"></div><script type="module" src="/stdin.js"></script>'});
   const body=await readFile(resolve(dir,path.slice(1))).catch(()=>null);
   return route.fulfill({status:body?200:404,body:body??'',contentType:path.endsWith('.js')?'text/javascript':'text/css'});
  });
  await page.goto('http://stream.test/');await page.waitForFunction(()=>window.ready);
  await page.locator('#root').evaluate(el=>el.scrollTop=el.scrollHeight);
  await page.waitForTimeout(300);
  const samples=await page.evaluate(()=>window.measure());samples.sort((a,b)=>a-b);
  assert.deepEqual(errors,[]);assert.equal(await page.locator('.jx-msg').count(),300);
  results[variant]={messages:300,updates:samples.length,p50_ms:samples[20],p95_ms:samples[38],errors};
  await page.screenshot({path:resolve(output,`stream-${variant}.png`)});
  await page.close();
 }
 await writeFile(resolve(output,'stream-results.json'),JSON.stringify(results,null,2));console.log(JSON.stringify(results,null,2));
}finally{await browser.close();}
