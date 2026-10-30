import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/html-preview-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { CanvasPanel } from './src/components/canvas/CanvasPanel';
import { useCanvasStore } from './src/stores/canvasStore';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
useDeploymentModeStore.setState({isDesktop:true,provisionMode:'dual',activeLocal:false});
const file={file_id:'page',name:'预览.html',url:'/files/page',origin:'local',mime_type:'text/html'};
useCanvasStore.getState().openCanvas(file);
function Fixture(){return <><button onClick={()=>useCanvasStore.getState().openCanvas(file)}>Reopen</button><button onClick={()=>useCanvasStore.getState().openCanvas({...file,file_id:"external",url:"https://external.test/page.html",origin:"cloud"})}>External</button><CanvasPanel/></>}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}});
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage();
 let status=401,body=JSON.stringify({detail:'请先登录'}),contentType='application/json',version=0;
 const requests=[];
 await page.route('https://external.test/**', route=>route.fulfill({contentType:'text/html',body:'<!doctype html><h1>External HTML</h1>'}));
 await page.route('https://preview.test/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
  if(url.pathname==='/api/files/page'){
   requests.push(url);
   return route.fulfill({status,contentType,body});
  }
  if(url.pathname.startsWith('/api'))return route.fulfill({json:{code:0,data:[]}});
  return route.fulfill({contentType:'text/html',body:'<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://preview.test/');
 await page.locator('.jx-canvas-error').waitFor({timeout:5000});
 assert.match(await page.locator('.jx-canvas-error').innerText(),/登录|身份/);
 assert.equal(await page.locator('iframe').count(),0,'API errors must not be embedded as HTML');
 for(status of [403,404]){
  await page.getByRole('button',{name:'Reopen'}).click();
  await page.waitForFunction(expected=>document.querySelector('.jx-canvas-error')?.textContent?.includes(expected),status===403?'权限':'删除');
 }
 status=200;contentType='text/html';
 for(version of [1,2]){
  body='<!doctype html><html><head><meta charset="utf-8"><base href="https://assets.test/"></head><body><h1>内容 '+version+'</h1><script>try{parent.localStorage.getItem("secret");document.body.dataset.isolated="no"}catch{document.body.dataset.isolated="yes"}</script></body></html>';
  await page.getByRole('button',{name:'Reopen'}).click();
  await page.frameLocator('iframe').getByRole('heading',{name:'内容 '+version}).waitFor();
  assert.equal(await page.locator('iframe').getAttribute('sandbox'),'allow-scripts');
  assert.equal(await page.frameLocator('iframe').locator('html').evaluate(el=>el.ownerDocument.compatMode),'CSS1Compat');
  assert.equal(await page.frameLocator('iframe').locator('body').getAttribute('data-isolated'),'yes');
  assert.equal(await page.frameLocator('iframe').locator('html').evaluate(el=>el.ownerDocument.baseURI),'https://assets.test/');
 }
 assert.ok(requests.every(url=>url.searchParams.get('hg_target')==='local'));
 assert.notEqual(requests.at(-1).searchParams.get('v'),requests.at(-2).searchParams.get('v'));
 await page.getByRole('button',{name:'External',exact:true}).click();
 await page.frameLocator('iframe').getByRole('heading',{name:'External HTML'}).waitFor({timeout:5000});
 assert.equal(await page.locator('iframe').getAttribute('sandbox'),'allow-scripts');
 console.log('HTML preview: readable 401/403/404, local routing, sandbox isolation and fresh reopen passed');
}finally{await browser.close();}
