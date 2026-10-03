// Production-bundle browser -> HTTP -> real PostgreSQL chat routes.
// Authentication/catalog are controlled fixtures; no external model is invoked.
import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { gzipSync } from 'node:zlib';
import { chromium } from 'playwright';
const output=resolve('../../.git/performance-172');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_EXECUTABLE});
const results={};
try {
 for(const variant of ['before','after']) {
  const dist=variant==='before'?resolve(output,'dist-before'):resolve('dist');
  const port=variant==='before'?38171:38172;
  const page=await browser.newPage({viewport:{width:1440,height:900}});
  const errors=[]; const requests=[]; const assets=new Map();
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>localStorage.setItem('jx_lang','zh-CN'));
  await page.route('http://performance.test/**',async route=>{
   const url=new URL(route.request().url()); const path=url.pathname;
   if(path.startsWith('/api')) {
    if(/^\/api\/v1\/chats(?:$|\/search$|\/perf-history(?:$|\/messages$))/.test(path)) {
     requests.push(path+url.search);
     const response=await fetch(`http://127.0.0.1:${port}${path.slice(4)}${url.search}`);
     return route.fulfill({status:response.status,contentType:'application/json',body:await response.text()});
    }
    let data={};
    if(path.endsWith('/auth/session/check')) data={user_id:'perf-user',username:'perf',display_name:'Performance'};
    else if(path.endsWith('/meta/edition')) data={edition:'ee',features:{},license:{}};
    else if(path.endsWith('/catalog')) data={skills:[],mcp:[],agents:[],kb:[]};
    else if(/chats|tasks|notifications|models|agents|installed|contributions|automations|teams|folders|skills|projects/.test(path)) data={items:[],models:[],count:0};
    return route.fulfill({json:{code:0,data}});
   }
   const file=resolve(dist,path==='/'?'index.html':path.slice(1));
   const body=await readFile(file).catch(()=>null);
   if(!body) return route.fulfill({status:404});
   if(path.endsWith('.js')) assets.set(path,{bytes:body.length,gzip:gzipSync(body).length});
   return route.fulfill({body,contentType:path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':path.endsWith('.svg')?'image/svg+xml':path.startsWith('/assets/')?'application/octet-stream':'text/html'});
  });
  const started=Date.now();
  await page.goto('http://performance.test/');
  await page.getByText('Performance history',{exact:true}).first().waitFor({timeout:15000}).catch(async e=>{console.log('BODY',await page.locator('body').innerText(),'ERRORS',errors);await page.screenshot({path:resolve(output,`failed-${variant}.png`)});throw e;});
  const bootMs=Date.now()-started;
  await page.getByText('Performance history',{exact:true}).first().click();
  await page.getByText(/Performance message 0400/).first().waitFor({timeout:30000});
  await page.waitForTimeout(500);
  const initialMessages=await page.locator('.jx-msg').count();
  const scroll=page.locator('.jx-content');
  for(let i=0;i<3;i++) {
   await scroll.evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll'));});
   await page.waitForTimeout(500);
  }
  const olderMessages=await page.locator('.jx-msg').count();
  assert.ok(olderMessages>initialMessages, 'scroll loads real older API pages');
  const ids=await page.locator('.jx-msg').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('data-message-uid')));
  assert.equal(new Set(ids).size,ids.length,'no duplicate bubbles');
  const selection=await page.locator('.jx-msg').first().evaluate(el=>{const range=document.createRange();range.selectNodeContents(el);getSelection().removeAllRanges();getSelection().addRange(range);return getSelection().toString().length;});
  assert.ok(selection>0,'offscreen containment preserves text selection');
  await page.screenshot({path:resolve(output,`browser-${variant}.png`)});
  assert.deepEqual(errors,[]);
  results[variant]={bootMs,initialMessages,olderMessages,initialJsBytes:[...assets.values()].reduce((a,b)=>a+b.bytes,0),initialJsGzipBytes:[...assets.values()].reduce((a,b)=>a+b.gzip,0),jsRequests:assets.size,historyRequests:requests.filter(x=>x.includes('/messages')),errors};
  await page.close();
 }
 await writeFile(resolve(output,'browser-results.json'),JSON.stringify(results,null,2));
 console.log(JSON.stringify(results,null,2));
} finally {await browser.close();}
