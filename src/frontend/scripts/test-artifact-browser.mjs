import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createServer } from 'node:http';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/artifact-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { DeliveredFiles } from './src/components/chat/DeliveredFiles';
import { FilePreviewPane } from './src/components/file/FilePreviewPane';
import { useCanvasStore } from './src/stores/canvasStore';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { registerLocalChat, setHybridDual } from './src/api';
import './src/styles/variables.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
import './src/styles/myspace.css';
import './src/styles/canvas.css';
setHybridDual(true); registerLocalChat('local-chat');
useDeploymentModeStore.setState({isDesktop:true,provisionMode:'dual',activeLocal:false});
window.deliveryPreviews = [];
useCanvasStore.setState({ openCanvas: (file) => { window.deliveryPreviews.push(file.file_id); } });
function Fixture() {
 const [file, setFile] = React.useState({id:'office',file_id:'office',name:'本机报告.docx',type:'document',download_url:'/files/office',origin:'local',created_at:''});
 return <main><h2>文件操作</h2>
 <section id="local"><h3>本机文件</h3><DeliveredFiles chatId="local-chat" artifacts={[{file_id:'report', name:'北极星周报.pdf', url:'/files/report', size:5200000}]} /></section>
 <section id="reference"><DeliveredFiles chatId="local-chat" artifacts={[{file_id:'sheet',name:'FastGPT知识库 chunk_token统计.xlsx',description:'FastGPT 知识库 chunk/token 统计'}]} /></section>
 <section id="cloud"><h3>云端文件</h3><DeliveredFiles artifacts={[{file_id:'cloud', name:'云端周报.pdf', url:'/files/cloud', origin:'cloud', size:5200000}]} /></section>
 <section id="multiple"><DeliveredFiles chatId="local-chat" artifacts={Array.from({length:6},(_,index)=>({file_id:'multi-'+index,name:index===0?'图片.png':'A very long 文件名称 '+index+'.txt',mime_type:index===0?'image/png':'text/plain'}))} /></section>
 <nav>{['pdf','image','text','project'].map(kind => <button key={kind} onClick={() => setFile({id:kind,file_id:kind,name:kind === 'image' ? '本机.png' : kind === 'text' ? '本机.txt' : kind === 'project' ? '项目.docx' : '本机.pdf',type:kind === 'image' ? 'image' : 'document',download_url:kind === 'project' ? '/v1/projects/local-project/local-files/raw?path=report.docx' : '/files/'+kind,origin:'local',created_at:''})}>{kind} preview</button>)}</nav>
 <section id="preview"><FilePreviewPane item={file} /></section>
 </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'),
 bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
 external: ['/loader.gif','/loader-done.png'], loader: { '.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl' } });
// Chromium downloads can bypass Playwright routing; serve that transport for real.
const downloadRequests = [];
const server = createServer((req, res) => {
 downloadRequests.push(req.url);
 res.writeHead(200, { 'content-type':'application/pdf',
  'content-disposition': "attachment; filename*=UTF-8''" + encodeURIComponent('云端周报.pdf') });
 res.end('%PDF-1.4\n%%EOF');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({ headless: true });
try {
 const page = await browser.newPage({ acceptDownloads: true, viewport: { width: 1060, height: 820 } });
 await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
 const errors = [], requests = [], native = [];
 let failNextOpen = false;
 page.on('pageerror', e => { errors.push(e.message); console.error('browser error:', e.message); });
 await page.context().route(origin + '/**', async route => {
  const url = new URL(route.request().url()); requests.push(url.pathname + url.search);
  if (url.pathname === '/__desktop/open-path') {
   native.push(url.searchParams.get('path')); return route.abort('aborted');
  }
  if (url.pathname.endsWith('/applications')) return route.fulfill({ json: {
   applications: [{id:'default-editor',name:'Editor',icon:null},{id:'alternate',name:'Alternate',icon:null}],
   default_id:'default-editor',path:'/Reports/北极星周报.pdf',
  } });
  if (url.pathname.endsWith('/open')) {
   const body = route.request().postDataJSON();
   if (failNextOpen) { failNextOpen = false; return route.fulfill({status:503,json:{detail:'无法打开文件，请重试'}}); }
   native.push(body.action === 'reveal' ? '/Reports' : body.application || '/Reports/北极星周报.pdf');
   return route.fulfill({json:{ok:true}});
  }
  if (url.pathname.endsWith('/local-path')) return route.fulfill({ json: { path: '/Reports/北极星周报.pdf', folder_path: '/Reports' } });
  if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css')
   return route.fulfill({ contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: await readFile(resolve(output, url.pathname.slice(1))) });
  if (url.pathname.startsWith('/icons/file/')) return route.fulfill({
   contentType:'image/svg+xml',body:await readFile(resolve('public',url.pathname.slice(1))),
  });
  if (url.pathname.endsWith('.svg')) return route.fulfill({ contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="#467ff7"/></svg>' });
  if (url.pathname === '/api/files/text') return route.fulfill({contentType:'text/plain',body:'本机预览成功'});
  if (url.pathname === '/api/files/image') return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="blue"/></svg>'});
  if (url.pathname.startsWith('/api/files') || url.pathname.startsWith('/api/v1/projects/')) return route.fulfill({ contentType:'application/pdf', headers: {'content-disposition': url.pathname === '/api/files/cloud' ? 'attachment; filename="cloud.pdf"' : 'inline'}, body:'%PDF-1.4\n%%EOF' });
  return route.fulfill({ contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>body{margin:0;background:var(--color-bg-container);color:var(--color-text);font:15px Arial}main{padding:28px;max-width:920px;margin:auto}section{margin:22px 0}#preview{height:420px}button,a{font:inherit}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
 });
 await page.goto(origin + '/');
 const local = page.locator('#local');
 await local.getByRole('button',{name:'使用 Editor 打开',exact:true}).waitFor();
 assert.equal(await local.getByRole('link').count(),0);
 assert.equal((await page.locator('#cloud').getByRole('link',{name:'下载 云端周报.pdf'}).innerText()).trim(),'下载');
 const colors = async () => local.locator('.hg-delivery-file').evaluate(element => ({
  background:getComputedStyle(element).backgroundColor,
  border:getComputedStyle(element).borderTopColor,
  text:getComputedStyle(element.querySelector('.hg-delivery-name')).color,
  secondary:getComputedStyle(element.querySelector('.hg-delivery-description')).color,
 }));
 assert.deepEqual(await colors(),{background:'rgb(250, 250, 250)',border:'rgba(0, 0, 0, 0.04)',text:'rgb(15, 17, 21)',secondary:'rgb(129, 133, 140)'});
 assert.equal(await page.locator('#reference .hg-delivery-type-excel').evaluate(element=>getComputedStyle(element).color),'rgb(34, 197, 94)');
 await page.locator('#reference .hg-delivery-file').screenshot({path:resolve(output,'delivery-reference.png'),animations:'disabled'});
 await local.locator('.hg-delivery-file').screenshot({path:resolve(output,'delivery-single.png'),animations:'disabled'});
 await page.locator('#cloud .hg-delivery-file').screenshot({path:resolve(output,'delivery-cloud.png'),animations:'disabled'});
 await page.locator('#multiple .hg-deliveries').screenshot({path:resolve(output,'delivery-multiple.png'),animations:'disabled'});
 await local.getByRole('button',{name:'打开选项'}).click();
 await page.getByRole('menuitem',{name:'打开所在文件夹'}).waitFor();
 await page.locator('.ant-dropdown').evaluate(element => {
  for (const animation of element.getAnimations({subtree:true})) animation.finish();
 });
 const screenshotStyle = await page.addStyleTag({content:'#reference,#cloud,#multiple,nav,#preview{visibility:hidden}'});
 const cardBounds = await local.locator('.hg-delivery-file').boundingBox();
 const menuBounds = await page.getByRole('menu').boundingBox();
 const menuLeft = Math.min(cardBounds.x,menuBounds.x);
 const menuTop = Math.min(cardBounds.y,menuBounds.y);
 await page.screenshot({path:resolve(output,'delivery-open-menu.png'),animations:'disabled',clip:{
  x:Math.max(0,menuLeft-8),y:Math.max(0,menuTop-8),
  width:Math.max(cardBounds.x+cardBounds.width,menuBounds.x+menuBounds.width)-menuLeft+16,
  height:Math.max(cardBounds.y+cardBounds.height,menuBounds.y+menuBounds.height)-menuTop+16,
 }});
 await screenshotStyle.evaluate(element=>element.remove());
 await page.getByRole('menuitem',{name:'Alternate',exact:true}).click();
 await local.getByRole('status').filter({hasText:'已打开文件'}).waitFor();
 assert.deepEqual(native,['alternate']);
 await local.getByRole('button',{name:'使用 Editor 打开',exact:true}).click();
 await local.getByRole('status').filter({hasText:'已打开文件'}).waitFor();
 await local.getByRole('button',{name:'打开选项'}).click();
 await page.getByRole('menuitem',{name:'打开所在文件夹'}).click();
 await local.getByRole('status').filter({hasText:'已显示文件'}).waitFor();
 assert.deepEqual(native,['alternate','/Reports/北极星周报.pdf','/Reports']);
 assert.ok(requests.some(url=>url.includes('/applications?hg_target=local')));
 failNextOpen = true;
 await local.getByRole('button',{name:'使用 Editor 打开',exact:true}).click();
 await local.getByRole('status').filter({hasText:'无法打开文件，请重试'}).waitFor();
 await local.getByRole('button',{name:'使用 Editor 打开',exact:true}).click();
 await local.getByRole('status').filter({hasText:'已打开文件'}).waitFor();
 assert.deepEqual(await page.evaluate(()=>window.deliveryPreviews),[]);
 await local.getByRole('button',{name:'预览文件：北极星周报.pdf'}).click();
 assert.deepEqual(await page.evaluate(()=>window.deliveryPreviews),['report']);
 const multiple = page.locator('#multiple');
 assert.equal(await multiple.locator('[data-presented-file]').count(),4);
 await multiple.getByRole('button',{name:'显示全部 6 个文件'}).click();
 assert.equal(await multiple.locator('[data-presented-file]').count(),6);
 await multiple.getByRole('button',{name:'预览文件：图片.png'}).click();
 assert.deepEqual(await page.evaluate(()=>window.deliveryPreviews),['report','multi-0']);
 await multiple.getByRole('button',{name:'收起交付文件'}).click();
 assert.equal(await multiple.locator('[data-presented-file]').count(),4);
 assert.ok(requests.some(url => url.startsWith('/api/files/office/preview?') && url.includes('hg_target=local')));
 const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#cloud').getByRole('link',{name:'下载 云端周报.pdf',exact:true}).click()]);
 assert.equal(download.suggestedFilename(),'云端周报.pdf');
 assert.equal(await download.failure(), null);
 assert.ok(requests.includes('/api/files/cloud') || downloadRequests.includes('/api/files/cloud'));
 assert.ok(!requests.some(url => url.startsWith('/api/files/cloud?hg_target')));
 for (const kind of ['pdf','image','text','project']) {
  const expectedPath = kind === 'project' ? '/api/v1/projects/local-project/local-files/raw/preview' : '/api/files/' + kind;
  const [request] = await Promise.all([page.waitForRequest(req => new URL(req.url()).pathname === expectedPath),
   page.getByRole('button',{name:kind + ' preview',exact:true}).click()]);
  assert.equal(new URL(request.url()).searchParams.get('hg_target'),'local');
  if (kind === 'text') await page.getByText('本机预览成功',{exact:true}).waitFor();
 }
 assert.deepEqual(errors,[]);
 await page.screenshot({path:resolve(output,'delivery-desktop.png'),fullPage:true,animations:'disabled'});
 const grid = page.locator('#multiple .hg-deliveries-grid');
 assert.equal(await grid.evaluate(element=>getComputedStyle(element).gridTemplateColumns.split(' ').length),2);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await grid.evaluate(element=>getComputedStyle(element).gridTemplateColumns.split(' ').length),1);
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.screenshot({path:resolve(output,'delivery-mobile.png'),fullPage:true,animations:'disabled'});
 await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
 await page.waitForFunction(()=>getComputedStyle(document.querySelector('#local .hg-delivery-file')).backgroundColor==='rgb(33, 33, 35)');
 assert.deepEqual(await colors(),{background:'rgb(33, 33, 35)',border:'rgba(255, 255, 255, 0.06)',text:'rgb(249, 250, 251)',secondary:'rgb(173, 178, 184)'});
 await page.screenshot({path:resolve(output,'delivery-dark.png'),fullPage:true,animations:'disabled'});
 console.log('browser artifact actions: native local menu, labelled cloud download, preview, retries and layout passed');
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
