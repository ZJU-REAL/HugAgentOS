import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createServer } from 'node:http';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/archive-browser');
await mkdir(output, { recursive: true });
async function buildFixture(apiBase) {
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ResourceCard } from './src/components/myspace/ResourceCard';
import { CanvasPanel } from './src/components/canvas/CanvasPanel';
import { FilePreviewPane } from './src/components/file/FilePreviewPane';
import { useCanvasStore } from './src/stores/canvasStore';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { setHybridDual } from './src/api';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/myspace.css';
import './src/styles/chat.css';
import './src/styles/canvas.css';
setHybridDual(true);
useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'dual', activeLocal: false });
const items = [
 {id:'zip',file_id:'zip',name:'项目资料.zip',origin:'local',type:'document',created_at:''},
 {id:'rar',file_id:'rar',name:'历史资料.rar',origin:'cloud',type:'document',created_at:''},
 {id:'empty',file_id:'empty',name:'空文件夹.tar.gz',origin:'cloud',type:'document',created_at:''},
 {id:'retry',file_id:'retry',name:'重试.zip',origin:'cloud',type:'document',created_at:''},
];
function Fixture() {
 const [modal, setModal] = React.useState(false);
 return <main><section id="files">{items.map(item => <ResourceCard key={item.id} item={item} checked={false} anySelected={false} onCheck={()=>{}}
   onPreview={file=>{setModal(false);useCanvasStore.getState().openCanvas({...file,url:'/files/'+file.file_id})}} />)}
 <button onClick={()=>setModal(true)}>导入预览</button></section>
 <section id="preview">{modal ? <FilePreviewPane item={{...items[0],download_url:'/files/zip'}} /> : <CanvasPanel/>}</section></main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'),
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': JSON.stringify(apiBase ? {VITE_API_BASE_URL:apiBase} : {}) },
  external: ['/loader.gif', '/loader-done.png'], loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
}
const requests = [];
let retryCount = 0;
const listing = {
  format: 'zip', blocked_count: 1, entries: [
    {path:'资料/说明.txt',size:13,is_dir:false,readable:true,reason:null},
    {path:'资料/报告.docx',size:100,is_dir:false,readable:true,reason:null},
    {path:'资料/预览.pdf',size:20,is_dir:false,readable:true,reason:null},
    {path:'资料/页面.html',size:35,is_dir:false,readable:true,reason:null},
    {path:'资料/图像.png',size:68,is_dir:false,readable:true,reason:null},
    {path:'empty',size:0,is_dir:true,readable:false,reason:null},
    {path:'locked.txt',size:12,is_dir:false,readable:false,reason:'archive_encrypted'},
  ],
};
const html = '<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial;background:var(--color-bg-container);color:var(--color-text)}main{display:flex;height:100vh}#files{flex:1;padding:16px;min-width:0}#preview{width:680px;max-width:100%;height:100vh}.jx-canvas{width:100%;height:100%}.jx-canvas-dragHandle{display:none} .jx-spaceImportPreview{display:flex;flex-direction:column;height:100%}.jx-spaceImportPreview-body{display:flex;flex:1;min-height:0;overflow:auto}.jx-spaceImportPreview-frame{width:100%;min-height:380px;border:0}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>';
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  requests.push(url.pathname + url.search);
  if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
    res.writeHead(200, {'content-type': url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css'});
    res.end(await readFile(resolve(output, url.pathname.slice(1)))); return;
  }
  if (url.pathname.endsWith('/archive')) {
    if (url.pathname.includes('/rar/')) {
      res.writeHead(415, {'content-type':'application/json'}); res.end(JSON.stringify({detail:'archive_format_unsupported'})); return;
    }
    if (url.pathname.includes('/retry/') && retryCount++ === 0) {
      res.writeHead(503, {'content-type':'application/json'}); res.end(JSON.stringify({detail:'archive_read_failed'})); return;
    }
    res.writeHead(200, {'content-type':'application/json'});
    res.end(JSON.stringify({code:0,data:url.pathname.includes('/empty/') ? {...listing,entries:[listing.entries.find(entry=>entry.path==='empty')],blocked_count:0} : listing})); return;
  }
  if (url.pathname.endsWith('/archive/member')) {
    const member = url.searchParams.get('path');
    const inline = url.searchParams.get('inline');
    res.writeHead(200, {'content-type': member?.endsWith('.png') ? 'image/png' : member?.endsWith('.pdf') ? 'application/pdf' : 'text/plain',
      'content-disposition': (inline ? 'inline' : 'attachment') + "; filename*=UTF-8''" + encodeURIComponent(member.split('/').pop()) });
    res.end(member.endsWith('.png') ? Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64') :
      member.endsWith('.pdf') ? '%PDF-1.4\n%%EOF' : member.endsWith('.html') ? '<script>window.archiveExecuted=true</script>' : '压缩包预览成功'); return;
  }
  if (url.pathname.endsWith('/archive/member/preview')) {
    res.writeHead(200, {'content-type':'application/pdf'}); res.end('%PDF-1.4\n%%EOF'); return;
  }
  if (url.pathname.endsWith('.svg')) {
    res.writeHead(200, {'content-type':'image/svg+xml'}); res.end('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"/>'); return;
  }
  res.writeHead(200, {'content-type':'text/html'}); res.end(html);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
await buildFixture(process.argv.includes('--absolute-api') ? origin + '/api' : undefined);
const browser = await chromium.launch({ headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM });
try {
  const page = await browser.newPage({ acceptDownloads:true, viewport:{width:1250,height:820} });
  await page.addInitScript(() => { if (window === window.top) localStorage.setItem('jx_lang','zh-CN'); });
  const errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(origin);
  await page.locator('#files').getByText('项目资料.zip',{exact:true}).click();
  const preview = page.locator('#preview');
  await page.screenshot({path:resolve(output,'archive-first.png'),fullPage:true,animations:'disabled'});
  await preview.getByRole('button',{name:'资料',exact:true}).click();
  assert.ok(requests.some(url=>url==='/api/files/zip/archive?hg_target=local'));
  await preview.getByRole('button',{name:/说明.txt/}).click();
  await preview.getByText('压缩包预览成功',{exact:true}).waitFor();
  assert.ok(requests.some(url=>url.includes('/archive/member?') && url.includes('hg_target=local') && decodeURIComponent(url).includes('资料/说明.txt')));
  assert.equal(await preview.locator('.jx-archive-preview').getByRole('button',{name:'打开文件',exact:true}).count(),0);
  const memberDownload = preview.locator('.jx-archive-preview').getByRole('link',{name:'下载 说明.txt',exact:true}).first();
  const [download] = await Promise.all([page.waitForEvent('download'),memberDownload.click()]);
  assert.equal(download.suggestedFilename(),'说明.txt');
  assert.equal(await download.failure(),null);
  await preview.getByRole('button',{name:'返回文件列表',exact:true}).click();
  await preview.getByRole('searchbox',{name:'搜索当前目录'}).fill('报告');
  assert.equal(await preview.locator('.jx-archive-list li').count(),1);
  await preview.getByRole('button',{name:/报告.docx/}).click();
  await page.waitForRequest(req=>new URL(req.url()).pathname.endsWith('/archive/member/preview'),{timeout:2000}).catch(()=>{
    assert.ok(requests.some(url=>url.includes('/archive/member/preview?')&&url.includes('hg_target=local')));
  });
  await preview.getByRole('button',{name:'返回文件列表',exact:true}).click();
  await preview.getByRole('searchbox',{name:'搜索当前目录'}).fill('');
  await preview.getByRole('button',{name:'预览.pdf',exact:true}).click();
  const pdfFrame = preview.locator('.jx-archive-preview iframe');
  await pdfFrame.waitFor();
  const pdfUrl = new URL(await pdfFrame.getAttribute('src'), origin);
  assert.equal(pdfUrl.pathname,'/api/files/zip/archive/member');
  assert.equal(pdfUrl.searchParams.get('inline'),'true');
  assert.equal(pdfUrl.searchParams.get('hg_target'),'local');
  await preview.getByRole('button',{name:'返回文件列表',exact:true}).click();
  await preview.getByRole('button',{name:/页面.html/}).click();
  await preview.getByText('<script>window.archiveExecuted=true</script>',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.archiveExecuted),undefined);
  await preview.getByRole('button',{name:'返回文件列表',exact:true}).click();
  await preview.getByRole('button',{name:'上级目录',exact:true}).click();
  assert.equal(await preview.getByRole('button',{name:/locked.txt/}).isDisabled(),true);
  await page.screenshot({path:resolve(output,'archive-light.png'),fullPage:true,animations:'disabled'});
  await page.evaluate(()=>document.documentElement.dataset.theme='dark');
  await page.screenshot({path:resolve(output,'archive-dark.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:760,height:820});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth));
  await page.screenshot({path:resolve(output,'archive-narrow.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:1250,height:820});
  await page.locator('#files').getByText('历史资料.rar',{exact:true}).click();
  await preview.getByText(/暂不支持此压缩格式/).waitFor();
  assert.ok(!requests.some(url=>url.startsWith('/api/files/rar/archive?hg_target')));
  await page.locator('#files').getByText('空文件夹.tar.gz',{exact:true}).click();
  await preview.getByRole('button',{name:'empty',exact:true}).click();
  await preview.getByText('此目录没有文件',{exact:true}).waitFor();
  await page.locator('#files').getByText('重试.zip',{exact:true}).click();
  await preview.getByRole('button',{name:'重试',exact:true}).click();
  await preview.getByRole('button',{name:'资料',exact:true}).waitFor();
  await page.getByRole('button',{name:'导入预览',exact:true}).click();
  await preview.getByRole('button',{name:'资料',exact:true}).click();
  await preview.getByRole('button',{name:/图像.png/}).click();
  await preview.getByRole('img',{name:'图像.png',exact:true}).waitFor();
  await page.waitForFunction(()=>{const image=document.querySelector('.jx-archive-preview .jx-spaceImportPreview-image img'); return image?.complete&&image.naturalWidth>0;});
  assert.deepEqual(errors,[]);
  console.log('Archive browser: MySpace row, folders, text/image/PDF/Office previews, HTML isolation, member download, search, encryption, retry, unsupported formats, local/cloud routing and light/dark/narrow layout passed');
} finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
