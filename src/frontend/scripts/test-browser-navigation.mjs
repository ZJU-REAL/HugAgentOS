import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const root='../backend/plugin_bundles/marketplace/browser-automation/web/browser/';
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try {
 const page=await browser.newPage();
 await page.route('https://browser.test/**', async route=>{
  const path=new URL(route.request().url()).pathname.slice(1)||'index.html';
  if(path==='channel.js') return route.fulfill({contentType:'text/javascript',body:`
window.calls=[];window.listener=null;
window.BrowserChannel={listen(fn){window.listener=fn;},epoch(){},connection(){return 'viewer';},
 async command(action,params){window.calls.push({action,params});if(action==='take_control')return {...window.state,controller:'user',connection_id:'viewer'};return {ok:true};},
 newTab:async()=>{},navigationResult(){}};
`});
  await route.fulfill({body:await readFile(root+path),contentType:path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'text/html'});
 });
 await page.goto('https://browser.test/');
 await page.evaluate(()=>{
 window.state={type:'state',tabs:[{id:'1',url:'about:blank',title:''}],active_tab:'1',controller:'agent',connection_id:'',epoch:0,viewport_revision:1,viewport_limits:{min_width:320,max_width:1600,min_height:200,max_height:1200},downloads:[],dialogs:[],filechoosers:[]};
 window.listener({type:'attached'});window.listener(window.state);
 });
 assert.equal(await page.locator('#overlay').textContent(),'输入网址或搜索内容，按 Enter 打开');
 assert.equal(await page.locator('.chrome-tab-title').textContent(),'新标签页');
 await page.locator('#address').fill(' example.com/path ');
 await page.locator('#address').press('Enter');
 await page.waitForFunction(()=>window.calls.some(c=>c.action==='navigate'));
 assert.equal(await page.evaluate(()=>window.calls.find(c=>c.action==='navigate').params.url),'https://example.com/path');
 await page.locator('#address').fill('javascript:alert(1)');
 await page.locator('#address').press('Enter');
 await page.waitForTimeout(100);
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='navigate').length),1);
 await page.evaluate(async()=>{
  window.calls=[];
  await window.listener({type:'navigate',id:'open',url:'https://example.com/'});
 });
 assert.equal(await page.evaluate(()=>window.calls.find(c=>c.action==='navigate').params.url),'https://example.com/');
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='new_tab').length),0,'Opening the first URL reuses the initial blank tab');
 await page.evaluate(async()=>{
  window.state={...window.state,tabs:[{id:'1',url:'https://example.com/',title:'Example'}]};
  window.listener(window.state);
  window.calls=[];
  await window.listener({type:'navigate',id:'second',url:'https://example.org/'});
 });
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='new_tab').length),1);
 assert.equal(await page.locator('#overlay').textContent(),'正在加载网页…','A changed tab must not expose the previous bitmap before its first frame');
 await page.waitForTimeout(250);
 await page.evaluate(()=>{
  window.calls=[];
  window.state={...window.state,dialogs:[{tab_id:'1',kind:'alert',message:'Confirm'}]};
  window.listener(window.state);
 });
 await page.setViewportSize({width:900,height:700});
 await page.waitForTimeout(250);
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='resize').length),0,'A modal dialog must not queue a blocking viewport screenshot');
 await page.evaluate(()=>{window.state={...window.state,dialogs:[]};window.listener(window.state);});
 await page.waitForFunction(()=>window.calls.some(c=>c.action==='resize'));

 // Background metadata must preserve an address draft, but a tab change replaces it.
 await page.locator('#address').fill('unfinished address');
 await page.evaluate(()=>window.listener({...window.state}));
 assert.equal(await page.locator('#address').inputValue(),'unfinished address');
 await page.evaluate(()=>{
  window.state={...window.state,tabs:[...window.state.tabs,{id:'2',url:'https://example.org/',title:'Second'}],active_tab:'2',viewport_revision:2};
  window.listener(window.state);
 });
 assert.equal(await page.locator('#address').inputValue(),'https://example.org/');
 assert.equal(await page.locator('[role=tab][aria-selected=true]').getAttribute('title'),'https://example.org/');
 await page.evaluate(()=>{
  window.state={...window.state,tabs:[...window.state.tabs,{id:'3',url:'about:blank',title:''}],active_tab:'3',viewport_revision:3};
  window.listener(window.state);
 });
 assert.equal(await page.locator('#address').inputValue(),'');
 assert.equal(await page.locator('#address').evaluate(el=>el===document.activeElement),true);
 await page.evaluate(()=>{window.state={...window.state,active_tab:'2',viewport_revision:4};window.listener(window.state);});

 await page.setViewportSize({width:420,height:700});
 await page.evaluate(()=>{
  window.state={...window.state,tabs:Array.from({length:12},(_,i)=>({id:String(i+1),url:'https://example.org/'+i,title:'Tab '+(i+1)})),active_tab:'12',viewport_revision:5};
  window.listener(window.state);
 });
 const visible=await page.locator('[role=tab][aria-selected=true]').evaluate(el=>{
  const box=el.getBoundingClientRect(),container=el.parentElement.getBoundingClientRect();
  return box.left>=container.left-.5 && box.right<=container.right+.5;
 });
 assert.equal(visible,true,'The newly active tab must remain visible in a crowded narrow strip');
 await page.setViewportSize({width:1280,height:720});

 const cases=[
 ['site:example.com email@example.org','https://www.bing.com/search?q='+encodeURIComponent('site:example.com email@example.org')],
 ['inurl:user@example.com','https://www.bing.com/search?q='+encodeURIComponent('inurl:user@example.com')],
 ['weather','https://www.bing.com/search?q=weather'],
 ['杭州 天气','https://www.bing.com/search?q='+encodeURIComponent('杭州 天气')],
 ['site:example.com hello','https://www.bing.com/search?q='+encodeURIComponent('site:example.com hello')],
 ['2+2 & café','https://www.bing.com/search?q='+encodeURIComponent('2+2 & café')],
 ['example.com/a','https://example.com/a'],
 ['localhost:3000','https://localhost:3000/'],
 ['127.0.0.1:8080/a','https://127.0.0.1:8080/a'],
 ['http://example.com/a?q=hello world','http://example.com/a?q=hello%20world'],
 ['https://','https://www.bing.com/search?q=https%3A%2F%2F'],
 ];
 for(const [input,expected] of cases){
  await page.evaluate(()=>{window.calls=[];});
  await page.locator('#address').fill(input);await page.locator('#address').press('Enter');
  await page.waitForFunction(()=>window.calls.some(c=>c.action==='navigate'));
  assert.equal(await page.evaluate(()=>window.calls.find(c=>c.action==='navigate').params.url),expected,input);
 }
 await page.evaluate(async()=>{window.calls=[];await window.listener({type:'navigate',id:'search',url:'中文搜索'});});
 assert.equal(await page.evaluate(()=>window.calls.find(c=>c.action==='new_tab').params.url),'https://www.bing.com/search?q='+encodeURIComponent('中文搜索'));
 for (const value of ['https://user:secret@','https://user:secret@bad host','user:secret@bad host','https://user:secret@example.com','data:text/html,test','file:///tmp/a']) {
  await page.evaluate(()=>{window.calls=[];});
  await page.locator('#address').fill(value);await page.locator('#address').press('Enter');
  await page.waitForTimeout(30);
  assert.equal(await page.evaluate(()=>window.calls.filter(c=>['navigate','new_tab'].includes(c.action)).length),0,value);
 }

 await page.locator('#address').focus();
 await page.keyboard.press('Control+=');
 assert.equal(await page.locator('#zoom-reset').textContent(),'110%');
 await page.waitForTimeout(200);
 const stageWidth=await page.locator('#stage').evaluate(el=>el.clientWidth);
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='resize').at(-1).params.width),Math.round(stageWidth/1.1));
 await page.keyboard.press('Control+-');
 assert.equal(await page.locator('#zoom-reset').textContent(),'100%');
 await page.keyboard.press('Control+=');
 await page.keyboard.press('Control+0');
 assert.equal(await page.locator('#zoom-reset').textContent(),'100%');
 await page.locator('#zoom-in').click();
 assert.equal(await page.locator('#zoom-reset').textContent(),'110%');
 await page.locator('#zoom-reset').click();
 assert.equal(await page.locator('#zoom-reset').textContent(),'100%');

 for(const size of [{width:1200,height:700},{width:420,height:900}]){
  await page.setViewportSize(size);
  await page.evaluate(async()=>{
   const stage=document.getElementById('stage');
   const width=stage.clientWidth,height=stage.clientHeight;
   window.state={...window.state,viewport_revision:window.state.viewport_revision+1};
   window.listener(window.state);
   const bitmap=document.createElement('canvas');bitmap.width=width;bitmap.height=height;
   const ctx=bitmap.getContext('2d');ctx.fillStyle='red';ctx.fillRect(0,0,width,height);
   const blob=await new Promise(resolve=>bitmap.toBlob(resolve,'image/jpeg'));
   await window.listener({type:'frame',tab_id:window.state.active_tab,viewport_revision:window.state.viewport_revision,viewport:{width,height}},new Uint8Array(await blob.arrayBuffer()));
  });
  const geometry=await page.evaluate(()=>{
   const stage=document.getElementById('stage').getBoundingClientRect(),screen=document.getElementById('screen').getBoundingClientRect();
   return {left:screen.left-stage.left,right:stage.right-screen.right,top:screen.top-stage.top,bottom:stage.bottom-screen.bottom};
  });
  assert.ok(Object.values(geometry).every(value=>value>=-.5),JSON.stringify(geometry));
 }
 console.log('Address entry normalizes bare URLs and rejects unsafe schemes');
} finally {await browser.close();}
