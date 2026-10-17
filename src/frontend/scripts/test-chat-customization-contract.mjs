import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const output=resolve('node_modules/.tmp/chat-customization-contract');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {ChatCustomizationRoot} from './src/customization/ChatCustomization';
import {Controls} from '../../examples/customer-chat/src/controls';
import {ChatClient} from '@hugagent/chat-sdk';
const root=document.getElementById('root');
if(location.hash==='#invalid'){
 createRoot(root).render(<ChatCustomizationRoot><div>Never shown</div></ChatCustomizationRoot>);
}else{
 const client=new ChatClient({baseUrl:'/api'});
 const controls=new Controls(client,root,()=> 'chat/1',text=>{document.getElementById('status').textContent=text;});
 window.emit=event=>controls.consume(event);
}
`,resolveDir:process.cwd(),loader:'tsx'},bundle:true,format:'esm',jsx:'automatic',outdir:output,
 plugins:[{name:'invalid-contract',setup(builder){builder.onResolve({filter:/^\.\/selected$/},()=>({path:'invalid',namespace:'test'}));builder.onLoad({filter:/.*/,namespace:'test'},()=>({contents:"export default {apiVersion:999,id:'invalid'};"}));}}],
 alias:{react:resolve('node_modules/react'),'react-dom':resolve('node_modules/react-dom')},
 define:{'process.env.NODE_ENV':'"production"'}});
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM || '/home/aaron/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'});
try{
 const page=await browser.newPage();const requests=[];
 await page.route('http://contract.test/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname.startsWith('/api/')){
   requests.push({url:url.pathname,body:route.request().postDataJSON()});
   return route.fulfill({json:{code:10000,data:{ok:true}}});
  }
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:'<p id="status"></p><main id="root"></main><script type="module" src="/stdin.js"></script>'});
  return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,url.pathname.slice(1)))});
 });
 await page.goto('http://contract.test/#invalid');
 await page.getByRole('alert').filter({hasText:'Unsupported chat UI API version'}).waitFor();
 console.log('PASS incompatible API version renders a visible error');
 await page.goto('http://contract.test/');
 await page.waitForFunction(()=>typeof window.emit==='function');
 await page.evaluate(()=>window.emit({type:'file_confirm',confirm_id:'confirm/1',logical_path:'<script>bad()</script>'}));
 assert.equal(await page.locator('script').count(),1);
 await page.getByRole('button',{name:'Deny',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('[data-request]'));
 assert.deepEqual(requests.at(-1),{url:'/api/v1/chats/chat%2F1/file-confirm',body:{confirm_id:'confirm/1',decision:'deny'}});
 await page.evaluate(()=>window.emit({type:'user_question',request_id:'question/1',questions:[{id:'q1',question:'Choose',options:[{id:'yes',label:'Yes'}]}]}));
 await page.getByRole('radio',{name:'Yes',exact:true}).check();
 await page.getByRole('button',{name:'Submit answers',exact:true}).click();
 await page.waitForTimeout(100);
 assert.deepEqual(requests.at(-1).body,{answers:[{id:'q1',selected:['yes'],custom:''}]});
 assert.equal(await page.locator('[data-request]').count(),0,'accepted question must close even if SSE is delayed');
 await page.evaluate(()=>window.emit({type:'user_question_resolved',request_id:'question/1'}));
 assert.equal(await page.locator('[data-request]').count(),0);
 console.log('PASS file denial, question answers, server resolution, encoded IDs and safe text rendering');
}finally{await browser.close();}
