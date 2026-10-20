import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';

const output=resolve('node_modules/.tmp/plugin-upgrade');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {PluginDetailView} from './src/components/catalog/PluginDetails';
import {PluginUpgradeCard} from './src/workbench/PluginUpgradeCard';
import {useDeploymentModeStore} from './src/stores/deploymentModeStore';
import {setCapabilityPlaneReady,setHybridDual} from './src/api';
const root=createRoot(document.getElementById('root'));
window.fixture={show({installId,mode='dual',ready=true,admin=false}){
 useDeploymentModeStore.setState({isDesktop:true,provisionMode:mode,localReady:ready});
 setHybridDual(mode==='dual');setCapabilityPlaneReady(ready);
 const detail={install_id:installId,name:'交互可视化',slug:'visualize',version:'1.0.0',
  description:'生成可预览的 HTML',source:'local',is_global:false,category:'信息处理',
  skills:[{skill_id:'visualize',name:'visualize',description:'Create visualizations',
   enabled:true,files:[]}],mcp:[]};
 root.render(<div key={installId+mode+ready+admin}>{admin
  ? <PluginUpgradeCard installId={installId} token="fixture-admin"/>
  : <PluginDetailView pluginDetail={detail} navDir={null} backToList={()=>{}}
     isCE={false} handleToggle={async()=>{}} handleUninstall={async()=>{}}
     openComponent={()=>{}} onUpgradeChanged={async()=>{}}/>}</div>);
}};
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),
 bundle:true,format:'esm',jsx:'automatic',loader:{'.css':'empty'},
 define:{'import.meta.env':'{}'}});

const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.setDefaultTimeout(10000);
const errors=[],requests=[];
let status='current',fail=false;
page.on('pageerror',error=>errors.push(error.message));
await page.addInitScript(()=>localStorage.setItem('jx_lang','zh-CN'));
await page.route('http://plugin.test/**',async route=>{
 const request=route.request(),url=new URL(request.url());
 if(url.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',
  body:await readFile(resolve(output,'fixture.js'))});
 if(url.pathname.startsWith('/api/')){
  requests.push({path:url.pathname,method:request.method(),headers:request.headers(),
   body:request.postDataJSON()});
  if(url.pathname.includes('/plugin-upgrades/')){
   if(fail)return route.fulfill({status:503,json:{message:'unavailable'}});
   const decision=url.pathname.endsWith('/decision');
   const encoded=url.pathname.split('/plugin-upgrades/')[1].split('/')[0];
   const installId=decodeURIComponent(encoded);
   if(decision)status='current';
   return route.fulfill({json:{code:200,data:{installId,currentRevision:'old-revision',
    candidateRevision:status==='pending'?'b'.repeat(64):null,status,
    addedPermissions:status==='pending'?['module:page:file.save']:[],stateSchemaChanges:[]}}});
  }
  return route.fulfill({json:{code:200,data:{items:[]}}});
 }
 return route.fulfill({contentType:'text/html',
  body:'<meta charset="utf-8"><div id="root"></div><script type="module" src="/fixture.js"></script>'});
});
const upgradeRequests=()=>requests.filter(r=>r.path.includes('/plugin-upgrades/'));
async function show(options){
 requests.length=0;
 await page.evaluate(options=>window.fixture.show(options),options);
}
async function loaded(){
 await page.getByLabel('正在读取升级状态').waitFor({state:'hidden'});
}
try{
 await page.goto('http://plugin.test/');
 await page.waitForFunction(()=>!!window.fixture);
 for(const scenario of [
  {installId:'plugin:local:visualize',mode:'local_only',target:'local'},
  {installId:'plugin:local:visualize',mode:'dual',target:'local'},
  {installId:'visualize@owner',mode:'dual',target:'cloud'},
  {installId:'plugin:account_123:visualize',mode:'dual',target:'cloud'},
  {installId:'visualize@owner',mode:'cloud_only',target:'cloud'},
  {installId:'plugin:local:visualize',mode:'dual',target:'local',admin:true},
  {installId:'visualize@owner',mode:'dual',target:'cloud',admin:true},
 ]){
  status='current';fail=false;
  const response=page.waitForResponse(response=>response.url().includes('/plugin-upgrades/'));
  await show(scenario);
  // A current admin upgrade card intentionally renders no content.
  await response;
  await loaded();
  assert.equal(upgradeRequests().length,1,JSON.stringify(scenario));
  const request=upgradeRequests()[0];
  assert.equal(request.headers['x-hugagent-target'],scenario.target);
  assert.equal(request.headers['x-workbench-origin'],undefined,'install IDs do not supply application origin');
  assert.equal(request.headers.authorization,scenario.admin?'Bearer fixture-admin':undefined);
  assert.equal(request.path.includes('/admin/plugin-upgrades/'),!!scenario.admin);
  assert.equal(await page.getByText('无法读取升级状态。',{exact:true}).count(),0);
  if(!scenario.admin)assert.equal(await page.getByText('已启用',{exact:true}).count(),1);
 }
 // Both local and cloud approval POSTs must use the same installation namespace.
 for(const scenario of [
  {installId:'plugin:local:visualize'},
  {installId:'visualize@owner'},
  {installId:'visualize@owner',admin:true},
 ]){
  const {installId,admin}=scenario;
  status='pending';
  await show(scenario);
  await page.getByRole('button',{name:'批准升级',exact:true}).waitFor();
  await page.getByRole('button',{name:'批准升级',exact:true}).click();
  await page.waitForFunction(()=>!document.body.textContent.includes('插件升级需要审批'));
  await loaded();
  const post=upgradeRequests().find(r=>r.method==='POST');
  assert.ok(post,'approval must reach the endpoint');
  assert.equal(post.path.includes('/admin/plugin-upgrades/'),!!admin);
  assert.equal(post.headers['x-hugagent-target'],installId.startsWith('plugin:local:')?'local':'cloud');
  assert.deepEqual(post.body,{expectedRevision:'old-revision',candidateRevision:'b'.repeat(64),
   decision:'approve',resetState:false});
 }
 // Genuine server failures remain visible; retry must perform a new status read.
 fail=true;
 await show({installId:'plugin:local:retry'});
 await page.getByText('无法读取升级状态。',{exact:true}).waitFor();
 assert.equal(upgradeRequests().length,1);
 fail=false;status='current';
 await page.getByRole('button',{name:/重\s*试/}).click();
 await page.getByText('无法读取升级状态。',{exact:true}).waitFor({state:'hidden'});
 await loaded();
 assert.equal(upgradeRequests().length,2);
 // Not-ready local requests cannot fall back to cloud.
 await show({installId:'plugin:local:not-ready',ready:false});
 await page.getByText('无法读取升级状态。',{exact:true}).waitFor();
 assert.equal(upgradeRequests().length,0);
 const response=page.waitForResponse(response=>response.url().includes('/plugin-upgrades/'));
 await show({installId:'plugin:local:visualize'});
 await response;
 await loaded();
 await page.screenshot({path:resolve(output,'plugin-detail-current.png'),fullPage:true});
 assert.deepEqual(errors,[]);
 console.log('PluginDetailView: local/cloud desktop status, admin, approval, failure/retry and readiness passed (Chromium; simulated API transport, not a native installer).');
}finally{await browser.close();}
