import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {chromium} from 'playwright';

const output=resolve('node_modules/.tmp/plugin-login');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {PluginDetailView} from './src/components/catalog/PluginDetails';
import {useDeploymentModeStore} from './src/stores/deploymentModeStore';
import {setHybridDual,setCapabilityPlaneReady} from './src/api';
const root=createRoot(document.getElementById('root'));
window.show=({mode='dual',connection='yida',ready=true})=>{
 useDeploymentModeStore.setState({isDesktop:!!mode,provisionMode:mode,localReady:ready});
 setHybridDual(mode==='dual');setCapabilityPlaneReady(ready);
 const detail={install_id:'demo@owner',slug:'demo',name:'测试插件',version:'1',
 description:'demo',skills:[],mcp:[],connection,is_global:true};
 root.render(<PluginDetailView key={mode+connection+ready} pluginDetail={detail}
 navDir={null} backToList={()=>{}} isCE={false} handleToggle={async()=>{}}
 handleUninstall={async()=>{}} openComponent={()=>{}}/>);
};
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),
bundle:true,format:'esm',jsx:'automatic',loader:{'.css':'empty'},define:{'import.meta.env':'{}'}});
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
const page=await browser.newPage();
page.setDefaultTimeout(10000);
const requests=[],errors=[];
let loggedIn=false;
page.on('pageerror',e=>errors.push(e.message));
await page.addInitScript(()=>localStorage.setItem('jx_lang','zh-CN'));
await page.route('http://plugin.test/**',async route=>{
 const req=route.request(),url=new URL(req.url());
 if(url.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
 if(url.pathname.startsWith('/api/')){
  requests.push({path:url.pathname,headers:req.headers(),method:req.method()});
  const provider=url.pathname.split('/')[4];
  let data={status:loggedIn?'connected':'disconnected'};
  if(url.pathname.endsWith('/login')) data={status:'pending',qr_data_uri:'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>',verification_url:'https://auth.example',user_code:'123'};
  if(url.pathname.endsWith('/connect')){loggedIn=true;data={status:'connected'};}
  if(url.pathname.endsWith('/poll')){loggedIn=true;data={status:'connected'};}
  if(url.pathname.endsWith('/disconnect')){loggedIn=false;data={status:'disconnected'};}
  if(provider==='email')data={...data,email_address:'user@example.test'};
  return route.fulfill({json:{code:200,data}});
 }
 return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="root"></div><script type="module" src="/fixture.js"></script>'});
});
const integrationRequests=()=>requests.filter(r=>r.path.includes('/integrations/'));
async function show(options){
 requests.length=0;loggedIn=false;
 await page.evaluate(o=>window.show(o),options);
}
try{
 await page.goto('http://plugin.test/');
 await page.waitForFunction(()=>!!window.show);
 for(const connection of ['yida','lark','dingtalk','email']){
  await Promise.all([page.waitForResponse(r=>r.url().includes('/integrations/') && r.url().includes('/status')),show({connection,mode:'dual'})]);
  assert.equal(integrationRequests()[0].headers['x-hugagent-target'],'local');
  await Promise.all([page.waitForResponse(r=>r.url().includes('/integrations/') && r.url().includes('/status')),page.locator('.ant-segmented-item').filter({hasText:'云端'}).click()]);
  assert.equal(integrationRequests().at(-1).headers['x-hugagent-target'],undefined);
 }
 for(const mode of ['local_only','dual','cloud_only']){
  await show({mode,connection:'yida'});
  await page.getByRole('button',{name:/连接宜搭账号/}).click();
  await page.getByText('已连接',{exact:true}).waitFor();
  const calls=integrationRequests();
  assert.ok(calls.some(r=>r.path.endsWith('/login')));
  assert.ok(calls.some(r=>r.path.endsWith('/poll')));
  for(const r of calls) assert.equal(r.headers['x-hugagent-target'],mode==='cloud_only'?undefined:'local');
  await page.getByRole('button',{name:/断开连接/}).click();
  await page.getByRole('button',{name:/^断\s*开$/}).click();
  await page.getByText('未连接',{exact:true}).waitFor();
 }
 for(const [connection,label] of [['lark','连接飞书账号'],['dingtalk','连接钉钉账号']]){
  await show({mode:'dual',connection});
  await page.getByRole('button',{name:new RegExp(label)}).click();
  await page.getByText('已连接',{exact:true}).waitFor();
  assert.ok(integrationRequests().some(r=>r.path.endsWith('/poll')));
  assert.ok(integrationRequests().every(r=>r.headers['x-hugagent-target']==='local'));
 }
 await show({mode:'dual',connection:'email'});
 await page.getByLabel('邮箱地址',{exact:true}).fill('user@example.test');
 await page.getByLabel('授权码 / 密码',{exact:true}).fill('fixture-app-password');
 await page.locator('.jx-conn-form').getByRole('button',{name:/绑定邮箱/}).click();
 await page.getByText('已连接',{exact:true}).waitFor();
 assert.ok(integrationRequests().some(r=>r.path.endsWith('/connect')));
 assert.ok(integrationRequests().every(r=>r.headers['x-hugagent-target']==='local'));
 await show({mode:'dual',connection:'yida'});
 await page.getByRole('button',{name:/连接宜搭账号/}).waitFor();
 await page.screenshot({path:resolve(output,'local-login.png')});
 await show({mode:'dual',ready:false});
 await page.getByText('本机服务尚未就绪，请稍候重试。',{exact:true}).waitFor();
 assert.equal(integrationRequests().length,0,'local connection must never fall back to cloud');
 assert.deepEqual(errors,[]);
 console.log('Plugin account buttons, login lifecycle and local/cloud routing passed');
}finally{await browser.close();}
