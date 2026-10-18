import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { createServer } from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const output = 'node_modules/.tmp/space-presentation';
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {BrowserRouter} from 'react-router';
import {MySpacePanel} from './src/components/myspace/MySpacePanel';
import {CanvasPanel} from './src/components/canvas/CanvasPanel';
import {useCanvasStore} from './src/stores/canvasStore';
import {useEditionStore} from './src/stores/editionStore';
import {useMySpaceStore} from './src/stores/mySpaceStore';
import 'antd/dist/reset.css';
import './src/index.css';
import './src/styles';
useEditionStore.setState({edition:'ee',loaded:true,features:{multi_tenancy:true}});
window.space=useMySpaceStore;
window.canvas=useCanvasStore;
createRoot(document.getElementById('root')).render(<BrowserRouter><MySpacePanel/><CanvasPanel/></BrowserRouter>);
`, resolveDir: process.cwd(), loader:'tsx' }, outfile:output+'/fixture.js', bundle:true, format:'esm', jsx:'automatic',
define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN","VITE_API_BASE_URL":"/api"}'},
external:['/loader.gif','/loader-done.png'],
loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.svg':'dataurl','.png':'dataurl','.md':'text'} });
const server=createServer(async(req,res)=>{
 const path=req.url.split('?')[0];
 if(path==='/fixture.js'||path==='/fixture.css') {
  res.setHeader('Content-Type',path.endsWith('js')?'text/javascript':'text/css');
  return res.end(await readFile(output+path));
 }
 res.setHeader('Content-Type','text/html');
 res.end('<html lang="zh-CN"><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
let textContent='原始文件内容';
let saves=0;
const roots=[{space_id:'personal',name:'个人文件夹',kind:'personal',permission:'edit'},
{space_id:'team:edit',name:'项目资料',kind:'team',permission:'edit'},
{space_id:'team:read',name:'只读资料',kind:'team',permission:'view'}];
let entries=[{name:'项目说明.txt',path:'项目说明.txt',is_directory:false,size:12,modified_at:'2026-10-18T01:00:00Z'},
{name:'图片.png',path:'图片.png',is_directory:false,size:68,modified_at:null},
{name:'资料',path:'资料',is_directory:true,size:0,modified_at:null}];
try {
 browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1360,height:900}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',async route=>{
  const request=route.request(),u=new URL(request.url()),path=u.pathname;
  const reply=data=>route.fulfill({json:{code:0,data}});
  if(path.endsWith('/mounts'))return reply({items:roots,revision:'test'});
  if(path.endsWith('/entries')&&request.method()==='GET')return reply({space:roots.find(r=>r.space_id===u.searchParams.get('space')),path:u.searchParams.get('path'),entries:u.searchParams.get('path')?[]:entries,next_cursor:null});
  if(path.endsWith('/text')) {
   const writable=u.searchParams.get('space')!=='team:read';
   if(request.method()==='PUT') {
    assert.ok(writable,'readonly file must not save');
    textContent=request.postDataJSON().content;saves++;
   }
   return reply({content:textContent,revision:'a'.repeat(64),can_edit:writable,size:Buffer.byteLength(textContent)});
  }
  if(path.endsWith('/content')&&request.method()==='GET'&&!u.searchParams.get('path')?.endsWith('.png'))
   return route.fulfill({contentType:'text/plain',body:textContent});
  if(path.endsWith('/content')&&request.method()==='PUT') {
   const name=u.searchParams.get('path');entries.push({name,path:name,size:request.postDataBuffer().length,is_directory:false,modified_at:null});return reply({});
  }
  if(path.endsWith('/moves')){const body=request.postDataJSON();entries=entries.map(e=>e.path===body.source?{...e,name:body.destination,path:body.destination}:e);return reply({});}
  if(path.endsWith('/content'))return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD5sAAAAASUVORK5CYII=','base64')});
  return reply({items:[],total:0});
 });
 await page.goto('http://127.0.0.1:'+server.address().port+'/my-space/assets');
 await page.locator('.jx-mySpace-docRow-name').filter({hasText:'项目说明.txt'}).waitFor();
 await page.locator('.jx-mySpace-docRow-name').filter({hasText:'项目说明.txt'}).click();
 await page.locator('.jx-canvas').waitFor();
 const editor=page.getByRole('textbox',{name:'文件内容',exact:true});
 await editor.fill('Canvas 保存后的内容');
 await page.locator('.jx-canvas').getByRole('button',{name:'保存',exact:true}).click();
 await page.waitForFunction(()=>!window.canvas.getState().tabs.some(t=>t.dirty));
 assert.equal(textContent,'Canvas 保存后的内容');
 assert.equal(saves,1);
 await page.evaluate(()=>window.canvas.getState().resetSidebar());
 await page.locator('.jx-mySpace-docRow-name').filter({hasText:'项目说明.txt'}).click();
 await editor.waitFor();
 assert.equal(await editor.inputValue(),'Canvas 保存后的内容');
 await page.evaluate(()=>window.canvas.getState().resetSidebar());
 assert.equal(await page.locator('.ant-table').count(),0);
 assert.equal(await page.locator('.jx-mySpace-title').innerText(),'个人文件夹');
 await page.locator('input[type=file]').first().setInputFiles({name:'新增.txt',mimeType:'text/plain',buffer:Buffer.from('new')});
 const row=page.locator('.jx-mySpace-docRow').filter({hasText:'新增.txt'});
 await row.waitFor();
 await row.hover();
 await row.getByTitle('更多操作').click();
 await page.getByRole('menuitem',{name:'重命名',exact:true}).click();
 await page.getByRole('textbox',{name:'名称',exact:true}).fill('改名.txt');
 await page.locator('.ant-modal').getByRole('button',{name:/确.*定|OK/}).click();
 await page.locator('.jx-mySpace-docRow-name').filter({hasText:'改名.txt'}).waitFor();
 await page.getByRole('radio',{name:'图片',exact:true}).click();
 await page.locator('.jx-mySpace-imgThumb.loaded').waitFor();
 await page.getByRole('radio',{name:'文档',exact:true}).click();
 await page.screenshot({path:'/tmp/space-presentation-personal.png',fullPage:true});
 await page.evaluate(()=>window.space.getState().setAssetScope('team'));
 await page.locator('.jx-team-workspace-sidebar').waitFor();
 await page.locator('.ant-tree-title').filter({hasText:'只读资料'}).click();
 await page.getByText('仅可查看',{exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:/上传文件|新建文件夹/}).count(),0);
 const readonly=page.locator('.jx-mySpace-docRow').filter({hasText:'项目说明.txt'});
 await readonly.locator('.jx-mySpace-docRow-name').click();
 await page.getByText('当前文件只读',{exact:true}).waitFor();
 assert.equal(await page.locator('.jx-canvas').getByRole('button',{name:'保存',exact:true}).count(),0);
 assert.equal(await page.getByRole('textbox',{name:'文件内容',exact:true}).count(),0);
 assert.equal(saves,1);
 await page.evaluate(()=>window.canvas.getState().resetSidebar());
 await readonly.hover();
 await readonly.getByTitle('更多操作').click();
 assert.equal(await page.getByRole('menuitem',{name:'重命名',exact:true}).count(),0);
 assert.equal(await page.getByRole('menuitem',{name:'删除',exact:true}).count(),0);
 await page.keyboard.press('Escape');
 await page.screenshot({path:'/tmp/space-presentation-team.png',fullPage:true});
 assert.deepEqual(errors,[]);
 console.log('SPACE_PRESENTATION_PASS: layout, upload, rename, images, team sidebar, readonly controls, Canvas editing/save/reopen/readonly');
} catch(error) { if(browser) { const pages=browser.contexts()[0]?.pages(); if(pages?.[0]) await pages[0].screenshot({path:'/tmp/space-presentation-error.png',fullPage:true}); } throw error; } finally { await browser?.close(); await new Promise(resolve=>server.close(resolve)); }
