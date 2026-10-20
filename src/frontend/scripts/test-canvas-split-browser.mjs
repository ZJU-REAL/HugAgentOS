import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/canvas-split-browser');
await mkdir(output, { recursive: true });
await build({ plugins:[{name:'asset-url',setup(build){build.onResolve({filter:new RegExp('[?]url$')},args=>({path:args.path,namespace:'asset-url'}));build.onLoad({filter:/.*/,namespace:'asset-url'},()=>({contents:'export default "/mock-worklet.js"',loader:'js'}));}}], stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { InputArea } from './src/components/chat/InputArea';
import { RightSidebarPanel } from './src/components/canvas/RightSidebarPanel';
import { useCanvasStore } from './src/stores/canvasStore';

import { useModelCapabilitiesStore } from './src/stores/modelCapabilitiesStore';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
import './src/styles/myspace.css';
import './src/styles/canvas.css';
import './src/styles/mobile.css';

useModelCapabilitiesStore.setState(s => ({capabilities:{...s.capabilities,user_model_switch_enabled:true,user_selectable_models:[{provider_id:'long',display_name:'DeepSeek-V4.1-Flash-Long-Model-Name',is_default:true}]}}));
window.canvasStore=useCanvasStore;
const file={file_id:'report', name:'项目知识体系与人工智能技术标准体系_导读.txt',url:'/files/report',mime_type:'text/plain'};
function Fixture(){
 const open=useCanvasStore(s=>s.isOpen), full=useCanvasStore(s=>s.isFullscreen), width=useCanvasStore(s=>s.panelWidth);
 const inputRef=React.useRef(null), fileRef=React.useRef(null);
 return <><nav><button onClick={()=>useCanvasStore.getState().openCanvas(file)}>Preview</button><button onClick={()=>useCanvasStore.getState().closeCanvas()}>Close</button><button onClick={()=>document.documentElement.dataset.theme='dark'}>Dark</button></nav>
 <div className="shell"><aside className="navigation"><h2>项目</h2><p>产业链构建</p></aside>
 <main className={'jx-appMainLayout'+(full?' is-canvasFullscreen':'')} style={{display:'flex',height:'100%',overflow:'hidden','--jx-canvas-preferred-width':width ? width+'px' : undefined}}>
 <div className={'jx-primaryPane is-chatSurface'+(open?' is-canvasOpen':'')}>
 <header className="jx-chatTopbar"><span className="jx-chatTopbarTitle">产业链构建 / 总结文件夹</span></header>
 <div className="jx-mainRow"><div className="jx-content"><div className="jx-panel"><div className="jx-chatWrap">
 <div className="jx-msg assistant"><div className="jx-bubble jx-md"><p>已把文件发给你了，请查收对话区的附件。</p><p>项目原文件：<code>项目资料集/项目知识体系与人工智能技术标准体系_导读.pptx</code></p><p><a href="#">https://example.com/very-long-unbroken-project-file-name-with-more-than-one-hundred-characters-for-layout-checking.pptx</a></p><pre><code>const longLine = 'code keeps its own horizontal scroll';</code></pre></div></div>
 <div className="jx-chatFooter"><InputArea inputRef={inputRef} fileInputRef={fileRef} send={()=>{document.documentElement.dataset.sent='true'}} handleFileSelect={()=>{}} removeFile={()=>{}} /></div>
 </div></div></div></div></div>
 {open&&<div className={'jx-canvasPanelSlot'+(full?' is-fullscreen':'')} style={{display:'flex',flex:'none',height:'100%',minWidth:0}}><RightSidebarPanel/></div>}
 </main></div></>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}});

const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage({viewport:{width:1660,height:950}});
 page.setDefaultTimeout(10000);
 const errors=[], saves=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('https://layout.test/**',async route=>{
  const url=new URL(route.request().url()), path=url.pathname;
  if(path==='/fixture.js'||path==='/fixture.css') return route.fulfill({contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1)))});
  const text=path.match(new RegExp('/v1/file/([^/]+)/text$'));
  if(text) {
   let content='Content '+text[1];
   if(route.request().method()==='PUT') { const payload=route.request().postDataJSON();content=payload.content;saves.push(text[1]); }
   return route.fulfill({json:{code:0,data:{file_id:text[1],name:text[1]+'.txt',content,revision:'r1',size:content.length,can_edit:true}}});
  }
  if(path.startsWith('/api')) return route.fulfill({json:{code:0,data:[]}});
  if(path.endsWith('.svg')) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg"/>'});
  return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:15px Arial;color:var(--color-text);background:var(--color-bg-container)}nav{height:40px}.shell{display:flex;height:calc(100vh - 40px)}.navigation{width:220px;flex:none}.jx-appMainLayout{flex:1}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://layout.test/');
 await page.evaluate(()=>window.canvasStore.getState().openSidebar());
 await page.getByRole('heading',{name:'工具',exact:true}).waitFor();
 await page.getByRole('tab',{name:/新标签页/}).waitFor();
 await page.getByRole('button',{name:'全屏',exact:true}).click();
 await page.waitForFunction(()=>{
  const root=document.querySelector('.jx-canvasSplit').getBoundingClientRect();
  const launcher=document.querySelector('.jx-canvasLauncher').getBoundingClientRect();
  return root.width>1000 && Math.abs(root.right-launcher.right)<2 && Math.abs(root.left-launcher.left)<2;
 });
 await page.screenshot({path:resolve(output,'empty-fullscreen.png'),animations:'disabled'});
 await page.getByRole('button',{name:'关闭「新标签页」',exact:true}).click();
 await page.waitForFunction(()=>!window.canvasStore.getState().isOpen);
 const open=async id=>page.evaluate(id=>window.canvasStore.getState().openCanvas({file_id:id,name:id+'.txt',url:'/files/'+id,mime_type:'text/plain'}),id);
 await open('a'); await open('b'); await open('c');
 await page.getByRole('button',{name:'分屏',exact:true}).click();
 const left=page.getByRole('region',{name:'左侧分屏',exact:true});
 const right=page.getByRole('region',{name:'右侧分屏',exact:true});
 const editor=pane=>pane.getByRole('textbox',{name:'文件内容',exact:true});
 await editor(left).waitFor(); await editor(right).waitFor();
 assert.equal(await editor(left).inputValue(),'Content c');
 assert.equal(await editor(right).inputValue(),'Content a');
 await editor(right).fill('right unsaved');
 await editor(left).fill('left saved');
 await page.keyboard.press('Control+s');
 await page.waitForFunction(()=>!window.canvasStore.getState().tabs.find(t=>t.artifact?.file_id==='c').dirty);
 assert.deepEqual(saves,['c']);
 assert.equal(await editor(right).inputValue(),'right unsaved','saving the left pane must preserve right draft');
 await left.getByRole('tab',{name:/b.txt/}).click();
 await editor(left).waitFor();
 assert.equal(await editor(left).inputValue(),'Content b');
 assert.equal(await editor(right).inputValue(),'right unsaved','switching left must not remount right');
 await right.getByRole('button',{name:'合并分屏',exact:true}).click();
 await page.getByRole('dialog').waitFor();
 await page.getByRole('button',{name:/取.*消/}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});
 assert.equal(await editor(right).inputValue(),'right unsaved');
 // The keyboard separator changes actual geometry, not just a CSS marker.
 const divider=page.getByRole('separator',{name:'调整分屏大小',exact:true});
 const before=(await left.boundingBox()).width;
 await divider.focus(); await page.keyboard.press('ArrowLeft');
 await page.waitForFunction(before=>document.querySelector('.jx-canvasSplit-pane').getBoundingClientRect().width<before-10,before);
 assert.ok((await left.boundingBox()).width<before-10);
 const rect=await divider.boundingBox();
 await page.mouse.move(rect.x+2,rect.y+200); await page.mouse.down();
 await page.mouse.move(rect.x+80,rect.y+200,{steps:5}); await page.mouse.up();
 await page.waitForFunction(before=>document.querySelector('.jx-canvasSplit-pane').getBoundingClientRect().width>before,before);
 await divider.dblclick();
 await right.getByRole('button',{name:'全屏',exact:true}).click();
 await page.getByRole('button',{name:'退出全屏',exact:true}).waitFor();
 await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'全屏',exact:true}).waitFor();
 await right.getByRole('button',{name:'新建标签页',exact:true}).click();
 await right.getByRole('heading',{name:'工具',exact:true}).waitFor();
 assert.equal(await editor(left).inputValue(),'Content b');
 await page.keyboard.press('Escape');
 assert.equal(await editor(right).inputValue(),'right unsaved');
 await page.screenshot({path:resolve(output,'split.png'),animations:'disabled'});
 await page.evaluate(()=>document.documentElement.dataset.theme='dark');
 await page.screenshot({path:resolve(output,'split-dark.png'),animations:'disabled'});
 // Narrow screens retain reachable controls without page overflow.
 await page.setViewportSize({width:760,height:820});
 const controls=right.getByRole('button',{name:'收起右侧面板',exact:true});
 await controls.click();
 await page.getByRole('dialog').waitFor();
 await page.getByRole('button',{name:/取.*消/}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});
 assert.equal(await editor(right).inputValue(),'right unsaved');
 await page.setViewportSize({width:1660,height:950});
 await right.getByRole('button',{name:'合并分屏',exact:true}).click();
 await page.getByRole('button',{name:'放弃修改',exact:true}).click();
 await page.waitForFunction(()=>!window.canvasStore.getState().isSplit);
 assert.equal(await page.locator('.jx-canvasSplit-pane').count(),1);
 assert.ok((await page.locator('.jx-canvasSplit-pane').boundingBox()).width>600);
 await page.getByRole('button',{name:'收起右侧面板',exact:true}).click();
 await page.waitForFunction(()=>!window.canvasStore.getState().isOpen);
 await page.evaluate(()=>window.canvasStore.getState().openSidebar());
 await page.locator('.jx-canvasSplit-pane').waitFor();
 // Store transitions also cover an empty second pane and a plugin callback in the unfocused pane.
 const invariants=await page.evaluate(()=>{
  const store=window.canvasStore;
  store.getState().resetSidebar();
  store.getState().openPluginView({slug:'fixture',canvasId:'left',status:'success'});
  const first=store.getState().activeTabId;
  store.getState().toggleSplit();
  const empty=store.getState().paneTabs.secondary===null;
  store.getState().focusPane('secondary');
  store.getState().toggleSplit();
  const mergedEmpty=store.getState().activeTabId===first && store.getState().pluginTarget.canvasId==='left';
  store.getState().toggleSplit();
  store.getState().focusPane('secondary');
  store.getState().openPluginView({slug:'fixture',canvasId:'right',status:'success'});
  const second=store.getState().activeTabId;
  store.getState().updatePluginView({title:'Left updated'},first);
  const isolated=store.getState().activeTabId===second
    && store.getState().tabs.find(tab=>tab.id===first).target.title==='Left updated'
    && store.getState().pluginTarget.title===undefined;
  store.getState().activateTab(first);
  const owner=store.getState().activePane==='primary' && store.getState().paneTabs.secondary===second;
  store.getState().closeTab(first);
  const kept=store.getState().tabs.length===1 && store.getState().paneTabs.secondary===second;
  store.getState().resetSidebar();
  return {empty,mergedEmpty,isolated,owner,kept,reset:!store.getState().isSplit && store.getState().activePane==='primary'};
 });
 assert.deepEqual(invariants,{empty:true,mergedEmpty:true,isolated:true,owner:true,kept:true,reset:true});
 assert.deepEqual(errors,[]);
 console.log('Split panes: independent edits/tabs/save, dirty confirmations, resize, fullscreen/Escape, scoped launcher, dark/mobile, merge and reopen passed');
} finally { await browser.close(); }
