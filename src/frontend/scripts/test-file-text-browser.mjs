import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/file-text-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { CanvasPanel } from './src/components/canvas/CanvasPanel';
import { useCanvasStore } from './src/stores/canvasStore';
import { useCanvasGuard } from './src/components/canvas/useCanvasGuard';
import './src/styles/variables.css';
import './src/styles/canvas.css';
const file={file_id:'note',name:'note.md',url:'/files/note',mime_type:'text/markdown'};
useCanvasStore.getState().openCanvas(file);
window.canvas=useCanvasStore;
function Fixture(){const guard=useCanvasGuard();return <><button onClick={()=>guard(()=>useCanvasStore.getState().openCanvas(file))}>Reopen</button><button onClick={()=>guard(()=>useCanvasStore.getState().openCanvas({...file,name:'code.go',mime_type:'application/octet-stream'}))}>Open Go</button><button onClick={()=>guard(()=>useCanvasStore.getState().openCanvas({...file,name:'note.txt',mime_type:'text/plain'}))}>Open TXT</button><button onClick={()=>guard(()=>useCanvasStore.getState().openCanvas({...file,name:'page.html',mime_type:'text/html'}))}>Open HTML</button><CanvasPanel/></>}
createRoot(document.getElementById('root')).render(<Fixture/>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}});
const browser = await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try {
 const page=await browser.newPage();
 page.on('pageerror', error=>console.error(error));
 let content='# Original\n\n**Bold**\n\n| Name | Value |\n| --- | --- |\n| A | 1 |', revision='a'.repeat(64), canEdit=true, fail=0, saves=0;
 await page.route('https://text.test/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname==='/fixture.js'||url.pathname==='/fixture.css')return route.fulfill({
    contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css',
    body:await readFile(resolve(output,url.pathname.slice(1))) });
  if(url.pathname==='/api/v1/file/note/text'){
   if(route.request().method()==='PUT'){
    saves++;
    const body=route.request().postDataJSON();
    if(fail)return route.fulfill({status:fail,json:{detail:'文件已被修改，请保留草稿'}});
    assert.equal(body.revision,revision);
    content=body.content; revision='b'.repeat(64);
   }
   return route.fulfill({json:{code:10000,data:{file_id:'note',name:'note.md',content,revision,size:content.length,can_edit:canEdit}}});
  }
  if(url.pathname==='/api/files/note')return route.fulfill({contentType:'text/markdown',body:content});
  if(url.pathname.startsWith('/api'))return route.fulfill({json:{code:10000,data:[]}});
  return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://text.test/');
 const editor=page.getByRole('textbox',{name:'文件内容'});
 await editor.waitFor({timeout:5000});
 await editor.getByRole('heading',{name:'Original'}).waitFor();
 assert.equal(await page.evaluate(()=>window.canvas.getState().tabs[0].dirty),false,'loading Markdown must not modify the file');
 assert.equal(await editor.locator('strong').innerText(),'Bold');
 assert.equal(await editor.locator('table').count(),1);
 assert.equal(await page.getByRole('button',{name:'编辑',exact:true}).count(),0);
 assert.equal(await page.getByRole('button',{name:'取消编辑',exact:true}).count(),0);
 assert.equal(await page.locator('.jx-fileTextPanel-toolbar').count(),0);
 const saveButton=page.getByRole('button',{name:'保存',exact:true});
 assert.equal(await saveButton.evaluate(el=>el.parentElement.className),'jx-canvas-header-actions');
 assert.equal(await saveButton.innerText(),'');
 assert.equal(await page.locator('.jx-canvas-header-actions .jx-artifactAction').innerText(),'');
 const saveBox=await saveButton.boundingBox();
 const downloadBox=await page.locator('.jx-canvas-header-actions .jx-artifactAction').boundingBox();
 assert.ok(Math.abs(saveBox.y-downloadBox.y)<2,'save and download share the same row');
 await editor.focus();
 assert.equal(await editor.evaluate(el=>getComputedStyle(el).outlineStyle),'none');
 await page.screenshot({path:resolve(output,'rendered-markdown.png'),animations:'disabled'});
 await editor.press('Control+End');
 await editor.press('Enter');
 await editor.press('Enter');
 await editor.pressSequentially('追加文字');
 await page.waitForFunction(()=>window.canvas.getState().tabs[0].dirty);
 await editor.press('Control+s');
 await page.waitForFunction(()=>!window.canvas.getState().tabs[0].dirty);
 assert.equal(saves,1);
 assert.equal(await page.locator('.ant-message-notice').filter({hasText:'保存成功'}).count(),0);
 assert.match(content,/# Original/);
 assert.match(content,/\*\*Bold\*\*/);
 assert.match(content,/\| Name/);
 assert.match(content,/追加文字/);
 await editor.fill('draft on conflict');
 fail=409;
 await saveButton.click();
 await page.getByRole('alert').filter({hasText:'文件已被修改'}).waitFor();
 assert.equal((await editor.innerText()).trim(),'draft on conflict');
 assert.equal(await page.evaluate(()=>window.canvas.getState().tabs[0].dirty),true);
 await page.getByRole('button',{name:'关闭「note.md」',exact:true}).click();
 await page.getByRole('button',{name:/^取\s*消$/,exact:true}).click();
 assert.equal((await editor.innerText()).trim(),'draft on conflict');
 assert.equal(await page.evaluate(()=>{
  const event=new Event('beforeunload',{cancelable:true});
  window.dispatchEvent(event);return event.defaultPrevented;
 }),true);
 fail=0;
 await saveButton.click();
 await page.waitForFunction(()=>!window.canvas.getState().tabs[0].dirty);
 for (const name of ['Open Go','Open TXT','Open HTML']) {
  await page.getByRole('button',{name,exact:true}).click();
  if (name === 'Open HTML') {
    await page.getByRole('button',{name:'预览',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'预览',exact:true}).innerText(),'');
    assert.equal(await page.getByRole('button',{name:'源码',exact:true}).innerText(),'');
    assert.equal(await page.getByRole('button',{name:'预览',exact:true}).evaluate(el=>!!el.closest('.jx-canvas-header-actions')),true);
    assert.equal(await page.locator('.jx-canvas-body .jx-fileTextPanel-viewActions').count(),0);
    assert.equal(await page.getByRole('button',{name:'预览',exact:true}).getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('textarea').count(),0);
    await page.getByRole('button',{name:'源码',exact:true}).click();
  }
  await page.locator('textarea[aria-label="文件内容"]').waitFor();
  await editor.focus();
  assert.equal(await editor.evaluate(el=>getComputedStyle(el).outlineStyle),'none');
  await editor.fill('');
  await saveButton.click();
  await page.waitForFunction(()=>!window.canvas.getState().tabs[0].dirty);
  assert.equal(content,'');
  await editor.fill(name === 'Open HTML' ? '<!doctype html><h1>Draft preview</h1>' : 'next content');
  if (name === 'Open HTML') {
    await page.getByRole('button',{name:'预览',exact:true}).click();
    await page.frameLocator('iframe').getByRole('heading',{name:'Draft preview'}).waitFor();
    assert.equal(await page.locator('iframe').getAttribute('sandbox'),'allow-scripts');
    await page.getByRole('button',{name:'源码',exact:true}).click();
    assert.match(await editor.inputValue(),/Draft preview/);
  }
  await saveButton.click();
  await page.waitForFunction(()=>!window.canvas.getState().tabs[0].dirty);
 }
 canEdit=false;
 await page.getByRole('button',{name:'Reopen',exact:true}).click();
 await page.getByText('当前文件只读',{exact:true}).waitFor();
 assert.equal(await editor.count(),0);
 assert.equal(await saveButton.count(),0);
 console.log('Text browser: rendered Markdown editing, no blue outline, header save, no edit/cancel row, Markdown roundtrip, Ctrl+S, conflict guard and readonly passed');
} finally {await browser.close();}
