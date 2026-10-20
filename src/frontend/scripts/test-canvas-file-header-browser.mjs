import { createServer } from 'node:http';

import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output=resolve('node_modules/.tmp/canvas-file-header');
await mkdir(output,{recursive:true});
await build({stdin:{contents:"\nimport React from 'react';\nimport {createRoot} from 'react-dom/client';\nimport {RightSidebarPanel} from './src/components/canvas/RightSidebarPanel';\nimport {useCanvasStore} from './src/stores/canvasStore';\nimport {useDeploymentModeStore} from './src/stores/deploymentModeStore';\nimport './src/styles/variables.css';import './src/styles/canvas.css';\nwindow.canvas=useCanvasStore;\nfunction open(local,space=false,denied=false){\n useCanvasStore.getState().resetSidebar();\n useDeploymentModeStore.setState({isDesktop:local,provisionMode:local?'local_only':'cloud_only',activeLocal:local});\n useCanvasStore.getState().openCanvas({file_id:denied?'denied':local?'native':'cloud',name:'报告.txt',\n url:space?'/v1/myspace/content?space=personal&path=项目/报告.txt':'/files/'+(denied?'denied':local?'native':'cloud'),\n origin:local?'local':'cloud',...(space?{space:{id:'personal',name:'项目资料',kind:'personal',path:'项目/报告.txt',writable:true}}:{})});\n}\nfunction Fixture(){\n return <><nav><button onClick={()=>open(false)}>Cloud</button><button onClick={()=>open(true)}>Native</button>\n <button onClick={()=>open(false,true)}>Space</button><button onClick={()=>open(false,false,true)}>Denied</button>\n <button onClick={()=>document.documentElement.dataset.theme='dark'}>Dark</button></nav><main><RightSidebarPanel/></main></>;\n}\nopen(false);\ncreateRoot(document.getElementById('root')).render(<Fixture/>);\n",resolveDir:process.cwd(),loader:'tsx'},bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},
external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'},outfile:resolve(output,'fixture.js')});
let server;
let page;
const browser=await chromium.launch({headless:true});
try{
 page=await browser.newPage({viewport:{width:1120,height:800}});
 page.setDefaultTimeout(10000);
 const errors=[],opens=[];
let revision=1,appState='ready';
 const nativePath=['C:','SynologyDrive','一个很长的项目资料目录'.repeat(12),'报告.txt'].join(String.fromCharCode(92));
 page.on('pageerror',error=>errors.push(error.message));
 server=createServer(async(request,response)=>{
  try {
   const chunks=[];for await(const chunk of request)chunks.push(chunk);
   const body=Buffer.concat(chunks).toString();
   const route={
    request:()=>({url:()=>new URL(request.url,'http://127.0.0.1').toString(),method:()=>request.method,postDataJSON:()=>JSON.parse(body)}),
    fulfill:async result=>{
     response.writeHead(result.status||200,{'Content-Type':result.contentType||'application/json',...result.headers});
     response.end(result.json?JSON.stringify(result.json):result.body);
    },
   };
  const path=new URL(route.request().url()).pathname;
  if(path==='/fixture.js'||path==='/fixture.css')return route.fulfill({body:await readFile(resolve(output,path.slice(1))),contentType:path.endsWith('.js')?'text/javascript':'text/css'});
  if(path.endsWith('/denied/location'))return route.fulfill({status:403,json:{detail:'Forbidden'}});
  if(path.endsWith('/local-path'))return route.fulfill({json:{path:nativePath}});
  if(path.endsWith('/location'))return route.fulfill({json:{kind:'personal',root_name:null,relative_path:'资料/年度报告/报告.txt'}});
  if(path.endsWith('/applications') && appState==='failed')return route.fulfill({status:503,json:{detail:'Unavailable'}});
  if(path.endsWith('/applications') && appState==='empty')return route.fulfill({json:{applications:[],default_id:null,path:nativePath}});
  if(path.endsWith('/applications'))return route.fulfill({json:{applications:[{id:'editor',name:'Editor',icon:null},{id:'viewer',name:'Viewer',icon:null}],default_id:'editor',path:nativePath}});
  if(path.endsWith('/open')){opens.push(route.request().postDataJSON());return route.fulfill({json:{ok:true}});}
  if(path.endsWith('/text'))return route.fulfill({json:{code:0,data:{content:'revision '+revision,revision:String(revision),size:12,can_edit:true}}});
  if(path==='/api/files/cloud'||path==='/api/files/native')return route.fulfill({contentType:'text/plain',headers:{'Content-Disposition':'attachment; filename="report.txt"'},body:'download bytes'});
  if(path.startsWith('/icons/'))return route.fulfill({contentType:'image/svg+xml',body:await readFile(resolve('public',path.slice(1)))});
  if(path.startsWith('/api'))return route.fulfill({json:{code:0,data:[]}});
  return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;background:var(--color-bg-container);color:var(--color-text);font:13px Arial}nav{height:30px}main{height:calc(100vh - 30px);width:100%;display:flex}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
  }catch(error){response.writeHead(500);response.end(String(error));}
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 await page.goto('http://127.0.0.1:'+server.address().port+'/');
 const pathLabel=page.locator('[data-file-path]');
 await page.waitForFunction(()=>document.querySelector('[data-file-path]')?.getAttribute('title')==='我的空间/资料/年度报告/报告.txt');
 assert.equal((await page.locator('.jx-canvasTabs--strip').boundingBox()).height,38);
 assert.equal((await page.getByRole('toolbar',{name:'文件路径与操作'}).boundingBox()).height,38);
 assert.equal(await page.locator('.chrome-tab-background').count(),0);
 assert.equal(await page.locator('.jx-canvas-fileName').count(),0);
 const download=page.waitForEvent('download');
 await page.getByRole('link',{name:'下载 报告.txt',exact:true}).click();
 const downloaded=await download;
 assert.equal(downloaded.suggestedFilename(),'report.txt');
 assert.equal(await downloaded.failure(),null);
 assert.equal(await readFile(await downloaded.path(),'utf8'),'download bytes');
 await page.screenshot({animations:'disabled',path:resolve(output,'cloud.png')});
 await page.getByRole('button',{name:'Native',exact:true}).click();
 await page.getByRole('button',{name:'使用 Editor 打开',exact:true}).waitFor();
 await page.waitForFunction(path=>document.querySelector('[data-file-path]')?.getAttribute('title')===path,nativePath);
 await page.getByRole('button',{name:'使用 Editor 打开',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.hg-delivery-split')?.getAttribute('data-state')==='idle');
 assert.equal(opens.length,1);assert.equal(opens[0].action,'open');
 await page.getByRole('button',{name:'打开选项',exact:true}).click();
 await page.getByRole('menuitem',{name:'Viewer',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.hg-delivery-split')?.getAttribute('data-state')==='idle');
 assert.equal(opens[1].application,'viewer');
 await page.getByRole('textbox',{name:'文件内容',exact:true}).fill('dirty buffer');
 assert.equal(await page.getByRole('button',{name:'使用 Editor 打开',exact:true}).isEnabled(),false);
 await page.getByRole('button',{name:'刷新预览',exact:true}).click();
 await page.getByRole('dialog').waitFor();
 await page.getByRole('button',{name:/取.*消/}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});
 assert.equal(await page.getByRole('textbox',{name:'文件内容',exact:true}).inputValue(),'dirty buffer');
 revision=2;
 await page.getByRole('button',{name:'刷新预览',exact:true}).click();
 await page.getByRole('button',{name:'放弃修改',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('textarea')?.value==='revision 2');
 await page.screenshot({animations:'disabled',path:resolve(output,'native.png')});
 await page.getByRole('button',{name:'Dark',exact:true}).click();
 await page.setViewportSize({width:420,height:800});
 await page.screenshot({animations:'disabled',path:resolve(output,'native-dark-narrow.png')});
 assert.equal(await pathLabel.getAttribute('data-path-clipped'),'');
 const pathBox=await pathLabel.boundingBox();const actionsBox=await page.locator('.jx-fileHeader-actions').boundingBox();
 assert.ok(pathBox.x+pathBox.width<=actionsBox.x+1);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.getByRole('button',{name:'Space',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[data-file-path]')?.getAttribute('title')==='我的空间/项目资料/项目/报告.txt');
 await page.getByRole('button',{name:'Denied',exact:true}).click();
 await page.getByRole('button',{name:'重试获取文件位置',exact:true}).waitFor();
 assert.equal(await pathLabel.getAttribute('title'),'报告.txt');
 appState='empty';
 await page.getByRole('button',{name:'Native',exact:true}).click();
 await page.getByRole('button',{name:'打开所在文件夹',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.hg-delivery-split')?.getAttribute('data-state')==='idle');
 assert.equal(opens.at(-1).action,'reveal');
 appState='failed';
 await page.getByRole('button',{name:'Native',exact:true}).click();
 await page.getByRole('button',{name:'打开选项',exact:true}).click();
 appState='ready';
 await page.getByRole('menuitem',{name:'重新读取关联应用',exact:true}).click();
 await page.getByRole('button',{name:'使用 Editor 打开',exact:true}).waitFor();
 assert.deepEqual(errors,[]);
 console.log('File header: DSH geometry, logical space paths, cloud download, native application/default/menu, dirty refresh guards, long path, dark/narrow and permission errors passed');
}catch(error){await page.screenshot({path:resolve(output,'failure.png')});throw error;}finally{await browser.close();if(server)await new Promise(resolve=>server.close(resolve));}
