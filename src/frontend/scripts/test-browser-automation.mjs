import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const backend = process.env.BROWSER_E2E_BACKEND;
assert.ok(backend, 'BROWSER_E2E_BACKEND required');
const output = resolve('node_modules/.tmp/browser-automation');
await mkdir(output, {recursive:true});
await build({
 stdin:{contents:`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {RightSidebarPanel} from './src/components/canvas/RightSidebarPanel';
import {useChatStore} from './src/stores/chatStore';
import {processChatStream} from './src/hooks/chatStream';
import {usePluginUiStore} from './src/stores/pluginUiStore';
import {useCanvasStore} from './src/stores/canvasStore';
window.boundCanvasSource = () => useCanvasStore.getState().pluginTarget?.slug;
import {bindCanvasResourceSource,runForCurrentCanvas} from './src/components/canvas/canvasResourceSource';
window.checkDelayedCanvasSource = resource => {
 const store=useCanvasStore.getState();
 const oldId=store.activeTabId, oldTarget=store.pluginTarget;
 const other={...resource,resource_id:'different-resource',slug:'different-source',install_id:'plugin:local:different-source'};
 store.openPluginView({slug:other.slug,canvasId:other.module_id,status:'success',resource:other});
 const before=useCanvasStore.getState().activeTabId;
 bindCanvasResourceSource(resource);
 let staleRan=false;
 runForCurrentCanvas(oldId,oldTarget,()=>{staleRan=true;});
 const kept=useCanvasStore.getState().activeTabId===before && useCanvasStore.getState().pluginTarget.slug===other.slug && !staleRan;
 useCanvasStore.getState().closeTab(before);
 return kept;
};
import './src/styles/variables.css';
import './src/styles/canvas.css';
import './src/plugin-ui/styles.css';
window.showBrowser = async payload => {
 await usePluginUiStore.getState().fetchContributions(true);
 useChatStore.setState({currentChatId:'browser-chat'});
 const events=[
  {type:'tool_start',id:'browser-open-e2e',name:'browser_open',input:{}},
  {type:'tool_result',id:'browser-open-e2e',name:'browser_open',output:payload},
  {type:'end'}
 ];
 await processChatStream(new Response(events.map(e=>'data: '+JSON.stringify(e)+'\\n\\n').join('')),{chatId:'browser-chat',enableThinking:false});

};
document.documentElement.style.height='100%';document.body.style.height='950px';document.getElementById('root').style.height='100%';
createRoot(document.getElementById('root')).render(<RightSidebarPanel/>);
`,resolveDir:process.cwd(),loader:'tsx'},
 outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',
 define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN"}'},
 loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}
});
await writeFile(resolve(output,"index.html"),'<html><head><link rel="stylesheet" href="/fixture.css"></head><body><button id="outside">Chat</button><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
if(process.argv.includes("--prepare")) process.exit(0);
async function tool(name,body){
 const response=await fetch(backend+'/test/tool/'+name,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
 const text=await response.text();
 assert.equal(response.status,200,text);
 return JSON.parse(text);
}
const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{})});
const checks=[]; const errors=[];
try{
 const confined=await fetch(backend+'/test/confinement',{method:'POST'});
 assert.equal(confined.status,200,await confined.clone().text());
 assert.deepEqual(await confined.json(),{private_read_blocked:true,workspace_write:true});
 checks.push('Default auto permission launches the real OS sandbox: private credential probe unreadable; session workspace writable');
 const page=await browser.newPage({viewport:{width:1500,height:1000}});
 await page.addInitScript(()=>{const Native=window.WebSocket;window.__sockets=[];window.WebSocket=class extends Native{constructor(...args){super(...args);window.__sockets.push(this);}};});
 page.on('pageerror',error=>errors.push(error.message));
 page.on('response',async response=>{ if(response.status()>=400) console.log('Fixture HTTP failure',response.status(),new URL(response.url()).pathname,await response.text()); });
 page.on('websocket', socket=>socket.on('socketerror',message=>console.error('WS error',message)));
 page.on('requestfailed',request=>console.error('Request failed',request.url(),request.failure(), {referer:request.headers().referer,site:request.headers()['sec-fetch-site']}));
 await page.goto(process.env.BROWSER_E2E_FRONTEND || backend);
 const proofProbe=await fetch(backend+'/test/cloud-proof',{method:'POST'});
 assert.equal(proofProbe.status,200,await proofProbe.text());
 checks.push('Real cloud gateway replaces a different-key device proof; native HTTP request hook and internal resource callback verify it');
 const opened=await tool('browser_open',{});
 const id=opened.resource.resource_id;
 assert.ok(id);

 await page.evaluate(value=>window.showBrowser(value),opened);
 const frame=page.frameLocator('iframe');
 try { await frame.locator('#screen').waitFor(); } catch(error) { console.log('Browser fixture output',JSON.stringify(opened)); console.log(await page.locator('body').innerText()); console.log(await page.locator('iframe').evaluateAll(nodes=>nodes.map(n=>n.src))); throw error; }
 try{await frame.locator('#tabs [role=tab]').first().waitFor();}catch(error){await page.screenshot({path:resolve(output,'failure.png')});console.log(await page.locator('body').innerText());throw error;}
 assert.equal(await page.evaluate(()=>window.boundCanvasSource()),opened.resource.slug);
 checks.push('Same-name Canvas contributions bind to the exact modern local installation returned by the tool');
 assert.equal(await page.evaluate(resource=>window.checkDelayedCanvasSource(resource),opened.resource),true);
 checks.push('Delayed source binding and old module callbacks cannot change another active resource tab or steal its focus');
 await frame.locator('#tabs [role=tab]').first().waitFor();
 await frame.locator('#overlay').filter({hasText:'输入网址或搜索内容，按 Enter 打开'}).waitFor();
 await frame.locator('#tab-add').click();
 await page.getByRole('textbox',{name:'网页地址',exact:true}).fill(backend+'/test/site');
 await page.getByRole('button',{name:'打开网页',exact:true}).click();
 await page.locator('.jx-canvasLauncher').waitFor({state:'detached'});
 await frame.locator('#screen').waitFor();
 await frame.locator('#overlay').waitFor({state:'hidden'});
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 const initialState=await tool('browser_observe',{resource_id:id,action:'state'});
 assert.equal(initialState.tabs.length,1,'The initial blank tab must be reused');
 assert.equal(initialState.tabs[0].url,backend+'/test/site');
 checks.push('First browser start shows an address hint; host new-tab address opens a real page without leaving an extra blank tab');
 assert.equal(await page.locator('.jx-canvasTabs-list').count(),0);
 assert.equal(await frame.locator('[role=tablist]').count(),1);
 checks.push('Complete Canvas renders one browser tab strip and preserves panel actions');
 await page.screenshot({path:resolve(output,'01-agent-browser.png')});
 await tool('browser_action',{resource_id:id,action:'fill',params:{selector:'#name',text:'工具自动填写'}});
 await tool('browser_action',{resource_id:id,action:'click',params:{selector:'#submit'}});
 assert.equal((await tool('browser_observe',{resource_id:id,action:'text',selector:'#result'})).text,'工具自动填写');
 checks.push('Actual chat SSE auto-opens Canvas; modern owned package → catalog allowlist → actual make_client → native MCP → backend → managed worker → Chromium DOM operations');

 // Fixed site fixture: input center is (200,45) in remote viewport coordinates.
 const canvas=frame.locator('#screen');
 const box=await canvas.boundingBox();
 const size=await canvas.evaluate(el=>({width:el.width,height:el.height}));
 await page.mouse.click(box.x+200*box.width/size.width,box.y+45*box.height/size.height);
 await page.waitForTimeout(500);
 const denied=await fetch(backend+'/test/tool/browser_observe',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({resource_id:id})});
 assert.equal(denied.status,409);
 checks.push('Agent observation fenced during private manual control');
 await frame.locator('#keyboard').press('Control+A');
 await frame.locator('#keyboard').pressSequentially('人工中文输入');
 await page.waitForTimeout(300);
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 assert.equal((await tool('browser_observe',{resource_id:id,action:'text',selector:'#result'})).text,'工具自动填写');
 await tool('browser_action',{resource_id:id,action:'click',params:{selector:'#submit'}});
 const after=await tool('browser_observe',{resource_id:id,action:'snapshot'});
 assert.ok(after.snapshot.includes('人工中文输入'),JSON.stringify(after));
 checks.push('Canvas mouse coordinates and Unicode keyboard input reach the same Chromium session');
 await page.screenshot({path:resolve(output,'02-manual-browser.png')});

 // Wheel must target the hovered remote element, without a preliminary page click.
 const scrollBox=await canvas.boundingBox();
 const scrollSize=await canvas.evaluate(el=>({width:el.width,height:el.height}));
 await page.mouse.move(scrollBox.x+300*scrollBox.width/scrollSize.width,scrollBox.y+350*scrollBox.height/scrollSize.height);
 await page.mouse.wheel(0,450);
 await page.waitForTimeout(600);
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 assert.ok(Number((await tool('browser_observe',{resource_id:id,action:'text',selector:'#scroll-result'})).text)>0);
 checks.push('First wheel gesture targets and scrolls the hovered nested container');


 const previousScroll=Number((await tool('browser_observe',{resource_id:id,action:'text',selector:'#scroll-result'})).text);
 await page.mouse.click(scrollBox.x+300*scrollBox.width/scrollSize.width,scrollBox.y+350*scrollBox.height/scrollSize.height);
 await page.mouse.wheel(0,250);
 await page.waitForTimeout(600);
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 assert.ok(Number((await tool('browser_observe',{resource_id:id,action:'text',selector:'#scroll-result'})).text)>previousScroll);
 checks.push('Wheel at the exact previous click position reaches the nested remote scroll container');


 const beforeTouch=Number((await tool('browser_observe',{resource_id:id,action:'text',selector:'#scroll-result'})).text);
 const touchCdp=await page.context().newCDPSession(page);
 await touchCdp.send('Emulation.setTouchEmulationEnabled',{enabled:true});
 const tx=scrollBox.x+300*scrollBox.width/scrollSize.width;
 const ty=scrollBox.y+400*scrollBox.height/scrollSize.height;
 await touchCdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:tx,y:ty,id:1}]});
 await touchCdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:tx,y:ty-60,id:1}]});
 await touchCdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await page.waitForTimeout(600);
 await touchCdp.send('Emulation.setTouchEmulationEnabled',{enabled:false});
 await touchCdp.detach();
 await page.locator('#outside').click();await page.waitForTimeout(300);
 assert.ok(Number((await tool('browser_observe',{resource_id:id,action:'text',selector:'#scroll-result'})).text)>beforeTouch);
 checks.push('Touch drag scrolls the remote nested container without mouse drag selection');

 const baseWidth=await canvas.evaluate(el=>el.width);
 await frame.locator('#address').focus();
 await page.keyboard.press('Control+=');
 await frame.locator('#zoom-reset').filter({hasText:'110%'}).waitFor();
 await page.waitForFunction(()=>document.querySelector('iframe')!==null);
 await page.waitForTimeout(500);
 assert.ok(await canvas.evaluate(el=>el.width)<baseWidth);
 await page.keyboard.press('Control+0');
 await frame.locator('#zoom-reset').filter({hasText:'100%'}).waitFor();
 await page.waitForTimeout(500);
 assert.equal(await canvas.evaluate(el=>el.width),baseWidth);
 await page.locator('#outside').click();await page.waitForTimeout(300);
 checks.push('Ctrl plus reflows the remote viewport and Ctrl zero restores 100 percent');

 // A dialog must be answerable while the triggering click is still pending.
 const dialogClick=tool('browser_action',{resource_id:id,action:'click',params:{selector:'#dialog'}});
 await frame.locator('#dialog-message').filter({hasText:'请确认'}).waitFor();

 await frame.locator('#dialog-accept').click();
 await dialogClick;
 await frame.locator('#dialog').waitFor({state:'hidden'});
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 checks.push('Live JavaScript dialog handled without blocking the operation lock');
 await tool('browser_action',{resource_id:id,action:'click',params:{selector:'#upload'}});
 await frame.locator('#files button').filter({hasText:'选择上传文件'}).waitFor();

 const chooser=page.waitForEvent('filechooser');
 await frame.locator('#files button').filter({hasText:'选择上传文件'}).click();
 await (await chooser).setFiles({name:'中文.txt',mimeType:'text/plain',buffer:Buffer.from('实机上传')});
 await page.waitForTimeout(300);
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 assert.equal((await tool('browser_observe',{resource_id:id,action:'text',selector:'#filename'})).text,'中文.txt');
 checks.push('Canvas upload picker transfers real file bytes into Chromium');
 await tool('browser_action',{resource_id:id,action:'click',params:{selector:'#download'}});
 let state=await tool('browser_observe',{resource_id:id,action:'state'});
 const download=state.downloads.at(-1);
 assert.ok(download);
 const artifact=await tool('browser_action',{resource_id:id,action:'retain_download',params:{download_id:download.id}});
 assert.ok(artifact.file_id);
 checks.push('Browser download retained in the existing artifact store');
 // Close the real host WebSocket and wait for a fresh ticket/connection.

 await page.evaluate(()=>window.__sockets.at(-1).close(4000,'E2E network interruption'));
 await page.waitForFunction(()=>window.__sockets.length>=2);
 await frame.locator('#connection[data-connected=true]').waitFor();
 await page.mouse.click(box.x+200*box.width/size.width,box.y+45*box.height/size.height);

 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 assert.equal((await tool('browser_observe',{resource_id:id,action:'text',selector:'#result'})).text,'人工中文输入');
 assert.equal(await frame.locator('#notice').textContent(),'');
 checks.push('Desktop proxy carries HTTP assets and binary WebSocket frames; socket loss reconnects to the same browser and requires a new control lease');
 await tool('browser_action',{resource_id:id,action:'new_tab',params:{url:backend+'/test/site'}});
 assert.equal((await tool('browser_observe',{resource_id:id,action:'state'})).tabs.length,2);
 checks.push('Multiple tabs share session and Canvas state');
 await tool('browser_action',{resource_id:id,action:'click',params:{selector:'#newtab'}});
 for(let attempt=0;attempt<50;attempt++) {
  state=await tool('browser_observe',{resource_id:id,action:'state'});
  if(state.tabs.length===3 && state.active_tab==='3')break;
  await page.waitForTimeout(50);
 }
 assert.equal(state.active_tab,'3');
 assert.equal(state.tabs.length,3);
 await frame.locator('#overlay').waitFor({state:'hidden'});
 assert.equal(await frame.locator('#address').inputValue(),backend+'/test/cookie');
 assert.equal(await frame.locator('#screen').getAttribute('data-tab'),'3');
 await page.screenshot({path:resolve(output,'03-popup-selected.png')});
 checks.push('Target-blank link automatically selects its new page, updates the address and renders its live frame');
 await frame.locator('[role=tab]').nth(1).locator('.chrome-tab-close').click();
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 state=await tool('browser_observe',{resource_id:id,action:'state'});
 assert.equal(state.active_tab,'3');
 assert.equal(state.tabs.length,2);
 await frame.locator('[role=tab][active] .chrome-tab-close').click();
 await page.locator('#outside').click();
 await page.waitForTimeout(300);
 state=await tool('browser_observe',{resource_id:id,action:'state'});
 assert.equal(state.active_tab,'1');
 assert.equal(state.tabs.length,1);
 await frame.locator('#overlay').waitFor({state:'hidden'});
 assert.equal(await frame.locator('#address').inputValue(),backend+'/test/site');
 assert.equal(await frame.locator('#screen').getAttribute('data-tab'),'1');
 checks.push('Canvas close buttons preserve selection for background tabs and restore the adjacent page/frame');
 for(const [action,params,url] of [
  ['navigate',{url:backend+'/test/cookie'},backend+'/test/cookie'],
  ['back',{},backend+'/test/site'],['forward',{},backend+'/test/cookie'],
  ['back',{},backend+'/test/site'],['reload',{},backend+'/test/site']
 ]) {
  await tool('browser_action',{resource_id:id,action,params});
  assert.equal((await tool('browser_observe',{resource_id:id,action:'state'})).tabs[0].url,url);
 }
 checks.push('Back, forward and reload operate on the selected surviving page');
 await tool('browser_close',{resource_id:id});
 const retained=await fetch(backend+artifact.url);
 assert.equal(retained.status,200,await retained.clone().text());
 assert.equal(await retained.text(),'browser-download');
 const closed=await fetch(backend+'/api/v1/plugin-resources/'+id);
 assert.equal(closed.status,410);
 checks.push('Explicit close terminates browser access while retained downloads remain available');
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'report.json'),JSON.stringify({checks,errors,scope:'WSL Chromium, real FastMCP HTTP, actual React Canvas and MessagePort/WebSocket bridge'},null,2));
 console.log(JSON.stringify({checks,output},null,2));
}finally{await browser.close();}
