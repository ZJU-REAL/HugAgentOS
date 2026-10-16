import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createServer } from 'node:net';
import { build } from 'esbuild';
import { chromium } from 'playwright';
const root = resolve('../..');
const output = resolve('node_modules/.tmp/overflow-browser');
const data = resolve(root, '.scratch/workbench-06/browser-' + Date.now());
const probe = createServer(); await new Promise(r => probe.listen(0, '127.0.0.1', r));
const port = probe.address().port; await new Promise(r => probe.close(r));
const origin = 'http://127.0.0.1:' + port;
await mkdir(output, { recursive: true }); await mkdir(data, { recursive: true });
await build({ stdin: { contents: `
import {createRoot} from 'react-dom/client';
import {createMemoryRouter, RouterProvider} from 'react-router';
import {bindRouter,pathForPanel} from './src/routing/navigation';
import {AppThemeProvider} from './src/AppThemeProvider';
import './src/styles/index';
import {usePanel} from './src/routing/usePanel';
import AppCenter from './src/components/docs/AppCenterPanel';
import {useMySpaceStore} from './src/stores/mySpaceStore';
import {useUIStore} from './src/stores/uiStore';
import {Sidebar} from './src/components/sidebar/Sidebar';
import {useAuthStore} from './src/stores/authStore';
import {useSidebarLayoutStore} from './src/stores/sidebarLayoutStore';
import {useDeploymentModeStore} from './src/stores/deploymentModeStore';
import {reloadDirectory} from './src/workbench/catalog';
import {reloadNavigationPreferences} from './src/workbench/preferences';
useAuthStore.setState({authUser:{user_id:'wb-reader',allowed_apps:null}});
window.__preferences={switchUser:id=>useAuthStore.setState({authUser:{user_id:id,allowed_apps:null}}),
layout:layout=>useSidebarLayoutStore.setState({layout}),reload:()=>{reloadDirectory();reloadNavigationPreferences();},
lab:lab_enabled=>useAuthStore.setState({authUser:{...useAuthStore.getState().authUser,lab_enabled}}),
environment:provisionMode=>useDeploymentModeStore.setState({provisionMode}),
collapse:value=>useUIStore.setState({siderCollapsed:value}),theme:value=>useUIStore.getState().setThemeMode(value),open:path=>router.navigate(path)};
useMySpaceStore.setState({notifUnreadCount:7});
const noop=()=>{};
function Fixture(){const panel=usePanel();return <AppThemeProvider><div style={{height:'100%',display:'flex'}}><Sidebar onNewChat={()=>router.navigate('/')}
onNewProjectChat={noop} onDeleteChat={noop} onTogglePinned={noop} onToggleFavorite={noop}
onStartRename={noop} onCommitRename={noop} onExportChat={noop} onSelectChat={noop} onSetPanel={(p,s)=>router.navigate(pathForPanel(p,s))}/>
<main style={{flex:1,overflow:'auto'}}>{panel==='app_center'?<AppCenter/>:<h1>{panel}</h1>}</main></div></AppThemeProvider>}
const router=createMemoryRouter([{path:'*',element:<Fixture/>}]);
bindRouter(router);createRoot(document.getElementById('root')).render(<RouterProvider router={router}/>);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'), bundle: true,
external: ['/loader.gif','/loader-done.png'], format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
loader: { '.css': 'css', '.svg': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
await writeFile(resolve(output, 'index.html'), '<html><head><style>#root{height:100%}</style><link rel="stylesheet" href="/fixture.css"></head><body style="margin:0;height:100vh"><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');


const http = process.argv.includes('--http');
const {readFile} = await import('node:fs/promises');
const {cpus,totalmem} = await import('node:os');
let server;
async function start(){
 server=spawn((process.env.WB_TEST_PYTHON || resolve(root, '.venv/bin/python')),
 ['-m','uvicorn','tests.workbench_overflow_http_fixture:app','--host','127.0.0.1','--port',String(port)],
 {cwd:root,env:{...process.env,PYTHONPATH:'src/backend',AUTH_MODE:'mock',CONFIG_TOKEN:'wb-overflow-config',
 DATABASE_URL:'sqlite:///'+resolve(data,'http.db'),STORAGE_PATH:resolve(data,'storage'),TMPDIR:data},stdio:['ignore','pipe','pipe']});
 let log='';server.stdout.on('data',v=>log+=v);server.stderr.on('data',v=>log+=v);
 for(let n=0;n<150;n++){if(server.exitCode!==null)throw new Error(log);try{if((await fetch(origin+'/ready')).ok)return;}catch{}await new Promise(r=>setTimeout(r,100));}throw new Error(log);
}
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:360}});
const items=Array.from({length:100},(_,n)=>({applicationKey:'external:report_'+n,revision:1,enabled:true,
declaration:{schemaVersion:'1.0.0',id:'report_'+n,title:'Report '+n,description:'Analysis '+n,navigation:{group:'Research'},
icon:n===0?{light:origin+'/color.svg',dark:origin+'/dark.svg'}:n===1?{light:origin+'/missing.svg'}:undefined,
target:{kind:'external',mode:'browser',url:origin+'/destination/'+n}},
availability:{available:true,reason:null},navigation:{defaultPinned:true,forced:false}}));
const builtin=['chat','projects','my_space','ability_center','app_center','automation','sites','lab','settings','docs'].map(id=>({
applicationKey:'builtin:'+id,revision:1,enabled:true,declaration:{schemaVersion:'1.0.0',id,title:({'my_space':'我的空间'})[id]||id,target:{kind:'builtin',componentId:id}},
availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}}));
let total=100;let writes=0;let policyRevision=0;
let prefs={revision:0,environment:'web',pinned:[...items.map(x=>x.applicationKey),'builtin:app_center','builtin:ability_center'],order:[]};
const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.addInitScript(()=>localStorage.setItem('jx_lang','zh-CN'));
await page.context().route(origin+'/**',async route=>{
 const path=new URL(route.request().url()).pathname;
 if(['/fixture.js','/fixture.css'].includes(path))return route.fulfill({contentType:path.endsWith('.css')?'text/css':'text/javascript',body:await readFile(resolve(output,path.slice(1)))});
 if(path==='/missing.svg')return route.fulfill({status:404});
 if(path==='/color.svg'||path==='/dark.svg')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="red"/></svg>'});
 if(!path.startsWith('/api/')){
  if(path.startsWith('/home/'))return route.fulfill({contentType:'image/svg+xml',body:await readFile(resolve('public',path.slice(1))).catch(()=>'<svg xmlns="http://www.w3.org/2000/svg"/>')});
  if(path.startsWith('/destination/'))return route.fulfill({contentType:'text/html',body:'<h1>Report destination</h1>'});
  return route.fulfill({contentType:'text/html',body:await readFile(resolve(output,'index.html'))});
 }
 if(path.endsWith('/me/navigation-preferences')&&route.request().method()==='PUT')writes++;
 if(http)return route.continue({headers:{...route.request().headers(),Authorization:'Bearer wb-reader'}});
 let result=[];
 if(path.endsWith('/workbench/catalog'))result={items:[...builtin,...items.slice(0,total)],policy:{revision:0,defaultPinned:[],forced:[]}};
 if(path.endsWith('/me/navigation-preferences')){
  if(route.request().method()==='PUT')prefs={...prefs,...route.request().postDataJSON(),revision:prefs.revision+1};
  result=prefs;
 }
 if(path.endsWith('/launch')){const key=decodeURIComponent(path.split('/').at(-2));result={applicationKey:key,target:items.find(x=>x.applicationKey===key).declaration.target};}
 return route.fulfill({json:{code:10000,data:result}});
});
const api=origin+'/api/v1/workbench';
const admin={'Content-Type':'application/json',Authorization:'Bearer wb-overflow-config'};
async function readPrefs(){if(!http)return prefs;return (await(await fetch(api+'/me/navigation-preferences',{headers:{Authorization:'Bearer wb-reader'}})).json()).data;}
async function matrixCount(count){
 total=count;
 if(http){const response=await fetch(api+'/admin/policy',{method:'PUT',headers:admin,
 body:JSON.stringify({revision:policyRevision,defaultPinned:[],forced:[],hidden:items.slice(count).map(x=>x.applicationKey)})});
 assert.equal(response.status,200);policyRevision=(await response.json()).data.revision;}
 await page.evaluate(()=>window.__preferences.reload());
 // A public search verifies the last entry became discoverable.
}
async function more(){await page.getByRole('button',{name:'更多',exact:true}).click();await page.getByRole('searchbox',{name:'搜索应用',exact:true}).waitFor();await page.waitForFunction(()=>document.activeElement?.closest('[role=dialog]')!==null);}
async function close(){await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'hidden'});}
async function viewport(height,zoom){
 await page.setViewportSize({width:1440,height});
 await page.evaluate(({height,zoom})=>{document.documentElement.style.zoom=String(zoom);document.body.style.height=(height/zoom)+'px';},{height,zoom});
}
async function waitAvailable(locator){
 await locator.evaluate(node=>new Promise(resolve=>{function ready(){if(!node.disabled&&node.getAttribute('aria-disabled')!=='true')resolve();else setTimeout(ready,10);}ready();}));
}
async function paintSample(locator){
 await waitAvailable(locator);
 return locator.evaluate(node=>new Promise(resolve=>{const start=performance.now();node.click();requestAnimationFrame(()=>resolve(performance.now()-start));}));
}
const cells=[];const samples=[];
try{
 if(http){
  await start();
  for(const item of items){const response=await fetch(api+'/admin/external-applications',{method:'POST',headers:admin,body:JSON.stringify({declaration:{schemaVersion:item.declaration.schemaVersion,id:item.declaration.id,title:item.declaration.title,description:item.declaration.description,target:item.declaration.target},enabled:true})});assert.equal(response.status,200);}
  const response=await fetch(api+'/me/navigation-preferences',{method:'PUT',headers:{'Content-Type':'application/json',Authorization:'Bearer wb-reader'},body:JSON.stringify({revision:0,pinned:prefs.pinned,order:[]})});assert.equal(response.status,200);
 }
 await page.goto(origin);
 await page.getByRole('button',{name:'用户菜单',exact:true}).waitFor();
 for(const count of [5,20,100]){
  await matrixCount(count);
  for(const layout of ['modern','classic','classic-mini'])for(const theme of ['light','dark'])for(const height of [360,1000]){
   const zoom=height===360?1.25:1;
   await viewport(height,zoom);
   await page.evaluate(({layout,theme})=>{window.__preferences.layout(layout==='modern'?'modern':'classic');window.__preferences.collapse(layout==='classic-mini');window.__preferences.theme(theme);},{layout,theme});
   await page.waitForTimeout(50);
   const before=(await readPrefs()).revision;
   const tools=await page.getByRole('button',{name:'用户菜单',exact:true}).boundingBox();
   const trigger=await page.getByRole('button',{name:'更多',exact:true}).boundingBox();
   if(tools.y<0||tools.y+tools.height>height+1) console.error('TOOL',tools);
   assert.ok(tools.y>=0&&tools.y+tools.height<=height+1,layout+' tools fit '+height+' zoom '+zoom);
   assert.ok(trigger.y>=0&&trigger.y+trigger.height<=height+1,layout+' More fits');
   await more();const search=page.getByRole('searchbox',{name:'搜索应用',exact:true});
   await search.fill('Analysis '+(count-1));
   const entry=page.getByRole('menuitem',{name:'Report '+(count-1),exact:true});
   await entry.waitFor();assert.equal(await entry.locator('small').last().innerText(),'外部应用 · external:report_'+(count-1)+ (http?'':' · Research'));
   await search.focus();await page.keyboard.press('ArrowDown');assert.equal(await entry.evaluate(n=>n===document.activeElement),true);
   await close();assert.equal(await page.getByRole('button',{name:'更多',exact:true}).evaluate(n=>n===document.activeElement),true,'focus restored');
   assert.equal((await readPrefs()).revision,before,'resize/theme/layout never writes pins');
   assert.equal(await page.locator('iframe').count(),0,'unopened apps have no iframe');
   cells.push({count,layout,theme,height,zoom,result:'pass'});
  }
 }
 await viewport(1000,1);await page.evaluate(()=>{window.__preferences.layout('modern');window.__preferences.collapse(false);});
 await more();await page.getByRole('searchbox',{name:'搜索应用'}).fill(http?'Analysis':'Research');
 assert.equal(await page.getByRole('menuitem').count(),100,'group search finds all entries');
 await page.getByRole('searchbox',{name:'搜索应用'}).fill('Report 0');
 if(!http){
 const color=page.getByRole('menuitem',{name:'Report 0',exact:true}).locator('img');
 await color.waitFor();assert.equal(await color.evaluate(n=>getComputedStyle(n).filter),'none','dark color asset is preserved');
 assert.ok((await color.getAttribute('src')).endsWith('/dark.svg'));
 await page.getByRole('searchbox',{name:'搜索应用'}).fill('Report 1');
 await page.getByRole('menuitem',{name:'Report 1',exact:true}).locator('.jx-applicationIconFallback').waitFor();
 }
 await page.getByRole('searchbox',{name:'搜索应用'}).fill('Report 99');
 const keyboardEntry=page.getByRole('menuitem',{name:'Report 99',exact:true});
 await keyboardEntry.focus();await page.keyboard.press('Tab');
 assert.equal(await page.getByRole('menuitemcheckbox',{name:'Report 99 固定',exact:true}).evaluate(n=>n===document.activeElement),true);
 for(let n=0;n<2;n++){await waitAvailable(page.getByRole('menuitemcheckbox',{name:'Report 99 固定',exact:true}));const previous=(await readPrefs()).revision;await page.keyboard.press('Space');
  for(let attempt=0;(await readPrefs()).revision===previous&&attempt<100;attempt++)await page.waitForTimeout(10);
  assert.equal((await readPrefs()).revision,previous+1);assert.equal(await page.getByRole('menuitemcheckbox',{name:'Report 99 固定',exact:true}).evaluate(n=>n===document.activeElement),true,'pin save keeps keyboard focus');}
 await keyboardEntry.focus();
 const popupPromise=page.waitForEvent('popup');await page.keyboard.press('Enter');
 const popup=await popupPromise;await popup.waitForURL('**/destination/99');await popup.close();
 // The app center uses the same list, source badges, preferences and opener.
 await page.evaluate(()=>window.__preferences.open('/app-center'));
 await page.locator('.jx-agentPage').getByRole('searchbox',{name:'搜索应用'}).fill('Report 99');
 const center=page.locator('.jx-agentPage');
 const popup2Promise=page.waitForEvent('popup');await center.getByRole('button',{name:'Report 99',exact:true}).click();
 const popup2=await popup2Promise;await popup2.waitForURL('**/destination/99');await popup2.close();
 assert.equal(await page.locator('.jx-moduleSidebar').count(),0,'no empty secondary sidebar');
 await more();await page.getByRole('menuitem',{name:'应用中心',exact:true}).waitFor();
 assert.equal(await page.getByRole('menuitem',{name:'应用中心',exact:true}).getAttribute('aria-current'),'page','current overflow entry is marked');
 await close();
 await page.evaluate(()=>{window.__preferences.open('/');window.__preferences.layout('classic');window.__preferences.collapse(false);});
 await page.getByRole('button',{name:'管理应用导航',exact:true}).click();
 const badgeManager=page.getByRole('region',{name:'导航偏好',exact:true});
 const badgeBefore=(await readPrefs()).revision;
 await badgeManager.getByRole('checkbox',{name:'我的空间 固定',exact:true}).click();
 for(let n=0;(await readPrefs()).revision===badgeBefore&&n<100;n++)await page.waitForTimeout(10);
 const badgeValue=await readPrefs();
 if(http){const response=await fetch(api+'/me/navigation-preferences',{method:'PUT',headers:{'Content-Type':'application/json',Authorization:'Bearer wb-reader'},body:JSON.stringify({revision:badgeValue.revision,pinned:badgeValue.pinned,order:['builtin:my_space',...badgeValue.order.filter(k=>k!=='builtin:my_space')]})});assert.equal(response.status,200);}
 else prefs={...prefs,revision:prefs.revision+1,order:['builtin:my_space',...prefs.order.filter(k=>k!=='builtin:my_space')]};
 await page.evaluate(()=>window.__preferences.reload());
 await page.getByRole('button',{name:'Close',exact:true}).click();
 await page.locator('.jx-classicNav .ant-badge-count').filter({hasText:'7'}).waitFor();
 await page.evaluate(()=>window.__preferences.layout('modern'));

 // 100 navigation handler-to-next-frame samples. HTTP completion is checked outside timing.
 for(let n=0;n<40;n++){
  samples.push({operation:'more',ms:await paintSample(page.getByRole('button',{name:'更多',exact:true}))});
  await page.getByRole('searchbox',{name:'搜索应用'}).waitFor();await close();
 }
 await more();await page.getByRole('searchbox',{name:'搜索应用'}).fill('Report 99');
 for(let n=0;n<30;n++){
  const before=(await readPrefs()).revision;
  samples.push({operation:'pin',ms:await paintSample(page.getByRole('menuitemcheckbox',{name:'Report 99 固定',exact:true}))});
  for(let attempt=0;(await readPrefs()).revision===before&&attempt<100;attempt++)await page.waitForTimeout(10);
  assert.equal((await readPrefs()).revision,before+1);
 }
 await close();
 await page.getByRole('button',{name:'管理应用导航',exact:true}).click();
 const manager=page.getByRole('region',{name:'导航偏好',exact:true});
 for(let n=0;n<30;n++){
  const before=(await readPrefs()).revision;
  samples.push({operation:'sort',ms:await paintSample(manager.getByRole('button',{name:'Report 1 '+(n%2?'下移':'上移'),exact:true}))});
  for(let attempt=0;(await readPrefs()).revision===before&&attempt<100;attempt++)await page.waitForTimeout(10);
  assert.equal((await readPrefs()).revision,before+1);
 }
 const sorted=samples.map(x=>x.ms).sort((a,b)=>a-b);const p95=sorted[Math.ceil(sorted.length*.95)-1];
 assert.ok(p95<=100,'navigation P95 <=100ms, got '+p95);
 assert.deepEqual(errors,[]);
 const report={level:http?'real workbench HTTP + isolated SQLite + Chromium':'mock API + Chromium',cells,samples,p95,writes,
 cpu:cpus()[0].model,logicalCPUs:cpus().length,memoryBytes:totalmem(),browser:browser.version(),buildMode:'esbuild bundled public UI fixture',
 scope:'UI handler to next frame; authorization/network and third-party business loading excluded; no native client claim'};
 await writeFile(resolve(data,'report.json'),JSON.stringify(report,null,2));
 await writeFile(resolve(root,'.scratch/workbench-06',http?'http-report.json':'fixture-report.json'),JSON.stringify(report,null,2));
 console.log('PASS '+report.level+': 36 matrix cells; search/source/pin/open/current; keyboard/focus; 100 operations P95='+p95.toFixed(2)+'ms; no unopened iframe');
}catch(error){await page.screenshot({path:resolve(data,'failure.png')});console.error('BOXES',await page.locator('.jx-sider,.jx-classicInner,.jx-classicFooter,.jx-classicNav,.jx-classicMini').evaluateAll(nodes=>nodes.map(n=>({className:n.className,rect:n.getBoundingClientRect().toJSON(),height:getComputedStyle(n).height}))), 'UI',(await page.locator('body').innerText()).slice(-1200),'errors',errors);throw error;}
finally{await browser.close();if(server&&server.exitCode===null){const done=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await done;}}
