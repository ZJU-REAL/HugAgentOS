import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const root=resolve('../../examples/customer-chat/standalone-dist');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM || '/home/aaron/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'});
try{
 const page=await browser.newPage();
 let snapshot=false;
 const pages=[];
 await page.route('http://standalone.test/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname.startsWith('/api/')){
   let data={};
   if(url.pathname.endsWith('/messages')){
    const number=Number(url.searchParams.get('page'));pages.push(number);
    data={items:number===1?Array.from({length:100},(_,i)=>({message_id:'m'+i,role:'user',content:'History '+i,chat_seq:i+1})): [{message_id:'latest',role:'assistant',content:'Newest reply',chat_seq:101}]};
    if(!snapshot)await new Promise(resolve=>setTimeout(resolve,250));
   }else if(url.pathname.endsWith('/pending-confirm'))data={pendings:[]};
   else if(url.pathname.endsWith('/pending-user-questions'))data={requests:[]};
   else if(url.pathname.endsWith('/active-run'))data=snapshot?{run_id:'run1',status:'running'}:null;
   else if(url.pathname.endsWith('/subscription')){
    return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'run_snapshot',state:{terminal:false,blocks:[{kind:'text',content:'Discarded answer'},{kind:'answer',content:'Replaced answer'},{kind:'text',content:' continued'}],signals:{}}})+'\n\n'});
   }
   return route.fulfill({json:{code:10000,data}});
  }
  const file=url.pathname.endsWith('/')?'index.html':url.pathname.replace(/^\/customer\//,'');
  return route.fulfill({contentType:file.endsWith('.html')?'text/html':file.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(root,file))});
 });
 await page.goto('http://standalone.test/customer/?chat=history');
 assert.equal(await page.getByRole('button',{name:'New chat',exact:true}).isDisabled(),true);
 assert.equal(new URL(page.url()).searchParams.get('chat'),'history','initialization must prevent switching while old data is pending');
 await page.getByText('Newest reply',{exact:true}).waitFor();
 assert.equal(await page.locator('#messages > p').count(),101);
 assert.deepEqual(pages,[1,2]);
 await page.waitForFunction(()=>!document.getElementById('send').disabled);
 await page.getByRole('button',{name:'New chat',exact:true}).click();
 assert.equal(await page.locator('#messages > p').count(),0);
 console.log('PASS paged history after 100 rows and initialization concurrency guard');
 snapshot=true;
 await page.goto('http://standalone.test/customer/?chat=snapshot');
 await page.locator('[data-role="assistant"]').filter({hasText:'Replaced answer continued'}).waitFor();
 assert.equal(await page.locator('#messages').innerText().then(text=>text.includes('Discarded answer')),false);
 console.log('PASS native snapshot applies replacement blocks in order');
}finally{await browser.close();}
