import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/canvas-layout-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
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
 const page=await browser.newPage({viewport:{width:1340,height:900}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('https://layout.test/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/fixture.js'||path==='/fixture.css') return route.fulfill({contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1)))});
  if(path==='/api/files/report') return route.fulfill({contentType:'text/plain',body:'项目原文件预览。正文与对话各自排版。'});
  if(path.startsWith('/api')) return route.fulfill({json:{code:0,data:[]}});
  if(path.endsWith('.svg')) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="#467ff7"/></svg>'});
  return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:15px Arial;color:var(--color-text);background:var(--color-bg-container)}nav{height:40px}.shell{display:flex;height:calc(100vh - 40px)}.navigation{width:280px;flex:none;background:var(--color-bg-layout);padding:24px}.jx-appMainLayout{flex:1}.jx-chatWrap{justify-content:space-between}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://layout.test/');
 await page.getByRole('button',{name:'Preview',exact:true}).click();
 await page.locator('.jx-canvas').waitFor();
 const check=async(label,split)=>{
  await page.waitForTimeout(350);
  const dimensions=await page.evaluate(()=>{
   const pane=document.querySelector('.jx-primaryPane'), bar=document.querySelector('.jx-composerBar');
   const bounds=bar.getBoundingClientRect();
   const escaped=[...bar.querySelectorAll('button, [role=button], .jx-modelEffortStatic')].filter(el=>{const r=el.getBoundingClientRect();return r.width>0&&(r.left<bounds.left-1||r.right>bounds.right+1);}).map(el=>el.className);
   const text=document.querySelector('.jx-bubble');
   return {width:pane.getBoundingClientRect().width,escaped,textWidth:text.clientWidth,textScroll:text.scrollWidth,screen:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth};
  });
  await page.screenshot({path:resolve(output,label+'.png'),animations:'disabled'});
  if(split)assert.ok(dimensions.width>=420,JSON.stringify(dimensions));
  assert.deepEqual(dimensions.escaped,[],label);
  assert.ok(dimensions.textScroll<=dimensions.textWidth+1,JSON.stringify(dimensions));
  assert.ok(dimensions.scroll<=dimensions.screen,JSON.stringify(dimensions));
 };
 await check('split',true);
 assert.equal(await page.locator('.jx-canvas-header-actions > a').evaluate(el=>getComputedStyle(el).whiteSpace),'nowrap');
 const handle=page.locator('.jx-canvas-dragHandle'); const box=await handle.boundingBox();
 await page.mouse.move(box.x+2,box.y+50);await page.mouse.down();await page.mouse.move(300,box.y+50,{steps:8});await page.mouse.up();
 await check('drag-limit',true);
 await handle.focus();await page.keyboard.press('ArrowLeft');await check('keyboard-limit',true);
 await page.keyboard.press('ArrowRight');await check('keyboard-resize',true);
 await page.setViewportSize({width:1100,height:820});await check('narrow-overlay',false);
 const canvas=await page.locator('.jx-canvasPanelSlot').boundingBox();const main=await page.locator('.jx-appMainLayout').boundingBox();
 assert.ok(Math.abs(canvas.width-main.width)<2,'narrow preview should cover the main area');
 await page.getByRole('button',{name:'收起右侧面板',exact:true}).click();
 await page.locator('.jx-canvas').waitFor({state:'detached'});
 await page.setViewportSize({width:760,height:820});await check('narrow-chat',false);
 await page.locator('.tiptap').fill('继续优化报告');
 await page.getByRole('button',{name:'发送',exact:true}).click();
 assert.equal(await page.locator('html').getAttribute('data-sent'),'true');
 await page.getByRole('button',{name:'切换模型与思考强度'}).click();
 await page.getByRole('menu').waitFor();
 const menu=await page.getByRole('menu').boundingBox();const pane=await page.locator('.jx-primaryPane').boundingBox();
 assert.ok(menu.x>=pane.x&&menu.x+menu.width<=pane.x+pane.width,'model menu should fit conversation');
 await page.getByRole('button',{name:'Dark',exact:true}).click();await check('dark-chat',false);
 assert.deepEqual(errors,[]);
 console.log('Canvas layout: split, mouse/keyboard resizing, overlay, long text, composer, menu, collapse and send callback passed');
}finally{await browser.close();}
