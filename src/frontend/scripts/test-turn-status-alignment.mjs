import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { createServer } from 'node:http';

// Render the real message state transition from startup to reasoning.
const fixture = `
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MessageBody} from './src/components/chat/MessageBody';
import {useChatStore} from './src/stores';
import './src/styles/variables.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
useChatStore.setState({currentChatId:'alignment',currentUserId:'alignment-test',store:{order:['alignment'],chats:{alignment:{id:'alignment',title:'Alignment',messages:[],createdAt:1,updatedAt:1}}}});
function Fixture(){
 const [phase,setPhase]=useState(false);
 const m={uid:'alignment-message',role:'assistant',content:'',isStreaming:true,lastActivityTs:Date.now(),segments:phase?[{type:'thinking',content:'Checking the report.'}]:[],ts:1};
 return <main style={{padding:20,maxWidth:700}}>
 <button id='phase' onClick={()=>setPhase(v=>!v)}>Switch state</button>
 <section id='message' className='jx-msg assistant'><div className='jx-msgContent'><MessageBody m={m} messageIndex={0} currentChatId='alignment' send={()=>{}}/></div></section>
 </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`;
const bundle = await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,format:'esm',jsx:'automatic',outfile:'fixture.js',define:{'import.meta.env':'{}'},loader:{'.css':'css','.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl'}});
const js=bundle.outputFiles.find(f=>f.path.endsWith('.js')).text;
const css=bundle.outputFiles.find(f=>f.path.endsWith('.css')).text;
const server=createServer(async(req,res)=>{
 if(req.url==='/fixture.js'){res.setHeader('Content-Type','text/javascript');res.end(js);}
 else if(req.url==='/fixture.css'){res.setHeader('Content-Type','text/css');res.end(css);}
 else{res.setHeader('Content-Type','text/html');res.end('<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
try{
 browser=await chromium.launch({headless:true});
 const page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:'+server.address().port);
 await page.waitForSelector('#message .jx-turnStatusText');
 const measure=selector=>page.locator(selector).evaluate(el=>{
  const row=el.querySelector('.jx-turnStatusRow'),mark=el.querySelector('.jx-agentSlot'),text=el.querySelector('.jx-turnStatusText');
  const r=row.getBoundingClientRect(),m=mark.getBoundingClientRect(),t=text.getBoundingClientRect(),s=getComputedStyle(text);
  return {rowHeight:r.height,markX:m.x-r.x,markY:m.y-r.y,markWidth:m.width,markHeight:m.height,textX:t.x,textY:t.y,textHeight:t.height,fontSize:s.fontSize,fontWeight:s.fontWeight,lineHeight:s.lineHeight};
 });
 for(const width of [736,320])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:600});
  await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
  const before=await measure('#message');
  await page.click('#phase');
  for(const delay of [0,80,240]){
   if(delay)await page.waitForTimeout(delay);
   const after=await measure('#message');
   for(const key of Object.keys(before)){
    if(typeof before[key]==='number')assert(Math.abs(before[key]-after[key])<.1,theme+'/'+width+' '+key+': '+before[key]+' → '+after[key]);
    else assert.equal(after[key],before[key],key);
   }
  }
  const text=await page.locator('#message .jx-trs-title').textContent();assert(text.includes('正在思考'));
  await page.click('#message .jx-trs-head');await page.waitForTimeout(250);
  await page.click('#message .jx-tcr-header');await page.waitForTimeout(250);
  assert(await page.locator('#message .jx-thinkingDetailBody').isVisible());
  await page.click('#phase');await page.waitForSelector('#message .jx-turnStatus');
 }
 assert.deepEqual(errors,[]);
 console.log('PASS: actual MessageBody startup → reasoning positions/sizes match at 0/80/240ms; light/dark; 320/736px; disclosure works.');
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
