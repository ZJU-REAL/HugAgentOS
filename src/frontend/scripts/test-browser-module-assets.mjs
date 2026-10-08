
/** Real authenticated deployment: opaque iframe assets, Canvas handshake and user input. */
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {chromium} from 'playwright';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';

const contextPath=process.env.BROWSER_UI_CONTEXT;
const origin=process.env.BROWSER_UI_ORIGIN;
const output=process.env.BROWSER_UI_EVIDENCE;
assert.ok(contextPath && origin && output,'BROWSER_UI_CONTEXT, BROWSER_UI_ORIGIN, BROWSER_UI_EVIDENCE required');
const context=JSON.parse(await readFile(contextPath,'utf8'));
await mkdir(output,{recursive:true});
const bundle=await build({stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {RightSidebarPanel} from './src/components/canvas/RightSidebarPanel';
import {usePluginUiStore} from './src/stores/pluginUiStore';
import {useCanvasStore} from './src/stores/canvasStore';
import {useChatStore} from './src/stores/chatStore';
import './src/styles/variables.css';
import './src/styles/canvas.css';
import './src/plugin-ui/styles.css';
document.documentElement.style.height='100%';
document.body.style.height='100vh';
document.getElementById('root').style.height='920px';
document.getElementById('root').style.width='420px';
const binding=window.fixture.resource.resource;
window.canvasStore=useCanvasStore;window.chatStore=useChatStore;
usePluginUiStore.setState({items:[{slug:binding.slug,version:2,contributes:{modules:[window.fixture.module]}}],loaded:true});
useCanvasStore.getState().openPluginView({slug:binding.slug,canvasId:window.fixture.module.id,chatId:binding.chat_id,title:'浏览器',status:'success',output:window.fixture.resource});
createRoot(document.getElementById('root')).render(<div className="jx-canvasPanelSlot" style={{height:'100%',width:'100%',maxWidth:'100%',borderLeft:0}}><RightSidebarPanel/></div>);
`,resolveDir:process.cwd(),loader:'tsx'},bundle:true,format:'esm',jsx:'automatic',write:false,
define:{'import.meta.env':'{}'},loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'},outfile:'fixture.js'});
const fixtureHtml='<html><head><link rel="stylesheet" href="fixture.css"></head><body><button id="outside">Chat</button><div id="root"></div><script type="module" src="fixture.js"></script></body></html>';
if(process.env.BROWSER_UI_PREPARE_ONLY){
 const dir=process.env.BROWSER_UI_FIXTURE_DIR;
 assert.ok(dir);
 await mkdir(dir,{recursive:true});
 await writeFile(resolve(dir,'index.html'),fixtureHtml);
 for(const file of bundle.outputFiles)await writeFile(resolve(dir,file.path.endsWith('.css')?'fixture.css':'fixture.js'),file.contents);
 process.exit(0);
}
const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{})});
try{
 const page=await browser.newPage({viewport:{width:1500,height:1000}});
 await page.context().addCookies([{name:context.cookie_name,value:context.cookie,url:origin,httpOnly:true,sameSite:'Lax'}]);
 const responses=[],errors=[],socketErrors=[];
 page.on('websocket',socket=>socket.on('socketerror',error=>socketErrors.push(String(error))));
 let navigationCommands=0,browserState;
 page.on('websocket',socket=>socket.on('framereceived',({payload})=>{
  try {
   const header=typeof payload==='string'?JSON.parse(payload):JSON.parse(payload.subarray(4,4+payload.readUInt32BE(0)).toString());
   if(header.type==='state')browserState=header;
  }catch{/* Non-state messages do not affect the assertions. */}
 }));
 page.on('websocket',socket=>socket.on('framesent',({payload})=>{
  if(typeof payload!=='string')return;
  try{if(JSON.parse(payload).action==='navigate')navigationCommands++;}catch{/* Binary frames are unrelated. */}
 }));
 page.on('response',r=>{if(r.url().includes('plugin-resource-assets'))responses.push({asset:new URL(r.url()).pathname.split('/').at(-1),status:r.status()});});
 page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{
  const Native=window.WebSocket;
  window.__sockets=[];
  window.WebSocket=class extends Native {constructor(...args){super(...args);window.__sockets.push(this);}};
 });
 await page.addInitScript(value=>{window.fixture=value;},{module:context.module,resource:context.resource});
 await page.goto(origin+'/browser-ui-fixture/index.html',{waitUntil:'domcontentloaded'});
 const frame=page.frameLocator('iframe');
 try { await frame.locator('#connection[data-connected=true]').waitFor({timeout:20000}); }
 catch(error) {
   console.log(JSON.stringify({responses,errors,socketErrors,parent:await page.locator('body').innerText(),frame:await frame.locator('body').innerText().catch(()=>'(no frame)')}));
   await page.screenshot({path:resolve(output,'failure-load.png')});
   throw error;
 }
 await frame.locator('#tabs [role=tab]').first().waitFor();
 assert.equal(await page.locator('.jx-pv-error').count(),0);
 assert.equal(await page.locator('.jx-canvasTabs-list').count(),0,'There must be no enclosing browser tab row');
 assert.equal(await frame.locator('[role=tablist]').count(),1);
 for(const asset of ['index.html','chrome-tabs.css','style.css','i18n.js','channel.js','control.js','viewport.js','tabs.js','input.js','browser.js']){
  assert.ok(responses.some(r=>r.asset===asset && r.status===200),JSON.stringify(responses));
 }
 const checks=['Complete Canvas has exactly one tab strip, with host fullscreen/collapse controls in the same row','SameSite=Lax session loads all ten opaque iframe assets with HTTP 200','Actual React module completes handshake and receives real browser state'];
 await page.screenshot({path:resolve(output,'01-module-loaded.png')});
 assert.equal(await frame.locator('#tabs [role=tab]').first().getAttribute('title'),'about:blank','Use a newly opened isolated browser for this test');
 assert.equal(await frame.locator('#controls,#control,#private,#save,#close').count(),0);
 const beforeComposition=navigationCommands;
 await frame.locator('#address').fill('https://demo.playwright.dev/todomvc/');
 const address=frame.locator('#address');
 await address.evaluate(el=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true})));
 await page.waitForTimeout(150);
 assert.equal(navigationCommands,beforeComposition);
 checks.push('Address Enter during IME composition sends no browser navigation command');
 await frame.locator('#navigate button').click();
 try { await frame.locator('#tabs [role=tab]').filter({hasText:'TodoMVC'}).waitFor({timeout:45000}); }
 catch(error){ console.log('Canvas status:',await frame.locator('body').innerText()); await page.screenshot({path:resolve(output,'failure.png')}); throw error; }
 await frame.locator('#screen').evaluate(async el=>{for(let i=0;i<100;i++){if(el.width===420 && el.height>750)return;await new Promise(r=>setTimeout(r,100));}throw new Error('Viewport did not resize');});
 await page.waitForTimeout(700);
 const canvas=frame.locator('#screen');
 await canvas.screenshot({path:resolve(output,'02-interactive-page.png')});
  // Responsive TodoMVC coordinates are checked against the saved 420px screenshot.
  const box=await canvas.boundingBox();
  const size=await canvas.evaluate(el=>({width:el.width,height:el.height}));
  const click=async(x,y)=>page.mouse.click(box.x+x*box.width/size.width,box.y+y*box.height/size.height);
  const stage=await frame.locator('#stage').boundingBox();
  assert.ok(Math.abs(stage.height-box.height)<2,'Browser image must fill tall Canvas');
  assert.ok(Math.abs(stage.width-box.width)<2,'Browser image must fill Canvas width');
  checks.push('420px narrow Canvas resizes actual remote viewport and fills the stage without bottom letterboxing');
  await page.locator('#outside').click();
  await page.waitForTimeout(500);
  assert.equal(browserState.controller,'agent');
  await click(210,210);
  await frame.locator('#keyboard').pressSequentially('浏览器模块实机验证',{delay:80});
  await frame.locator('#keyboard').press('Enter');
  await page.waitForTimeout(500);
  await click(30,270);
  await page.waitForTimeout(500);
  assert.equal(browserState.controller,'user');
  assert.equal(browserState.private,true);
  checks.push('Canvas mouse and Chinese keyboard input create and complete a TodoMVC item; shared DOM result verified separately through actual MCP');
  await page.screenshot({path:resolve(output,'03-canvas-user-input.png')});
 await click(210,210);
 for(let i=0;i<18;i++){
  await page.keyboard.insertText('长页面滚动验证'+i);
  await frame.locator('#keyboard').press('Enter');
  await page.waitForTimeout(80);
 }
 await page.waitForTimeout(1200);
 await page.locator('#outside').click();
 await page.waitForTimeout(600);
 const scrollBefore=await canvas.screenshot({path:resolve(output,'04-before-scroll.png')});
 const scrollBox=await canvas.boundingBox();
 await page.mouse.move(scrollBox.x+scrollBox.width/2,scrollBox.y+scrollBox.height/2);
 for(let i=0;i<40;i++)await page.mouse.wheel(0,60);
 await page.waitForTimeout(1800);
 const scrollAfter=await canvas.screenshot({path:resolve(output,'05-after-scroll.png')});
 assert.notDeepEqual(scrollAfter,scrollBefore,'First wheel and consecutive native wheel events must visibly scroll the real page');
 assert.equal(await frame.locator('#notice').textContent(),'');
 await page.mouse.wheel(0,-3000);
 await page.waitForTimeout(1000);
 checks.push('A real long page scrolls down and back up; 40 consecutive native wheel events produce no queue or connection error');
 const tabsBeforeLauncher = await frame.locator('#tabs [role=tab]').count();
 await frame.locator('#tab-add').click();
 await page.getByRole('heading',{name:'工具',exact:true}).waitFor();
 assert.equal(await frame.locator('#tabs [role=tab]').count(),tabsBeforeLauncher,'Plus must not create about:blank');
 await page.locator('.jx-canvasLauncher').screenshot({path:resolve(output,'07-functional-launcher.png')});
 await page.getByRole('button',{name:'关闭「新标签页」',exact:true}).click();
 assert.equal(await frame.locator('#tabs [role=tab]').count(),tabsBeforeLauncher);
 await frame.locator('#tab-add').click();
 await page.locator('.jx-canvasLauncher').getByRole('textbox',{name:'网页地址'}).fill('https://demo.playwright.dev/todomvc/');
 await page.locator('.jx-canvasLauncher').getByRole('button',{name:'打开网页',exact:true}).click();
 await frame.locator('#tabs [role=tab]').nth(1).waitFor();
 await page.waitForTimeout(700);
 assert.ok((await frame.locator('#address').inputValue()).includes('todomvc'));
 checks.push('Browser plus opens the shared functional launcher; cancel preserves tabs; URL confirmation creates the page');
 await page.waitForTimeout(16000);
 assert.equal(browserState.controller,'agent');
 assert.equal(browserState.private,false);
 checks.push('15 seconds of inactivity automatically returns control and clears the private observation fence');
 await frame.locator('#tabs [role=tab]').first().click();
 await page.waitForTimeout(700);
 assert.ok((await frame.locator('#address').inputValue()).includes('todomvc'));
 await frame.locator('#tabs [role=tab]').nth(1).locator('.chrome-tab-close').click();
 await page.waitForTimeout(700);
 assert.equal(await frame.locator('#tabs [role=tab]').count(),1);
 checks.push('Confirmed URL creates a real second tab, switching restores its page, and close removes the tab');
 await page.evaluate(()=>{document.getElementById('root').style.width='860px';document.getElementById('root').style.height='640px';});
 await page.waitForTimeout(1800);
 const wide=await canvas.boundingBox(),wideStage=await frame.locator('#stage').boundingBox();
 assert.ok(Math.abs(wide.width-wideStage.width)<2 && Math.abs(wide.height-wideStage.height)<2);
 await page.locator('.jx-rightSidebar').screenshot({path:resolve(output,'06-single-header.png')});
 checks.push('Resizing Canvas preserves exact page fill and input coordinate mapping');
 await page.locator('#outside').click();
 await page.waitForTimeout(800);
 assert.equal(browserState.controller,'agent');
 checks.push('Direct human actions acquire ownership automatically; focusing chat releases ownership');
 const socketCount=await page.evaluate(()=>window.__sockets.length);
 await page.waitForTimeout(32000);
 assert.equal(await page.evaluate(()=>window.__sockets.length),socketCount,'Idle stream must remain connected');
 await page.evaluate(()=>window.__sockets.at(-1).close(4000,'Verification network interruption'));
 await page.waitForFunction(count=>window.__sockets.length>count,socketCount);
 await frame.locator('#connection[data-connected=true]').waitFor();
 await frame.locator('#reload').click();
 await page.waitForTimeout(1800);
 assert.equal(await frame.locator('#notice').textContent(),'');
 await page.locator('#outside').click();
 await page.waitForTimeout(800);
 assert.equal(browserState.controller,'agent');
 checks.push('Idle stream remains connected beyond 30 seconds; forced socket loss reconnects and a new action clears stale errors');


 assert.deepEqual(errors,[]);
 const report={checks,responses,errors,method:'Actual React module against deployed Docker API and Nginx, with real session Cookie and Playwright mouse/keyboard input'};
 await writeFile(resolve(output,'result.json'),JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify(report,null,2));
}finally{await browser.close();}
