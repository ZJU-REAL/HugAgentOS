import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {chromium} from 'playwright';
import {mkdir, readFile} from 'node:fs/promises';
const output='node_modules/.tmp/canvas-launcher';
await mkdir(output,{recursive:true});
await build({stdin:{contents:
"import React from 'react';import {createRoot} from 'react-dom/client';import {RightSidebarPanel} from './src/components/canvas/RightSidebarPanel';import {useCanvasStore} from './src/stores/canvasStore';import './src/styles/variables.css';import './src/styles/canvas.css';import {useCanvasLauncherStore} from './src/components/canvas/canvasLauncherStore';window.launcherStore=useCanvasLauncherStore;window.canvasStore=useCanvasStore;useCanvasStore.getState().openCanvas({file_id:'fixture',name:'示例.txt',url:'/files/fixture'});createRoot(document.getElementById('root')).render(<React.StrictMode><RightSidebarPanel/></React.StrictMode>);",
resolveDir:process.cwd(),loader:'tsx'},bundle:true,jsx:'automatic',format:'esm',define:{'import.meta.env':'{}'},loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'},outfile:output+'/fixture.js'});
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try {
 const page=await browser.newPage({viewport:{width:700,height:950}});
 await page.route('https://launcher.test/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/fixture.js'||path==='/fixture.css')return route.fulfill({body:await readFile(output+path),contentType:path.endsWith('.js')?'text/javascript':'text/css'});
  if(path==='/api/files/fixture')return route.fulfill({body:'原文件内容',contentType:'text/plain'});
  if(path==='/api/v1/file/upload')return route.fulfill({json:{code:0,data:{file_id:'uploaded',name:'上传文件.txt',download_url:'/files/fixture',size:12,mime_type:'text/plain'}}});
  if(path.startsWith('/api'))return route.fulfill({json:{code:0,data:[]}});
  return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>body{margin:0}#root{height:940px;display:flex;position:relative}#root>div{width:100%}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://launcher.test/');
 await page.evaluate(()=>document.getElementById('root').style.width='420px');
 assert.ok(Math.abs((await page.locator('.jx-canvas').boundingBox()).width-420)<2);
 await page.evaluate(()=>document.getElementById('root').style.width='680px');
 assert.ok(Math.abs((await page.locator('.jx-canvas').boundingBox()).width-680)<2);
 await page.getByRole('button',{name:'新建标签页',exact:true}).click();
 await page.getByRole('heading',{name:'工具',exact:true}).waitFor();
 assert.equal(await page.locator('.jx-canvasLauncher').count(),1);
 assert.notEqual(await page.getByRole('button',{name:'关闭「新标签页」',exact:true}).evaluate(el=>getComputedStyle(el).backgroundImage),'none');
 await page.getByRole('button',{name:'关闭「新标签页」',exact:true}).click();
 assert.equal(await page.locator('.jx-canvasLauncher').count(),0);
 await page.getByRole('button',{name:'新建标签页',exact:true}).click();
 await page.getByRole('button',{name:'示例.txt',exact:true}).click();
 assert.equal(await page.locator('.jx-canvasLauncher').count(),0);
 await page.evaluate(()=>window.canvasStore.getState().setTabDirty(window.canvasStore.getState().activeTabId,true));
 await page.getByRole('button',{name:'新建标签页',exact:true}).click();
 assert.equal(await page.locator('.ant-modal-confirm').count(),0,'Launcher must preserve the mounted dirty file');
 assert.equal(await page.evaluate(()=>window.canvasStore.getState().tabs[0].dirty),true);
 await page.evaluate(()=>window.canvasStore.getState().setTabDirty(window.canvasStore.getState().activeTabId,false));
 await page.locator('.jx-canvasLauncher input[type=file]').setInputFiles({name:'上传文件.txt',mimeType:'text/plain',buffer:Buffer.from('上传内容')});
 await page.getByRole('tab',{name:/上传文件.txt/}).waitFor();
 assert.equal(await page.locator('.jx-canvasLauncher').count(),0);

 await page.evaluate(()=>{window.navigationInputs=[];window.launcherStore.getState().bindNavigation(window.canvasStore.getState().activeTabId,async value=>{window.navigationInputs.push(value);});});
 await page.getByRole('button',{name:'新建标签页',exact:true}).click();
 await page.getByRole('textbox',{name:'网页地址',exact:true}).fill('  杭州 天气  ');
 await page.getByRole('textbox',{name:'网页地址',exact:true}).press('Enter');
 await page.waitForFunction(()=>window.navigationInputs.length===1);
 assert.equal(await page.evaluate(()=>window.navigationInputs[0]),'杭州 天气');
 assert.equal(await page.locator('.jx-canvasLauncher').count(),0);
 console.log('Launcher opens, cancels, restores existing content, and preserves dirty edits');
} finally {await browser.close();}
