import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const kind=process.argv[2] || 'default';
const base=process.env.CHAT_E2E_URL || 'http://localhost:3400';
const output=resolve('../../.scratch/chat-customization-e2e');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM || '/home/aaron/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
const errors=[];page.on('pageerror',e=>errors.push(e.message));
const passed=[];
const stamp=String(Date.now());
const attachmentName='customization-'+kind+'-'+stamp+'.txt';
async function check(name, fn){await fn();passed.push(name);console.log('PASS',kind,name);}
try{
 await page.goto(base+'/mock-sso/login?auto=0&redirect=/');
 const editor=page.locator('.jx-composerEditor');await editor.waitFor({timeout:60000});
 await check('selected UI is mounted',async()=>{
  assert.equal(await page.locator('[data-chat-customization]').getAttribute('data-chat-customization'),kind==='custom'?'customer-portal':'default');
  if(kind==='custom'){
   await page.getByRole('button',{name:'Use a template',exact:true}).click();
   await assert.doesNotReject(()=>editor.getByText('Please summarize this document.').waitFor());
  }
 });
 await page.screenshot({path:resolve(output,kind+'-welcome.png'),fullPage:true});
 await check('real admission, SSE and rendered reply',async()=>{
  await editor.fill('请简要说明本次定制界面测试结果。');
  const admitted=page.waitForResponse(r=>r.url().endsWith('/v1/agents/responses'));
  await editor.press('Enter');assert.equal((await admitted).status(),200);
  await page.getByText('端到端测试已完成。附件内容可以在预览中核对。',{exact:true}).waitFor({timeout:120000});
  await page.locator('.jx-msg.assistant .jx-bubble.streaming').waitFor({state:'hidden'});
  if(kind==='custom')assert.ok(await page.getByTestId('customer-message').count()>=2);
 });
 await check('history survives reload',async()=>{
  await page.reload();await page.getByText('端到端测试已完成。附件内容可以在预览中核对。',{exact:true}).waitFor({timeout:60000});
 });
 await page.screenshot({path:resolve(output,kind+'-chat.png'),fullPage:true});
 await page.emulateMedia({colorScheme:'dark'});
 await page.waitForFunction(()=>document.documentElement.getAttribute('data-theme')==='dark');
 await page.screenshot({path:resolve(output,kind+'-dark.png'),fullPage:true});
 await page.emulateMedia({colorScheme:'light'});
 await page.waitForFunction(()=>document.documentElement.getAttribute('data-theme')!=='dark');
 await page.setViewportSize({width:390,height:844});
 const backdrop=page.locator('.jx-mobileSidebarBackdrop');
 if(await backdrop.isVisible()) await backdrop.click({force:true});
 await backdrop.waitFor({state:'hidden'});
 await page.waitForTimeout(250);
 assert.equal(await page.locator('.jx-composerEditor').isVisible(),true);
 await page.screenshot({path:resolve(output,kind+'-chat-mobile.png'),fullPage:true});
 await page.setViewportSize({width:1440,height:1000});
 await check('real upload and file preview',async()=>{
  const file=page.locator('input[type=file]').filter({hasNot:page.locator('[accept]')}).first();
  await file.setInputFiles({name:attachmentName,mimeType:'text/plain',buffer:Buffer.from('Customer attachment evidence 2026')});
  await page.getByRole('button',{name:'预览 '+attachmentName,exact:true}).click();
  await page.getByText('Customer attachment evidence 2026',{exact:false}).last().waitFor({timeout:30000});
 });
 await page.screenshot({path:resolve(output,kind+'-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:resolve(output,kind+'-mobile.png'),fullPage:true});
 await check('SDK against real HTTP backend: send, replay, terminal subscription, upload and unauthorized access',async()=>{
  const outcome=await page.evaluate(async nonce=>{
   const {ChatClient,ChatApiError}=await import('/sdk/index.js');
   const client=new ChatClient({baseUrl:'/api'});
   const chat=await client.createChat({title:'Independent SDK E2E'});
   const events=[];let run='';
   const source=client.send({chat_id:chat.chat_id,message:'SDK resumable acceptance'});
   for await(const event of source){events.push(event);if(event.type==='run_started'){run=String(event.run_id);break;}}
   if(!run)throw Error('No run ID');
   const resumed=[];for await(const event of client.resume(run,0))resumed.push(event);
   const terminal=[];for await(const event of client.subscribe(run))terminal.push(event);
   const saved=await client.messages(chat.chat_id);
   const upload=await client.upload(new File(['SDK real upload'],'sdk-real-'+nonce+'.txt',{type:'text/plain'}),chat.chat_id);
   const cancelled=await client.createChat({title:'SDK cancellation'});
   let cancelRun='';
   for await(const event of client.send({chat_id:cancelled.chat_id,message:'Please keep working'})){
    if(event.type==='run_started'){cancelRun=String(event.run_id);await client.cancel(cancelRun);break;}
   }
   const missing=await client.confirmFile(chat.chat_id,'expired-e2e','deny');
   const answer=await client.answerQuestion(chat.chat_id,'expired-e2e',[{id:'q1',selected:[],custom:'Example'}]);
   const anonymous=new ChatClient({baseUrl:'/api',credentials:'omit'});let unauthorized=false;
   try{await anonymous.chats();}catch(e){unauthorized=e instanceof ChatApiError && e.status===401;}
   return {run,reply:saved.items.find(item=>item.role==='assistant')?.content,terminal:terminal.some(event=>event.type==='run_snapshot' && event.state.terminal===true),upload:upload.name,cancelRun,missing,answer,unauthorized,resumed:resumed.length};
  },stamp);
  assert.equal(outcome.reply,'端到端测试已完成。附件内容可以在预览中核对。');
  assert.equal(outcome.terminal,true);assert.equal(outcome.upload,'sdk-real-'+stamp+'.txt');assert.ok(outcome.cancelRun);
  assert.equal(outcome.unauthorized,true);assert.equal(outcome.missing.stale,true);assert.equal(outcome.answer.stale,true);
 });
 await check('independent frontend, real user-question tool and attachment isolation',async()=>{
  await page.goto(base+'/customer/');
  await page.getByRole('textbox',{name:'Message',exact:true}).fill('Independent frontend acceptance');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.locator('#status').filter({hasText:'Complete'}).waitFor({timeout:120000});
  await page.locator('[data-role="assistant"]').filter({hasText:'端到端测试已完成。附件内容可以在预览中核对。'}).last().waitFor();
  await page.getByRole('textbox',{name:'Message',exact:true}).fill('[CUSTOMIZATION_QUESTION]');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.getByRole('radio',{name:'Continue',exact:true}).check({timeout:60000});
  await page.getByRole('button',{name:'Submit answers',exact:true}).click();
  await page.locator('#status').filter({hasText:'Complete'}).waitFor({timeout:120000});
  assert.equal(await page.locator('#controls [data-request]').count(),0);
  console.log('PASS independent UI real model tool → suspended question → human answer → resumed reply');
  const url=page.url();await page.reload();assert.equal(page.url(),url);
  await page.locator('[data-role="assistant"]').filter({hasText:'端到端测试已完成。附件内容可以在预览中核对。'}).last().waitFor();
  await page.locator('#file').waitFor();
  await page.waitForFunction(()=>!document.getElementById('file').disabled);
  await page.locator('#file').setInputFiles({name:'independent-'+stamp+'.txt',mimeType:'text/plain',buffer:Buffer.from('Independent upload')});
  await page.locator('#attachment').filter({hasText:'independent-'+stamp+'.txt'}).waitFor();
  await page.getByRole('button',{name:'New chat',exact:true}).click();
  assert.equal(await page.locator('#attachment').innerText(),'');
  await page.screenshot({path:resolve(output,kind+'-independent.png'),fullPage:true});
 });
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,kind+'-results.json'),JSON.stringify({kind,passed,errors},null,2));
}catch(error){await page.screenshot({path:resolve(output,kind+'-failure.png'),fullPage:true});console.error('UI', (await page.locator('body').innerText()).slice(-2000),'ERRORS',errors);throw error;}
finally{await browser.close();}
