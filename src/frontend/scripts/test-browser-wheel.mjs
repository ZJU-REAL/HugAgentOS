
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const root='../backend/plugin_bundles/marketplace/browser-automation/web/browser/';
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try {
 const page=await browser.newPage({hasTouch:true});
 await page.setContent('<div id="stage" style="height:500px;width:800px"><canvas id="screen" width="800" height="500"></canvas><textarea id="keyboard"></textarea></div>');
 await page.addStyleTag({content:await readFile(root+'style.css','utf8')});
 await page.addScriptTag({content:await readFile(root+'touch.js','utf8')});
 await page.addScriptTag({content:await readFile(root+'input.js','utf8')});
 await page.evaluate(()=>{
  window.calls=[];window.inputState={active_tab:'1',viewport_revision:1};
  const screen=document.getElementById('screen');screen.dataset.revision='1';screen.dataset.tab='1';
  window.installBrowserInput(screen,document.getElementById('keyboard'),()=>window.inputState,
    {owns:()=>true,hold(){},command:async(action,params)=>{window.calls.push(params);}});
 });
 await page.mouse.click(200,200);
 await page.mouse.wheel(0,300);
 await page.waitForTimeout(150);
 assert.ok(await page.evaluate(()=>window.calls.some(c=>c.kind==='wheel'&&c.dy===300)),'Wheel at the exact previous click position must reach the canvas');
 await page.locator('#keyboard').pressSequentially('中文');
 assert.ok(await page.evaluate(()=>window.calls.some(c=>c.kind==='text')),'Keyboard remains focusable');

 for(const mode of [1,2]){
  const expected=await page.evaluate(mode=>{
   window.calls=[];const screen=document.getElementById('screen'),box=screen.getBoundingClientRect();
   screen.dispatchEvent(new WheelEvent('wheel',{clientX:200,clientY:200,deltaY:2,deltaMode:mode,bubbles:true,cancelable:true}));
   return 2*(mode===1?parseFloat(getComputedStyle(screen).fontSize)*1.2:box.height)*screen.height/box.height;
  },mode);
  await page.waitForFunction(()=>window.calls.some(c=>c.kind==='wheel'));
  assert.equal(await page.evaluate(()=>window.calls.find(c=>c.kind==='wheel').dy),expected);
 }
 await page.evaluate(()=>{
  window.calls=[];const keyboard=document.getElementById('keyboard');
  keyboard.dispatchEvent(new CompositionEvent('compositionstart'));
  keyboard.value='中文输入';keyboard.dispatchEvent(new InputEvent('input',{isComposing:true}));
 });
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.kind==='text').length),0);
 await page.evaluate(()=>document.getElementById('keyboard').dispatchEvent(new CompositionEvent('compositionend')));
 await page.waitForFunction(()=>window.calls.some(c=>c.kind==='text'));
 assert.deepEqual(await page.evaluate(()=>window.calls.filter(c=>c.kind==='text').map(c=>c.text)),['中文输入']);

 const cdp=await page.context().newCDPSession(page);
 await page.evaluate(()=>{window.calls=[];});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:250,y:300,id:1}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:250,y:220,id:1}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await page.waitForFunction(()=>window.calls.some(c=>c.kind==='wheel'));
 assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.kind==='down'||c.kind==='up').length),0,'Touch drag must not start mouse selection');
 assert.ok(await page.evaluate(()=>window.calls.find(c=>c.kind==='wheel').dy>0));
 await page.evaluate(()=>{window.calls=[];});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:250,y:250,id:2}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await page.waitForFunction(()=>window.calls.some(c=>c.kind==='up'));
 assert.deepEqual(await page.evaluate(()=>window.calls.filter(c=>['down','up'].includes(c.kind)).map(c=>c.kind)),['down','up']);
 await page.evaluate(()=>{window.calls=[];});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:250,y:250,id:3}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]});
 assert.equal(await page.evaluate(()=>window.calls.length),0);
 await page.evaluate(()=>{window.calls=[];});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:250,y:250,id:4},{x:300,y:250,id:5}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 assert.equal(await page.evaluate(()=>window.calls.length),0,'Multitouch must not click');

 await page.evaluate(()=>{window.calls=[];});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:250,y:300,id:6}]});
 await page.evaluate(()=>{window.inputState={active_tab:'2',viewport_revision:2};const screen=document.getElementById('screen');screen.dataset.tab='2';screen.dataset.revision='2';});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:250,y:220,id:6}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 assert.equal(await page.evaluate(()=>window.calls.length),0,'Old touch cannot affect a new tab/revision');
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:250,y:250,id:7}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await page.waitForFunction(()=>window.calls.some(c=>c.kind==='up'));
 console.log('Click-then-wheel at identical coordinates and keyboard input passed');
} finally {await browser.close();}
