import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const base=process.env.GRAPHEME_E2E_URL||'http://localhost:3320';
const evidence=resolve('../../.scratch/grapheme-reveal-e2e');
await mkdir(evidence,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
const results=[];
const scenarioFilter=process.argv[2];
try {
 for (const theme of scenarioFilter?[]:['light','dark']) {
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'no-preference'});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
   window.reveals=[];
   document.addEventListener('animationstart',event=>{
    if(event.animationName!=='jxGraphemeReveal')return;
    const style=getComputedStyle(event.target);
    window.reveals.push({text:event.target.textContent,duration:style.animationDuration,
     easing:style.animationTimingFunction,delay:style.animationDelay});
   },true);
  });
  await page.goto(base+'/mock-sso/login?auto=0&redirect=/');
  const editor=page.locator('.jx-composerEditor');
  await editor.waitFor({timeout:60000});
  await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
  await editor.fill('请验证正文的流式呈现。');
  const admitted=page.waitForResponse(r=>r.url().endsWith('/v1/agents/responses'));
  await editor.press('Enter');
  assert.equal((await admitted).status(),200);
  await page.waitForFunction(()=>window.reveals.some(item=>item.text==='👨‍👩‍👧‍👦'));
  await page.screenshot({path:resolve(evidence,'app-'+theme+'-stream.png'),fullPage:true});
  await page.locator('.jx-msg.assistant .jx-msgText').last().evaluate(el=>{
   const node=document.createTreeWalker(el,NodeFilter.SHOW_TEXT).nextNode();
   const range=document.createRange();range.setStart(node,0);range.setEnd(node,2);
   getSelection().removeAllRanges();getSelection().addRange(range);
   window.stableTextNode=node;
  });

  await page.getByText('验证结束。',{exact:true}).waitFor({timeout:60000});
  await page.locator('.jx-msg.assistant .jx-bubble.streaming').waitFor({state:'hidden'});
  const body=page.locator('.jx-msg.assistant .jx-msgText').last();
  assert.equal(await body.locator('strong').innerText(),'结论');
  assert.equal(await body.locator('li').count(),2);
  assert.equal(await body.locator('pre code').innerText(),'const value = 42;');
  assert.equal(await body.locator('.jx-grapheme').count(),0);
  assert.equal(await page.evaluate(()=>getSelection().toString()),'你好','selection survives actual streaming updates');
  assert.equal(await page.evaluate(()=>window.stableTextNode.isConnected),true);
  const reveals=await page.evaluate(()=>window.reveals);
  assert.ok(reveals.length>10);
  assert.ok(reveals.every(item=>item.duration==='0.35s'&&item.easing==='ease-out'));
  await body.evaluate(el=>{
   const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
   const node=walker.nextNode(),range=document.createRange();
   range.setStart(node,0);range.setEnd(node,2);
   const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
  });
  assert.equal(await page.evaluate(()=>getSelection().toString()),'你好');
  await page.screenshot({path:resolve(evidence,'app-'+theme+'-complete.png'),fullPage:true});
  const url=page.url();
  await page.reload();
  await page.getByText('验证结束。',{exact:true}).waitFor({timeout:60000});
  assert.equal(page.url(),url);
  assert.equal(await page.evaluate(()=>window.reveals.length),0,'persisted history must not replay animation');
  assert.deepEqual(errors,[]);
  results.push({theme,animations:reveals.length,admission:200,historyReload:true,errors});
  console.log('PASS full application '+theme+': login → send → FastAPI/Redis/model/SSE → animation → formatted result → history reload');
  await page.close();
 }

 for(const scenario of scenarioFilter?[scenarioFilter]:['cancel','resume','reduced-motion']) {
  const page=await browser.newPage({viewport:{width:1280,height:900},reducedMotion:scenario==='reduced-motion'?'reduce':'no-preference'});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.reveals=[];
   document.addEventListener('animationstart',event=>{
    if(event.animationName==='jxGraphemeReveal')window.reveals.push(event.target.textContent);
   },true);
  });
  await page.goto(base+'/mock-sso/login?auto=0&redirect=/');
  const editor=page.locator('.jx-composerEditor');await editor.waitFor({timeout:60000});
  await editor.fill('请执行 '+scenario+' 流式测试。');
  const response=page.waitForResponse(r=>r.url().endsWith('/v1/agents/responses'));
  await editor.press('Enter');assert.equal((await response).status(),200);
  await page.waitForFunction(()=>document.querySelector('.jx-msg.assistant .jx-msgText')?.textContent.includes('👨‍👩‍👧‍👦'));
  if(scenario==='cancel'){
   const body=page.locator('.jx-msg.assistant .jx-msgText').last();
   const received=await body.innerText();
   const cancelled=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/cancel'));
   await page.getByRole('button',{name:'中止',exact:true}).click();
   assert.equal((await cancelled).status(),200);
   await page.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
   assert.ok((await body.innerText()).startsWith(received));
   assert.equal(await body.locator('.jx-grapheme').count(),0);
  }else if(scenario==='resume'){
   await page.reload();
   await page.getByText('验证结束。',{exact:true}).waitFor({timeout:60000});
   await page.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
   const replay=await page.evaluate(()=>window.reveals);
   assert.ok(replay.length>0,'new output after reload must still animate');
   assert.ok(!replay.some(text=>text==='你'||text==='好'||text==='👨‍👩‍👧‍👦'),'existing output must not replay after active-run reload');
   assert.equal(await page.locator('.jx-msg.assistant .jx-msgText').last().locator('li').count(),2);
  }else{
   await page.getByText('验证结束。',{exact:true}).waitFor({timeout:60000});
   await page.locator('.jx-bubble.streaming').waitFor({state:'hidden'});
   assert.equal(await page.evaluate(()=>window.reveals.length),0);
  }
  assert.deepEqual(errors,[]);
  results.push({scenario,errors});
  await page.screenshot({path:resolve(evidence,'app-'+scenario+'.png'),fullPage:true});
  console.log('PASS full application '+scenario);
  await page.close();
 }
 await writeFile(resolve(evidence,'full-app-results.json'),JSON.stringify(results,null,2));
}finally{await browser.close();}
