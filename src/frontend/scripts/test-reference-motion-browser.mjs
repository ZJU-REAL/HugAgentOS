import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const evidence=resolve('../../.scratch/reference-motion-20261014');
await mkdir(evidence,{recursive:true});
const fixture=`
import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MessageBubble} from './src/components/chat/MessageBubble';
import {TurnStatusIndicator} from './src/components/chat/TurnStatusIndicator';
import {ThinkingInline} from './src/components/chat/ThinkingInline';
import {ToolRunShell} from './src/components/tool/ToolRunShell';
import PanelLoadingSkeleton from './src/components/common/PanelLoadingSkeleton';
import {newMessageUid} from './src/utils/messageIdentity';
import './src/styles/variables.css';
import './src/styles/motion.css';
import './src/styles/skeleton.css';
import './src/styles/panel-loading.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
const ts=Date.now();
const messages=[{uid:newMessageUid(true),role:'user',content:'帮我解释这个效果',ts},
 {uid:newMessageUid(true),role:'assistant',content:'我把重点整理好了。',ts}];
function Fixture(){
 const [done,setDone]=useState(false);
 const tool={id:'reference-tool',name:'Bash',input:{command:'pwd'},status:done?'success':'running',startTs:ts,endTs:done?ts+1000:undefined,output:done?'/workspace':undefined};
 const steps=[{kind:'tool',key:'tool',tool},{kind:'thinking',key:'thinking',content:'检查输入并整理线索',active:!done}];
 return <main>
 <button onClick={()=>setDone(true)}>完成</button>
 <section>{messages.map((m,i)=><MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId='motion-fixture' send={()=>{}} exportChatRecord={async()=>{}}/>)}</section>
 {!done&&<TurnStatusIndicator startTs={ts}/>}
 <ThinkingInline content='理解问题，整理线索。' isActive={!done}/>
 <ToolRunShell steps={steps} isStreaming={!done}/>
 <PanelLoadingSkeleton/>
 </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`;
const result=await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,
 format:'esm',jsx:'automatic',outfile:'fixture.js',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],
 loader:{'.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl'}});
const js=result.outputFiles.find(f=>f.path.endsWith('.js')).text;
const css=result.outputFiles.find(f=>f.path.endsWith('.css')).text;
const server=createServer((req,res)=>{
 if(req.url==='/fixture.js'){res.setHeader('Content-Type','text/javascript');res.end(js);return;}
 if(req.url==='/fixture.css'){res.setHeader('Content-Type','text/css');res.end(css);return;}
 if(req.url.startsWith('/api/')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({code:20000,data:[]}));return;}
 if(req.url==='/loader.gif'||req.url==='/loader-done.png'){res.statusCode=204;res.end();return;}
 res.setHeader('Content-Type','text/html');res.end('<link rel="stylesheet" href="/fixture.css"><style>html,body{height:auto;overflow:auto}main{max-width:736px;margin:24px auto;padding:16px;display:grid;gap:24px}section{display:grid;gap:12px}</style><div id="root"></div><script type="module" src="/fixture.js"></script>');
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const errors=[],results=[];
try{
 for(const theme of ['light','dark']){
  const page=await browser.newPage({viewport:{width:960,height:1100}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('.jx-msg.user .jx-bubble').waitFor();
  await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
  const entrance=await page.locator('section').evaluate(el=>el.getAnimations({subtree:true}).map(a=>({name:a.animationName,frames:a.effect.getKeyframes(),duration:a.effect.getTiming().duration})));
  const user=entrance.find(a=>a.name==='jx-userBubbleEnter');
  const assistant=entrance.find(a=>a.name==='jx-kf-fadeInUp');
  // These one-shot entrances can already have completed on a busy CI worker.
  const styles=await page.locator('section').evaluate(el=>({user:getComputedStyle(el.querySelector('.jx-bubble.user')).animationDuration,assistant:getComputedStyle(el.querySelector('.jx-msg.assistant')).animationDuration}));
  assert.equal(styles.user,'0.3s');assert.equal(styles.assistant,'0.35s');
  if(user)assert.equal(user.frames[0].transform,'scale(0.92) translateY(6px)');
  if(assistant)assert.equal(assistant.frames[0].transform,'translateY(10px)');
  await page.locator('.jx-trs-head').click();
  const labels=page.locator('.jx-rollText-text.is-shimmer,.jx-inlineSummaryText--live,.jx-tcr-prefix--shimmer');
  const live=await labels.evaluateAll(els=>els.map(el=>{const s=getComputedStyle(el);return {name:s.animationName,duration:s.animationDuration,size:s.backgroundSize,clip:s.backgroundClip,gradient:s.backgroundImage};}));
  assert.ok(live.length>=5,'startup, inline thinking, execution title, tool and thinking step labels participate');
  assert.ok(live.every(s=>s.name==='jxTurnStatusShine'&&s.duration==='2.8s'&&s.size==='250% 100%'&&s.clip==='text'),'all running labels use the same reference scan');
  assert.equal(new Set(live.map(s=>s.gradient)).size,1,'tool.css must not override the shared gradient');
  const summary=page.locator('.jx-trs-summary');
  assert.equal(await summary.locator('.jx-trs-dur').count(),1);
  assert.equal(await page.locator('.jx-trs-steps').count(),0,'the tool-kind chips replace the step count');
  assert.equal(await page.locator('.jx-trs-head .jx-kindChip').count(),1,'one chip per kind of action');
  assert.equal(await page.locator('.jx-trs-head .jx-kindChip.is-running').count(),1,'the running kind is lit');
  assert.equal(await page.locator('.jx-trs-head .jx-agentMark--work').count(),0,'a run with tools shows its chips, not the agent mark');
  assert.equal(await page.locator('.jx-trs-head .jx-agentSlot').count(),0,'a run with tools leaves no mark slot');
  assert.equal(await page.locator('.jx-trs-head .jx-kindStack .jx-kindChip.is-running').count(),1,'the lit chip carries the running state');
  const title=summary.locator('.jx-rollText-text.is-shimmer');
  const scan=await title.evaluate(el=>{const animation=el.getAnimations()[0];window.summaryScan=animation;return {start:animation.startTime};});
  const before=await summary.locator('.jx-trs-dur').textContent();
  await page.waitForTimeout(1100);
  const tick=await title.evaluate(el=>({same:el.getAnimations()[0]===window.summaryScan,start:el.getAnimations()[0].startTime,fill:getComputedStyle(el).webkitTextFillColor}));
  assert.equal(tick.same,true,'timer ticks do not restart the title scan');
  assert.equal(tick.start,scan.start);
  assert.notEqual(await summary.locator('.jx-trs-dur').textContent(),before,'elapsed seconds continue to update');
  assert.equal(tick.fill,'rgba(0, 0, 0, 0)','the running title is the scanned surface');
  assert.notEqual(await summary.locator('.jx-trs-dur').evaluate(el=>getComputedStyle(el).webkitTextFillColor),'rgba(0, 0, 0, 0)','only the title scans; the clock stays plain');
  const positions=await page.locator('.jx-turnStatus-label .jx-rollText-text').evaluate(el=>{const a=el.getAnimations()[0];a.pause();const d=a.effect.getTiming().delay;return [0,280,1400,2500].map(t=>{a.currentTime=t+d+2800;return getComputedStyle(el).backgroundPosition;});});
  // Labels phase-lock to a shared clock through a negative delay; sample one full cycle in.
  assert.equal(positions[0],positions[1],'scan holds for the first 15%');assert.notEqual(positions[1],positions[2]);
  const skeleton=await page.locator('.jx-skeletonBlock').first().evaluate(el=>{const s=getComputedStyle(el,'::after');return {name:s.animationName,duration:s.animationDuration,gradient:s.backgroundImage,pointer:s.pointerEvents};});
  assert.equal(skeleton.name,'jx-skeletonSweep');assert.equal(skeleton.duration,'1.6s');assert.ok(skeleton.gradient.includes('110deg'));assert.equal(skeleton.pointer,'none');
  await page.screenshot({path:resolve(evidence,theme+'-running.png'),fullPage:true});
  await page.getByRole('button',{name:'完成',exact:true}).click();
  assert.equal(await labels.count(),0,'completed thinking and tool labels stop scanning');
  assert.equal(await page.locator('.jx-trs-head .jx-agentMark--rest').count(),0,'a settled run with tools shows no grey mark');
  assert.equal(await page.locator('.jx-trs-head .jx-kindChip').count(),1,'a settled run keeps its chips');
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.reload();
  await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
  const reduced=await page.locator('section').evaluate(el=>[...el.querySelectorAll('.jx-msg.assistant,.jx-bubble.user')].map(n=>({opacity:getComputedStyle(n).opacity,transform:getComputedStyle(n).transform})));
  assert.ok(reduced.every(s=>s.opacity==='1'&&['none','matrix(1, 0, 0, 1, 0, 0)'].includes(s.transform)),'reduced motion shows both message types immediately');
  assert.equal(await page.locator('.jx-turnStatus-label .jx-rollText-text').evaluate(el=>getComputedStyle(el).animationName),'jxTurnStatusShine');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'no narrow-screen overflow');
  await page.screenshot({path:resolve(evidence,theme+'-mobile-reduced.png'),fullPage:true});
  results.push({theme,liveLabels:live.length,entrance,positions,skeleton,reduced});
  await page.close();
 }
 assert.deepEqual(errors,[]);
 await writeFile(resolve(evidence,'browser-results.json'),JSON.stringify({results,errors},null,2));
 console.log('PASS real message entrances, all running labels, asymmetric scan, skeleton sweep, completed states, light/dark, reduced motion and 390px layout');
}finally{await browser.close();await new Promise(r=>server.close(r));}
