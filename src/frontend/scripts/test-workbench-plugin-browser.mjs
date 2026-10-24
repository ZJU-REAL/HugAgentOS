import {workbenchTestPython} from './workbench-test-python.mjs';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawn,spawnSync} from 'node:child_process';
import {chromium} from 'playwright';
const root=resolve('../..'),scratch=resolve(root,'.scratch/workbench-07');
const output=resolve(scratch,'browser');
await mkdir(output,{recursive:true});
const env={...process.env,PYTHONPATH:'src/backend',AUTH_MODE:'session',SESSION_STORE:'memory',SESSION_COOKIE_SECURE:'false',
 DATABASE_URL:'sqlite:///'+resolve(scratch,'browser-'+Date.now()+'.db'),STORAGE_PATH:resolve(scratch,'storage'),
 TMPDIR:resolve(scratch,'tmp'),WB07_FRONTEND:output};
const python=workbenchTestPython(root);
const origin='http://wb07.test:5217';
const serviceOrigin='http://127.0.0.1:5217';
let cookie='';
await build({stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {createBrowserRouter,RouterProvider,useLocation} from 'react-router';
import {bindRouter} from './src/routing/navigation';
import {WorkspaceApplication} from './src/workbench/WorkspaceApplication';
import AppCenterPanel from './src/components/docs/AppCenterPanel';
import {useAuthStore} from './src/stores/authStore';
import {useUIStore} from './src/stores/uiStore';
import {reloadDirectory} from './src/workbench/catalog';
import {attachWorkspaceBridge} from './src/workbench/workspaceBridge';
useAuthStore.setState({authUser:{user_id:'wb07',allowed_apps:null}});
window.fixture={probe:(html,delay)=>new Promise(resolve=>{const frame=document.createElement('iframe');frame.id='protocol-probe';frame.sandbox='allow-scripts';frame.srcdoc=html;document.body.append(frame);setTimeout(()=>{const bridge=attachWorkspaceBridge(frame,{protocolVersion:'1.0.0',theme:'light',locale:'en',capabilities:['host.info'],context:{applicationKey:'probe',installId:'probe',packageRevision:'1'}},()=>resolve('ready'),code=>resolve(code));window.fixture.closeProbe=()=>{bridge.dispose();frame.remove();};},delay);}),reload:reloadDirectory,theme:()=>useUIStore.setState({themeMode:'dark'}),
 switchUser:async()=>{await fetch('/fixture/session/other',{method:'POST'});useAuthStore.setState({authUser:{user_id:'other',allowed_apps:null}})}};
function Page(){const location=useLocation();return location.pathname.startsWith('/apps/')?<WorkspaceApplication/>:<AppCenterPanel/>}
const router=createBrowserRouter([{path:'*',element:<Page/>}]);bindRouter(router);
createRoot(document.getElementById('root')).render(<RouterProvider router={router}/>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),
 bundle:true,format:'esm',jsx:'automatic',loader:{'.css':'empty'},
 define:{'import.meta.env':'{}'}});
await writeFile(resolve(output,'index.html'),'<div id="root"></div><script type="module" src="/fixture.js"></script>');
// Independently installed packed SDK, with no host source or React dependency.
const consumer=resolve(scratch,'consumer');
await mkdir(consumer,{recursive:true});
await writeFile(resolve(consumer,'package.json'),'{"private":true,"type":"module"}');
const pack=spawnSync('npm',['pack','--json','--pack-destination',consumer],{cwd:resolve(root,'packages/workbench-sdk'),encoding:'utf8'});
assert.equal(pack.status,0,pack.stdout+pack.stderr);
const archive=JSON.parse(pack.stdout)[0].filename;
const install=spawnSync('npm',['install','--ignore-scripts','--offline',resolve(consumer,archive)],{cwd:consumer,encoding:'utf8'});
assert.equal(install.status,0,install.stdout+install.stderr);
await writeFile(resolve(consumer,'page.js'),`
import {connect} from '@hugagent/workbench-sdk';
(async()=>{
const c=await connect();
window.connection=c;
document.querySelector('h1').textContent='SDK ready';
document.querySelector('p').textContent=JSON.stringify(await c.request('host.info'));
c.onUpdate(info=>{document.querySelector('p').textContent=JSON.stringify(info);});
try{await c.request('native.invoke')}catch(e){document.querySelector('aside').textContent=e.message;}
window.addEventListener('unload',()=>c.dispose());
})();
`);
await build({entryPoints:[resolve(consumer,'page.js')],outfile:resolve(consumer,'page.bundle.js'),bundle:true,format:'iife',target:'es2022'});

function zip(html,entry='web/index.html'){
 const script="from tests.features.workbench.workbench_plugin_support import package; import sys; sys.stdout.buffer.write(package(html=sys.argv[1],entry=sys.argv[2],assets={'web/page.js':open(sys.argv[3]).read(),'web/style.css':'h1{color:rgb(1,2,3)}'}))";
 const r=spawnSync(python,['-c',script,html,entry,resolve(consumer,'page.bundle.js')],{cwd:root,env,maxBuffer:2**24});
 assert.equal(r.status,0,r.stderr?.toString() || String(r.error));return r.stdout;
}
let server,log='';
async function login(){
 const result=await fetch(serviceOrigin+'/fixture/session',{method:'POST'});
 cookie=result.headers.get('set-cookie').split(';')[0];
 const split=cookie.indexOf('=');
 await page.context().addCookies([{name:cookie.slice(0,split),value:cookie.slice(split+1),url:origin,httpOnly:true,sameSite:'Lax'}]);
}
async function start(){
 server=spawn(python,['-m','uvicorn','tests.features.workbench.workbench_routing_http_fixture:app','--host','127.0.0.1','--port','5217'],{cwd:root,env,stdio:['ignore','pipe','pipe']});
 server.stdout.on('data',b=>log+=b);server.stderr.on('data',b=>log+=b);
 for(let n=0;n<100;n++){if(server.exitCode!==null)throw Error(log);
  try{if((await fetch(serviceOrigin+'/ready')).ok)return;}catch{}
  await new Promise(r=>setTimeout(r,100));}throw Error(log);
}
async function stop(){if(server?.exitCode===null){const done=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await done;}}
async function upload(html,entry){
 const form=new FormData();form.append('file',new Blob([zip(html,entry)]),'plugin.zip');
 const response=await fetch(serviceOrigin+'/api/v1/plugins/import',{method:'POST',body:form,headers:{Cookie:cookie}});
 assert.equal(response.status,201,await response.clone().text());
 return (await response.json()).data.install_id;
}
const browser=await chromium.launch({headless:true,args:['--host-resolver-rules=MAP wb07.test 127.0.0.1','--no-proxy-server']});
const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
page.on('console',m=>{if(m.type()==='error')log+='\nCONSOLE: '+m.text();});
const html='<h1>Loading</h1><p></p><aside></aside><link rel="stylesheet" href="style.css"><script type="module" src="page.js"></script>';
try{
 await start();await login();
 const installed=await upload(html);
 const directory=await fetch(serviceOrigin+'/api/v1/workbench/catalog',{headers:{Cookie:cookie}});
 const pluginKey=(await directory.json()).data.items.find(i=>i.binding?.installId===installed).applicationKey;
 const preferenceUrl=serviceOrigin+'/api/v1/workbench/me/navigation-preferences';
 const pinned=await fetch(preferenceUrl,{method:'PUT',headers:{Cookie:cookie,'Content-Type':'application/json'},
  body:JSON.stringify({revision:0,pinned:[pluginKey],order:[pluginKey]})});
 assert.equal(pinned.status,200,await pinned.clone().text());
 assert.deepEqual((await pinned.json()).data.pinned,[pluginKey]);
 await page.goto(origin);
 assert.equal(await page.evaluate(()=>window.isSecureContext),false);
 assert.equal(await page.evaluate(()=>typeof crypto.randomUUID),'undefined');
 assert.equal(await page.locator('iframe').count(),0,'unopened pages must not run');
 await page.getByRole('button',{name:'SDK reports',exact:true}).click();
 const frame=page.frameLocator('iframe');
 await frame.getByRole('heading',{name:'SDK ready'}).waitFor();
 await frame.locator('p').filter({hasText:'installId'}).waitFor();
 const info=JSON.parse(await frame.locator('p').innerText());
 assert.equal(info.context.installId,installed);
 assert.deepEqual(Object.keys(info.context).sort(),['applicationKey','installId','packageRevision']);
 await frame.locator('aside').filter({hasText:'unknown_method'}).waitFor();
 assert.equal(await frame.locator('aside').innerText(),'unknown_method');
 const limits=await page.frames()[1].evaluate(async()=>{
  const c=window.connection;
  const cancelled=new AbortController();cancelled.abort();
  const outcome=p=>p.then(()=>'ok',e=>e.message);
  const cancellation=await outcome(c.request('host.info',{},cancelled.signal));
  const size=await outcome(c.request('host.info',{text:'x'.repeat(300000)}));
  const concurrency=await Promise.all(Array.from({length:9},()=>outcome(c.request('host.info'))));
  const rate=[];for(let n=0;n<31;n++)rate.push(await outcome(c.request('host.info')));
  return {cancellation,size,concurrency,rate};
 });
 assert.equal(limits.cancellation,'cancelled');assert.equal(limits.size,'message_too_large');
 assert.equal(limits.concurrency.filter(x=>x==='concurrency_limit').length,1);
 assert.ok(limits.rate.includes('rate_limited'));
 assert.equal(await page.locator('iframe').getAttribute('sandbox'),'allow-scripts');
 assert.equal(await page.frames()[1].evaluate(()=>{try{void parent.localStorage;return 'accessible'}catch{return 'blocked'}}),'blocked');
 await page.evaluate(()=>window.fixture.theme());
 await frame.locator('p').filter({hasText:'"theme":"dark"'}).waitFor();
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 await page.waitForURL('**/app-center');
 await page.waitForFunction(()=>!document.querySelector('iframe'));
 assert.equal(await page.locator('iframe').count(),0);
 // Package assets survive a service restart and are independent of upload temp paths.
 await stop();await start();await login();await page.reload();
 const restored=await fetch(preferenceUrl,{headers:{Cookie:cookie}});
 assert.deepEqual((await restored.json()).data.pinned,[pluginKey]);
 await page.getByRole('button',{name:'SDK reports',exact:true}).click();
 await frame.getByRole('heading',{name:'SDK ready'}).waitFor();
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 await upload('<h1>Never ready</h1>');
 await page.evaluate(()=>window.fixture.reload());
 await page.getByRole('button',{name:'SDK reports',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'插件页面未能加载'}).waitFor({timeout:12000});
 await upload(html);await page.getByRole('button',{name:'重试',exact:true}).click();
 await frame.getByRole('heading',{name:'SDK ready'}).waitFor();
 await fetch(serviceOrigin+'/api/v1/plugins/installed/'+encodeURIComponent(installed)+'/enable',{
  method:'PATCH',headers:{'Content-Type':'application/json',Cookie:cookie},body:'{"enabled":false}'});
 await page.evaluate(()=>window.fixture.reload());
 await page.waitForFunction(()=>!document.querySelector('iframe'));
 await fetch(serviceOrigin+'/api/v1/plugins/installed/'+encodeURIComponent(installed)+'/enable',{
  method:'PATCH',headers:{'Content-Type':'application/json',Cookie:cookie},body:'{"enabled":true}'});
 await page.evaluate(()=>window.fixture.reload());
 assert.ok(page.url().includes('/apps/'),'reenabling restores the same deep link');
 await frame.getByRole('heading',{name:'SDK ready'}).waitFor();
 await page.evaluate(()=>window.fixture.switchUser());
 await page.waitForFunction(()=>!document.querySelector('iframe'));
 const probeHtml='<h1>Loading</h1><p></p><aside></aside><script>'+(await readFile(resolve(consumer,'page.bundle.js'),'utf8'))+'</script>';
 assert.equal(await page.evaluate(html=>window.fixture.probe(html,500),probeHtml),'ready');
 const probe=page.frameLocator('#protocol-probe');
 await probe.locator('p').filter({hasText:'installId'}).waitFor();
 assert.equal(JSON.parse(await probe.locator('p').innerText()).locale,'en');
 await page.evaluate(()=>window.fixture.closeProbe());
 assert.deepEqual(errors,[]);
 console.log('Real HTTP + Chromium: install, isolated readiness, bounded methods, theme, close, restart, timeout/retry, disable, account disposal passed');
}finally{await browser.close();await stop();await writeFile(resolve(scratch,'browser-server.log'),log);}
