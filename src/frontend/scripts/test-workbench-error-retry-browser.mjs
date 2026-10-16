import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createServer} from 'node:http';
import {chromium} from 'playwright';
// Component behavior only: controlled directory/request boundary, actual React/router/errors.
const folder=resolve('../../.scratch/standards-error-retry-fix/browser-'+Date.now());
await mkdir(folder,{recursive:true});
const fixture=`
import {createRoot} from 'react-dom/client';
import {BrowserRouter} from 'react-router';
import {WorkspaceApplication} from './src/workbench/WorkspaceApplication';
window.stats={reloads:0,gets:0,posts:0};
const mode=new URLSearchParams(location.search).get('mode');
window.mode=mode;
window.state={identity:'owned',userId:'own',loading:false,error:'',items:[]};
if(['app_disabled','permission_denied','host_incompatible'].includes(mode))window.state.items=[{
 applicationKey:'plugin:own:app',availability:{available:false,reason:mode},
 declaration:{target:{kind:'plugin',moduleId:'main'}}}];
createRoot(document.getElementById('root')).render(<BrowserRouter><WorkspaceApplication/></BrowserRouter>);
`;
await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(folder,'fixture.js'),
 bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},loader:{'.css':'empty'},
 plugins:[{name:'controlled-component-boundary',setup(b){
 b.onLoad({filter:/[\/]workbench[\/]catalog\.ts$/},()=>({contents:`
 export function useApplicationDirectory(){return window.state;}
 export async function reloadDirectory(){window.stats.reloads++;}
 `,loader:'ts'}));
 b.onLoad({filter:/[\/]workbench[\/]sourceRequest\.ts$/},()=>({contents:`
 import {ApiResponseError} from '../utils/apiError';
 export class WorkbenchSourceError extends Error {}
 export function setHostedWorkbenchOrigin(){}
 export async function workbenchFetch(){throw new Error('unused');}
 export async function workbenchRequest(path,options){
  if(options?.method==='POST'){window.stats.posts++;return {data:{diagnosticId:'b'.repeat(32)}};}
  window.stats.gets++;
  throw new ApiResponseError('synthetic_server_secret',Number(window.mode),{detail:{diagnosticId:'a'.repeat(32),message:'synthetic_server_secret'}});
 }
 `,loader:'ts',resolveDir:resolve('src/workbench')}));
 }}]});
const body=await readFile(resolve(folder,'fixture.js'));
const server=createServer((req,res)=>{res.setHeader('Content-Type',req.url==='/fixture.js'?'text/javascript':'text/html');res.end(req.url==='/fixture.js'?body:'<div id="root"></div><script type="module" src="/fixture.js"></script>');});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({headless:true});
const results=[];
let privacyText;
try{
 for(const mode of ['app_disabled','permission_denied','host_incompatible','403','404','500']){
  const page=await browser.newPage();
  try{
   await page.goto('http://127.0.0.1:'+server.address().port+'/apps/plugin%3Aown%3Aapp/?mode='+mode);
   await page.getByRole('alert').waitFor();
   const text=await page.getByRole('alert').textContent();
   assert.ok(!text.includes('synthetic_server_secret'));
   const reasons={app_disabled:'应用已停用或配置发生冲突，请重新加载',permission_denied:'没有访问权限',host_incompatible:'宿主版本不兼容'};
   if(reasons[mode])assert.ok(text.includes(reasons[mode]));
   if(mode==='403')privacyText=text;
   if(mode==='404')assert.equal(text,privacyText);
   if(/^\d+$/.test(mode))assert.equal(await page.locator('[role="alert"] code').textContent({timeout:1000}),'a'.repeat(32));
   await page.getByRole('button',{name:'重试',exact:true}).click({timeout:1000});
   if(/^\d+$/.test(mode))await page.waitForFunction(()=>window.stats.gets===2);
   await page.waitForFunction(()=>window.stats.reloads===1);
   assert.equal(await page.evaluate(()=>window.stats.posts),0);
   results.push({mode,passed:true});console.log('PASS '+mode);
  }catch(error){results.push({mode,passed:false,error:error.message});console.log('FAIL '+mode+': '+error.message);}
  finally{await page.close();}
 }
}finally{await browser.close();await new Promise(r=>server.close(r));await writeFile(resolve(folder,'results.json'),JSON.stringify(results,null,2));}
assert.ok(results.every(r=>r.passed),JSON.stringify(results));
