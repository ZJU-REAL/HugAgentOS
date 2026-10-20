import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/file-action-layout');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { FilePreviewPane } from './src/components/file/FilePreviewPane';
import { MySpaceImportModal } from './src/components/file/MySpaceImportModal';
import { ArtifactFileAction } from './src/components/file/ArtifactFileAction';
import { DeliveredFiles } from './src/components/chat/DeliveredFiles';
import FileAttachmentCard from './src/components/file/FileAttachmentCard';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/mobile.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
import './src/styles/myspace.css';
import './src/styles/canvas.css';
useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'dual', activeLocal: false });
const file = { id: 'report', file_id: 'report', name: 'quarterly_report_20261012_183333_very_long_filename.txt', type: 'document', origin: new URLSearchParams(location.search).get('origin'), download_url: '/files/report', created_at: '' };
const modal = new URLSearchParams(location.search).get('modal');
createRoot(document.getElementById('root')).render(modal ? <MySpaceImportModal open onClose={()=>{}} mode={modal} /> : <main>
 <section id="preview" style={{ display:'flex', height:180 }}><FilePreviewPane item={file} /></section>
 <section className="jx-canvas-header"><div className="jx-canvas-header-left"><span className="jx-canvas-fileName">{file.name}</span></div><div className="jx-canvas-header-actions"><button className="jx-canvas-actionBtn" aria-label="Save">S</button><ArtifactFileAction file={file} className="jx-canvas-actionBtn" /></div></section>
 <section><FileAttachmentCard name={file.name} artifact={file} /></section>
 <section><DeliveredFiles artifacts={[{ ...file, url:file.download_url }]} /></section>
 </main>);
`, resolveDir: process.cwd(), loader:'tsx' }, outfile: resolve(output,'fixture.js'), bundle:true,
 format:'esm', jsx:'automatic', define:{'import.meta.env':'{}'},
 external:['/loader.gif','/loader-done.png'], loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'} });
const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_EXECUTABLE_PATH});
try {
 for (const lang of ['zh-CN','en']) {
  for (const origin of ['cloud','local']) {
   const page = await browser.newPage({viewport:{width:720,height:800}});
   await page.addInitScript(lang => localStorage.setItem('jx_lang',lang), lang);
   const errors = [];
   page.on('pageerror', error => errors.push(error.message));
   await page.route('http://fixture.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1)))});
    if (path === '/api/v1/artifacts') return route.fulfill({json:{code:0,data:{items:Array.from({length:20},(_,i)=>({id:'report-'+i,file_id:'report',name:'quarterly_report_20261012_183333_very_long_filename.txt',type:'document',origin,download_url:'/files/report',created_at:'2026-10-12'})),total:20,has_more:false}}});
    if (path === '/api/v1/myspace/folders') return route.fulfill({json:{code:0,data:{tree:[]}}});
    if (path === '/api/v1/my-teams') return route.fulfill({json:{code:0,data:{items:[]}}});
    if (path.endsWith('/applications')) return route.fulfill({json:{applications:[{id:'editor',name:'Editor',icon:null}],default_id:'editor'}});
    if (path.startsWith('/api/files/')) return route.fulfill({contentType:'text/plain',body:'Preview content'});
    if (path.endsWith('.svg')) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>'});
    return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>body{margin:0;font:14px Arial}main{padding:12px}section{margin-bottom:16px}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
   });
   await page.goto('http://fixture.test/?origin='+origin);
   await page.getByText('Preview content',{exact:true}).waitFor();
   for (const width of [720,420,320,240]) {
    await page.setViewportSize({width,height:800});
    const layout = await page.evaluate(() => {
     const selectors = ['.jx-spaceImportPreview-headerAction','.jx-canvas-header-actions > a','.jx-canvas-header-actions > .jx-artifactOpen','.jx-fileCard-nativeAction','.jx-fileAction-button'];
     return selectors.flatMap(selector => [...document.querySelectorAll(selector)].map(action => {
      const target = action.querySelector('.jx-artifactOpen-main') || action;
      const box = target.getBoundingClientRect();
      const walker = document.createTreeWalker(target,NodeFilter.SHOW_TEXT);
      const rects = [];
      while (walker.nextNode()) {
       if (!walker.currentNode.textContent.trim()) continue;
       const range = document.createRange(); range.selectNodeContents(walker.currentNode);
       rects.push(...[...range.getClientRects()].map(r => ({left:r.left,right:r.right,top:r.top,bottom:r.bottom})));
      }
      const icon = target.querySelector('.anticon')?.getBoundingClientRect();
      return {selector,text:target.textContent,box:{left:box.left,right:box.right,top:box.top,bottom:box.bottom},rects,icon:icon && {top:icon.top,bottom:icon.bottom},overflow:target.scrollWidth-target.clientWidth};
     }));
    });
    assert.equal(layout.length, 3);
    const delivery = page.locator('[data-presented-file]');
    assert.equal(await delivery.count(), 1);
    assert.equal(await delivery.locator('button button').count(), 0);
    assert.ok((await delivery.locator('.hg-delivery-name').boundingBox()).width >= 60, JSON.stringify({lang,origin,width}));
    for (const action of layout) {
     const context = JSON.stringify({lang,origin,width,...action});
     assert.ok(action.rects.length && Math.max(...action.rects.map(r=>r.top))-Math.min(...action.rects.map(r=>r.top))<2, 'label wraps: '+context);
     for (const r of action.rects) assert.ok(r.left>=action.box.left-1 && r.right<=action.box.right+1 && r.top>=action.box.top-1 && r.bottom<=action.box.bottom+1,'label escapes control: '+context);
     assert.ok(action.overflow<=1,'control content overflows: '+context);
     assert.ok(action.rects[0].top < action.icon.bottom && action.rects[0].bottom > action.icon.top,'icon and label are not on the same row: '+context);
    }
    const header = await page.locator('.jx-spaceImportPreview-header').evaluate(el => {
     const name=el.querySelector('.jx-spaceImportPreview-headerName');
     const action=el.querySelector('.jx-spaceImportPreview-headerAction');
     return {name:name.getBoundingClientRect().right,action:action.getBoundingClientRect().left,right:action.getBoundingClientRect().right,header:el.getBoundingClientRect().right};
    });
    assert.ok(header.name<=header.action && header.right<=header.header,'preview filename overlaps action');
   }
   await page.screenshot({path:resolve(output,lang+'-'+origin+'.png'),animations:'disabled'});
   if (process.argv.includes('--cards-only')) {
    assert.deepEqual(errors, []);
    await page.close();
    continue;
   }
   for (const mode of ['attach','project']) {
    await page.goto('http://fixture.test/?origin='+origin+'&modal='+mode);
    await page.locator('.jx-spaceImportItem').first().click();
    await page.getByText('Preview content',{exact:true}).waitFor();
    for (const viewport of [{width:1328,height:834},{width:768,height:800},{width:390,height:844},{width:320,height:667},{width:390,height:568},{width:844,height:390}]) {
     await page.setViewportSize(viewport);
     const bounds = await page.locator('.jx-spaceImportModal').evaluate(el => {
      const rect = node => { const r=node.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}; };
      const left=el.querySelector('.jx-spaceImportLeft'),right=el.querySelector('.jx-spaceImportRight');
      const list=el.querySelector('.jx-spaceImportList'),preview=el.querySelector('.jx-spaceImportPreview-body');
      const name=el.querySelector('.jx-spaceImportPreview-headerName'),action=el.querySelector('.jx-spaceImportPreview-headerAction');
      return {left:rect(left),right:rect(right),modal:rect(el),name:rect(name),action:rect(action),listHeight:list.clientHeight,listScroll:list.scrollHeight,previewHeight:preview.clientHeight,controls:[...el.querySelector('.jx-spaceImportControls').children].map(rect),vw:innerWidth,vh:innerHeight};
     });
     const context = JSON.stringify({lang,origin,mode,viewport,...bounds});
     assert.ok(bounds.left.width>0 && bounds.right.width>0 && bounds.listHeight>=40 && bounds.previewHeight>=40,'both panes must remain usable: '+context);
     assert.ok(bounds.modal.left>=0 && bounds.modal.right<=bounds.vw+1 && bounds.modal.bottom<=bounds.vh,'modal escapes viewport: '+context);
     assert.ok(bounds.name.right<=bounds.action.left && bounds.action.right<=bounds.right.right,'filename overlaps action: '+context);
     for (const control of bounds.controls) assert.ok(control.left>=bounds.left.left && control.right<=bounds.left.right+1,'search control escapes pane: '+context);
     if (viewport.width<=768) assert.ok(bounds.left.bottom<=bounds.right.top,'phone panes overlap: '+context);
     else assert.ok(bounds.left.right<=bounds.right.left,'desktop panes overlap: '+context);
     if (origin === 'local') {
      await page.locator('.jx-spaceImportPreview-headerAction .jx-artifactOpen-menu').click();
      const menu = page.getByRole('menuitem');
      await menu.waitFor();
      const box = await menu.boundingBox();
      assert.ok(box.x >= -1 && box.x + box.width <= viewport.width + 1 && box.y >= -1 && box.y + box.height <= viewport.height + 1,'folder menu escapes viewport: '+context);
      await page.keyboard.press('Escape');
      await page.locator('.jx-spaceImportPreview-headerName').click();
     }
     assert.ok(bounds.listScroll>bounds.listHeight,'long list must scroll inside its pane: '+context);
     await page.locator('.jx-spaceImportList').evaluate(el=>{el.scrollTop=el.scrollHeight;});
     assert.ok(await page.locator('.jx-spaceImportList').evaluate(el=>el.scrollTop>0));
     if (viewport.width === 390 && viewport.height === 568) await page.screenshot({path:resolve(output,lang+'-'+origin+'-'+mode+'-portrait.png'),animations:'disabled'});
    }
    await page.screenshot({path:resolve(output,lang+'-'+origin+'-'+mode+'-phone.png'),animations:'disabled'});
   }
   assert.deepEqual(errors,[]);
   await page.close();
  }
 }
 console.log(process.argv.includes('--cards-only')
  ? 'file action layout passed: 16 width/locale/origin combinations, delivery filenames and independent actions'
  : 'file action and import-modal layout passed');
} finally { await browser.close(); }
