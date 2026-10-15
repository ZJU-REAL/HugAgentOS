import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const fixture = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MessageBubble } from './src/components/chat/MessageBubble';
import { useChatStore } from './src/stores/chatStore';
import { processChatStream, markRunCancelledByUser } from './src/hooks/chatStream';
let controller;
import './src/styles/variables.css';
import './src/styles/motion.css';
import './src/styles/chat.css';
import './src/styles/common.css';
import './src/styles/tool.css';
useChatStore.setState({currentChatId:'reveal',store:{order:['reveal'],chats:{reveal:{
 id:'reveal',title:'Reveal',messages:[],createdAt:1,updatedAt:1
}}}});
function Fixture(){
 const messages=useChatStore(s=>s.store.chats.reveal.messages);
 return <main style={{maxWidth:800,padding:24}}>
  <button onClick={async()=>{controller=new AbortController();await processChatStream(await fetch('/api/v1/agents/responses',{method:'POST',signal:controller.signal}),{chatId:'reveal',enableThinking:false,signal:controller.signal});}}>start</button>
  <button onClick={()=>{const run=useChatStore.getState().activeRuns.reveal?.runId;if(run)markRunCancelledByUser(run);controller.abort();}}>cancel</button>
  <button onClick={()=>useChatStore.getState().updateStore(prev=>({...prev,chats:{...prev.chats,reveal:{...prev.chats.reveal,messages:[{uid:'history',role:'assistant',content:'历史正文 **已完成**',isMarkdown:true,isStreaming:false,ts:1}]}}}))}>history</button>
  <button onClick={()=>{const started=performance.now();useChatStore.getState().updateStore(prev=>({...prev,chats:{...prev.chats,reveal:{...prev.chats.reveal,messages:[{uid:'long-history',role:'assistant',content:'A complete historical paragraph. '.repeat(500),isMarkdown:true,isStreaming:false,ts:1}]}}}));requestAnimationFrame(()=>window.historyMs=performance.now()-started);}}>long-history</button>
  {messages.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId="reveal" send={()=>{}} exportChatRecord={async()=>{}} />)}
 </main>
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`;
const result=await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,
 format:'esm',jsx:'automatic',outfile:'fixture.js',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],
 loader:{'.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl'}});
const js=result.outputFiles.find(f=>f.path.endsWith('.js')).text;
const css=result.outputFiles.find(f=>f.path.endsWith('.css'))?.text||'';
let stream;
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname==='/api/v1/agents/responses'){
  res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'});
  stream=res;res.write(': connected\n\n');return;
 }
 if(url.pathname==='/emit'){
  let body='';for await(const chunk of req)body+=chunk;
  stream.write('data: '+body+'\n\n');res.end('ok');return;
 }
 if(url.pathname==='/fixture.js'){res.setHeader('Content-Type','text/javascript');res.end(js);return;}
 if(url.pathname==='/fixture.css'){res.setHeader('Content-Type','text/css');res.end(css);return;}
 if(url.pathname.startsWith('/api/')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({code:20000,data:[]}));return;}
 res.setHeader('Content-Type','text/html');res.end('<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>');
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const url='http://127.0.0.1:'+server.address().port;
const evidence=resolve('../../.scratch/grapheme-reveal-e2e');
await mkdir(evidence,{recursive:true});
try{
 const page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(url);
 await page.getByRole('button',{name:'start',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.jx-msg.assistant'));
 await page.request.post(url+'/emit',{data:{type:'content',delta:'你好👨‍👩‍👧‍👦é世界'}});
 await page.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.includes('世界'));
 const animations=await page.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').map(a=>({
  text:a.effect.target.textContent,duration:a.effect.getTiming().duration,delay:a.effect.getTiming().delay,easing:getComputedStyle(a.effect.target).animationTimingFunction
 })));
 assert.ok(animations.length>0,'new streamed text must fade in');
 assert.ok(animations.some(a=>a.text==='👨‍👩‍👧‍👦'),'family emoji is one animated grapheme');
 assert.ok(animations.some(a=>a.text==='é'),'combining accent is one animated grapheme');
 assert.ok(animations.every(a=>a.duration===350&&a.easing==='ease-out'));
 assert.deepEqual(animations.map(a=>a.delay),[0,65,130,195,260,325]);
 await page.screenshot({path:resolve(evidence,'01-reveal.png')});
 await page.waitForFunction(()=>!document.querySelector('.jx-msgText .jx-grapheme'));
 await page.locator('.jx-msgText').evaluate(el=>{
  const node=document.createTreeWalker(el,NodeFilter.SHOW_TEXT).nextNode();
  const range=document.createRange();range.setStart(node,0);range.setEnd(node,2);
  const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
  window.selectedNode=node;
 });
 await page.request.post(url+'/emit',{data:{type:'content',delta:'\n\n**结论** 新增文字'}});
 await page.locator('.jx-msgText strong').waitFor();
 assert.equal(await page.evaluate(()=>getSelection().toString()),'你好','Markdown arrival must preserve selection in earlier text');
 assert.equal(await page.evaluate(()=>window.selectedNode.isConnected),true,'stable prefix text node must survive');
 console.log('PASS Markdown arrival preserves earlier text and copy selection');
 await page.evaluate(()=>getSelection().removeAllRanges());
 await page.request.post(url+'/emit',{data:{type:'meta',citations:[{id:'e1',title:'可靠来源',snippet:'证据正文',url:'https://example.com',source_type:'internet'}]}});
 await page.request.post(url+'/emit',{data:{type:'content',delta:' [来源](cite:e1)'}});
 await page.locator('.jx-msgText .jx-citeRef').waitFor();
 await page.request.post(url+'/emit',{data:{type:'content_replace',content:'# 新结论 [来源](cite:e1)'}});
 await page.locator('.jx-msgText h1').waitFor();
 assert.equal(await page.locator('.jx-msgText h1 .jx-citeRef').count(),1,'citation portal must move with replaced Markdown structure');
 await page.locator('.jx-msgText h1 .jx-citeRef').hover();
 await page.getByText('可靠来源',{exact:true}).waitFor();
 console.log('PASS citation remains attached after content replacement');
 await page.request.post(url+'/emit',{data:{type:'content_replace',content:'## 格式验证\n\n- 第一项\n- 第二项\n\n| 列 | 值 |\n|---|---|\n| A | 1 |\n\n\x60\x60\x60js\nconst value = 42;\n\x60\x60\x60\n\n$E=mc^2$'}});
 await page.locator('.jx-msgText table').waitFor();
 await page.locator('.jx-msgText .katex').waitFor();
 assert.equal(await page.locator('.jx-msgText ul li').count(),2);
 assert.equal(await page.locator('.jx-msgText pre code').innerText(),'const value = 42;');
 assert.equal(await page.locator('.jx-msgText pre .jx-grapheme,.jx-msgText .katex .jx-grapheme').count(),0,'code and math DOM are not split into animated characters');
 console.log('PASS lists, tables, code highlighting and lazy math preserve structure');
 await page.locator('.jx-msgText').evaluate(el=>el.style.width='200px');
 const english='Streaming text keeps complete English words together. Smooth presentation preserves normal line wrapping.';
 await page.request.post(url+'/emit',{data:{type:'content_replace',content:english}});
 await page.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.includes('line wrapping.'));
 const wrapping=await page.locator('.jx-msgText').evaluate(el=>{
  const text=el.textContent,index=text.indexOf('presentation');
  const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
  const range=document.createRange();let offset=0,start=false;
  for(let node=walker.nextNode();node;node=walker.nextNode()){
   const end=offset+node.textContent.length;
   if(!start&&index<end){range.setStart(node,index-offset);start=true;}
   if(start&&index+12<=end){range.setEnd(node,index+12-offset);break;}
   offset=end;
  }
  return {lines:new Set([...range.getClientRects()].map(r=>Math.round(r.top))).size,overflow:el.scrollWidth>el.clientWidth};
 });
 assert.equal(wrapping.lines,1,'grapheme spans must not break a normal English word');
 assert.equal(wrapping.overflow,false);
 console.log('PASS narrow English word wrapping');
 await page.request.post(url+'/emit',{data:{type:'content_replace',content:'<img src="/bad-image" onerror="window.injected=true">\n\n[unsafe](javascript:alert(1))\n\n[external](https://example.com)'}});
 await page.locator('.jx-msgText img').waitFor({state:'attached'});
 assert.equal(await page.locator('.jx-msgText img').getAttribute('onerror'),null,'model HTML must not retain executable event attributes');
 assert.equal(await page.locator('.jx-msgText').getByText('unsafe',{exact:true}).getAttribute('href'),null,'model Markdown must not retain javascript links');
 const external=page.locator('.jx-msgText').getByRole('link',{name:'external',exact:true});
 assert.equal(await external.getAttribute('target'),'_blank','external links retain their new-tab behavior');
 assert.equal(await external.getAttribute('rel'),'noopener noreferrer');
 console.log('PASS Markdown HTML safety');


 await page.request.post(url+'/emit',{data:{type:'content_replace',content:'批量输出'.repeat(125)}});
 await page.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.trim().length===500);
 const burst=await page.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').map(a=>a.effect.getTiming().delay));
 assert.ok(burst.length>0);
 assert.ok(Math.max(...burst)<=601,'large burst must not leave an unbounded display backlog');
 await page.request.post(url+'/emit',{data:'[DONE]'});
 await page.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
 assert.equal(await page.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').length),0,'completion flushes all pending characters');
 await page.locator('.jx-msgText').scrollIntoViewIfNeeded();
 assert.equal(await page.locator('.jx-msgText').innerText(),'批量输出'.repeat(125));
 assert.ok(await page.locator('.jx-msgText').evaluate(el=>{const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let n=0;while(w.nextNode())n++;return n<10;}),'finished text must not retain hundreds of grapheme nodes');
 await page.getByRole('button',{name:'history',exact:true}).click();
 await page.getByText('已完成',{exact:true}).waitFor();
 assert.equal(await page.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').length),0,'history is shown immediately');
 console.log('PASS bounded burst, terminal flush, compact DOM and history');
 await page.close();

 const start=async(reducedMotion='no-preference')=>{
  const p=await browser.newPage({reducedMotion});
  p.on('pageerror',e=>errors.push(e.message));
  await p.goto(url);await p.getByRole('button',{name:'start',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('.jx-msg.assistant'));
  return p;
 };

 const split=await start();
 await split.request.post(url+'/emit',{data:{type:'content',delta:'组合e'}});
 await split.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.includes('组合e'));
 await split.request.post(url+'/emit',{data:{type:'content',delta:'́👩'}});
 await split.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.includes('é👩'));
 await split.request.post(url+'/emit',{data:{type:'content',delta:'‍💻结束'}});
 await split.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.includes('👩‍💻结束'));
 assert.equal(await split.locator('.jx-msgText').innerText(),'组合é👩‍💻结束');
 assert.equal(await split.locator('.jx-msgText').evaluate(el=>[...el.querySelectorAll('.jx-grapheme')].some(n=>n.textContent==='́'||n.textContent==='‍'||n.textContent==='💻')),false,'cross-frame clusters do not animate as detached combining/ZWJ suffixes');
 await split.request.post(url+'/emit',{data:'[DONE]'});await split.close();
 console.log('PASS graphemes split across network frames');

 const diagram=await start();
 await diagram.request.post(url+'/emit',{data:{type:'content',delta:'\x60\x60\x60mermaid\ngraph LR; A-->B\n\x60\x60\x60'}});
 await diagram.locator('.jx-mermaid').waitFor();
 assert.equal(await diagram.locator('.jx-mermaid svg.flowchart').count(),0,'charts wait until stream completion');
 await diagram.request.post(url+'/emit',{data:'[DONE]'});
 await diagram.locator('.jx-mermaid svg.flowchart').waitFor({timeout:30000});
 assert.equal(await diagram.locator('.jx-mermaid .jx-grapheme').count(),0);
 await diagram.close();
 console.log('PASS deferred Mermaid chart rendering');
 const restored=await start();
 await restored.request.post(url+'/emit',{data:{type:'run_snapshot',event_offset:10,state:{
  run_id:'restore-run',message_id:'restore-message',started_at:1,event_offset:10,terminal:false,signals:{},
  tools:[],blocks:[{kind:'text',content:'恢复的旧文字'}]
 }}});
 await restored.getByText('恢复的旧文字',{exact:true}).waitFor();
 assert.equal(await restored.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').length),0,'snapshot must not replay existing text');
 await restored.request.post(url+'/emit',{data:{type:'content',delta:'继续生成👩‍💻',event_offset:11}});
 await restored.waitForFunction(()=>document.querySelector('.jx-msgText')?.textContent.includes('继续生成'));
 assert.ok(await restored.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).some(a=>a.effect.target.textContent==='👩‍💻')),'new content after snapshot animates');
 await restored.getByRole('button',{name:'cancel',exact:true}).click();
 await restored.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
 assert.equal(await restored.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').length),0,'cancel reveals all received text');
 await restored.waitForFunction(()=>document.querySelector('.jx-msgText')?.innerText==='恢复的旧文字继续生成👩‍💻');
 assert.equal(await restored.locator('.jx-msgText').innerText(),'恢复的旧文字继续生成👩‍💻');
 await restored.close();
 console.log('PASS snapshot recovery, subsequent deltas and real AbortController cancellation');

 const reduced=await start('reduce');
 await reduced.request.post(url+'/emit',{data:{type:'content',delta:'减弱动画立即显示'}});
 await reduced.getByText('减弱动画立即显示',{exact:true}).waitFor();
 assert.equal(await reduced.locator('.jx-msgText').evaluate(el=>el.getAnimations({subtree:true}).filter(a=>a.animationName==='jxGraphemeReveal').length),0);
 await reduced.request.post(url+'/emit',{data:'[DONE]'});
 await reduced.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
 await reduced.close();
 console.log('PASS reduced-motion immediate display');

 const historyPage=await browser.newPage();
 await historyPage.goto(url);
 await historyPage.getByRole('button',{name:'long-history',exact:true}).click();
 await historyPage.waitForFunction(()=>window.historyMs!==undefined);
 const historyMetrics=await historyPage.locator('.jx-msgText').evaluate(el=>{
  const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let nodes=0;while(walker.nextNode())nodes++;
  return {milliseconds:window.historyMs,textLength:el.textContent.trim().length,textNodes:nodes};
 });
 assert.ok(historyMetrics.textLength>10000);
 assert.ok(historyMetrics.textNodes<10,'long history uses compact text nodes');
 assert.ok(historyMetrics.milliseconds<500,'long history must render without a half-second UI stall');
 console.log('PASS long history performance',JSON.stringify(historyMetrics));
 const longStream=await start();
 const paragraph='A long single paragraph. '.repeat(700);
 await longStream.request.post(url+'/emit',{data:{type:'run_snapshot',event_offset:20,state:{
  run_id:'performance-run',message_id:'performance-message',started_at:1,event_offset:20,terminal:false,signals:{},
  tools:[],blocks:[{kind:'text',content:paragraph}]
 }}});
 await longStream.waitForFunction(length=>document.querySelector('.jx-msgText')?.textContent.length>=length,paragraph.length);
 const samples=[];
 for(let i=0;i<12;i++){
  const suffix=' update-'+i;
  const started=performance.now();
  await longStream.request.post(url+'/emit',{data:{type:'content',delta:suffix,event_offset:21+i}});
  await longStream.waitForFunction(suffix=>document.querySelector('.jx-msgText')?.textContent.trimEnd().endsWith(suffix),suffix);
  samples.push(performance.now()-started);
 }
 samples.sort((a,b)=>a-b);
 assert.ok(samples.at(-1)<500,'long streamed paragraphs must keep updates responsive');
 await longStream.request.post(url+'/emit',{data:'[DONE]'});
 await longStream.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
 console.log('PASS long paragraph streaming performance',JSON.stringify({characters:paragraph.length,updates:samples.length,p50_ms:samples[6],p95_ms:samples[11]}));
 await longStream.close();

 await historyPage.close();

 assert.deepEqual(errors,[]);
 console.log('PASS real SSE → reducer → message bubble: graphemes and reference animation parameters');
}finally{stream?.end();await browser.close();await new Promise(r=>server.close(r));}
