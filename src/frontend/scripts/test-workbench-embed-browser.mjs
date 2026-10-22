// Real HTTP routes + real cross-origin sites. No production lifespan or data.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {createServer} from 'node:http';
import {mkdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawn} from 'node:child_process';
import {chromium} from 'playwright';

const smoke=process.argv.includes('--smoke');
const root=resolve('../..');
const scratch=resolve(root,'.scratch/workbench-14/browser-'+Date.now());
const output=resolve(scratch,'static');
const host='http://127.0.0.1:5254', external='http://localhost:5255';
await mkdir(output,{recursive:true});
await build({
  stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {useState} from 'react';
import {ExternalApplicationEditor} from './src/workbench/ExternalApplicationEditor';
import {EmbeddedExternalApplication} from './src/workbench/EmbeddedExternalApplication';
import {useApplicationDirectory, reloadDirectory} from './src/workbench/catalog';
import {useAuthStore} from './src/stores/authStore';
useAuthStore.setState({authUser:{user_id:'fixture',sso_token:'must-never-leak'}});
function Fixture(){
 const catalog=useApplicationDirectory(), [key,setKey]=useState('');
 return <><ExternalApplicationEditor token="wb14-test-config"/>
 <button onClick={reloadDirectory}>Reload catalog</button>
 {catalog.items.filter(i=>i.declaration.target.mode==='embedded').map(i=>
 <button key={i.applicationKey} onClick={()=>setKey(i.applicationKey)}>{i.applicationKey}</button>)}
 <button onClick={()=>setKey('')}>Close embed</button>
 {key && <EmbeddedExternalApplication key={key} applicationKey={key}/>}</>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`,resolveDir:process.cwd(),loader:'tsx'},
  bundle:true,format:'esm',jsx:'automatic',outfile:resolve(output,'fixture.js'),
  define:{'import.meta.env':'{}'},loader:{'.css':'empty'},
});
const html='<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>';
await writeFile(resolve(output,'index.html'),html);
await writeFile(resolve(output,'unprotected'),html);
const destinations=[];
const siteHandler=(req,res)=>{
  destinations.push(req.url);
  if(req.url==='/redirect-host'){res.writeHead(302,{Location:host});res.end();return;}
  const policy=req.url==='/xfo' ? {'X-Frame-Options':'DENY'} :
    req.url==='/csp' ? {'Content-Security-Policy':"frame-ancestors 'none'"} : {};
  if(req.url.startsWith('/forged')){
    res.writeHead(200,{'Content-Type':'text/html'});
    res.end('<script>parent.parent.postMessage({type:"hugagent.external.ready",protocolVersion:"1.0.0",nonce:decodeURIComponent(location.hash.slice(1))},'+JSON.stringify(host)+');</script>');
    return;
  }
  const wrong=req.url==='/wrong';
  const wrongOrigin=req.url==='/wrong-origin';
  const quiet=req.url==='/quiet';
  res.writeHead(200,{'Content-Type':'text/html',...policy});
  res.end(`<!doctype html><h1>External test page</h1><script>
try{localStorage.setItem('external','only-external')}catch{}
let denied=false; try{top.localStorage.getItem('host-secret')}catch{denied=true}
window.hostDenied=denied;
window.popup=window.open('about:blank','_blank');
window.addEventListener('message',event=>{
 if(event.source!==parent || event.origin!==${JSON.stringify(host)} || event.data?.type!=='hugagent.external.init')return;
 if(${quiet})return;
 if(${wrongOrigin}){const f=document.createElement('iframe');f.src='http://localhost:5256/forged#'+encodeURIComponent(event.data.nonce);document.body.append(f);return;}
 parent.postMessage({type:'hugagent.external.ready',protocolVersion:'1.0.0',
  nonce:${wrong}?'wrong-nonce':event.data.nonce},event.origin);
});
</script>`);
};
const site=createServer(siteHandler), attacker=createServer(siteHandler);
await new Promise(r=>attacker.listen(5256,'127.0.0.1',r));
await new Promise(r=>site.listen(5255,'127.0.0.1',r));
const server=spawn((process.env.WB_TEST_PYTHON || resolve(root, '.venv/bin/python')),
 ['-m','uvicorn','tests.features.workbench.workbench_embed_http_fixture:app','--host','127.0.0.1','--port','5254'],
 {cwd:root,env:{...process.env,PYTHONPATH:'src/backend',AUTH_MODE:'mock',CONFIG_TOKEN:'wb14-test-config',
  DATABASE_URL:'sqlite:///'+resolve(scratch,'http.db'),STORAGE_PATH:resolve(scratch,'storage'),
  TMPDIR:scratch,WB14_BROWSER_OUTPUT:output},stdio:['ignore','pipe','pipe']});
let log=''; server.stdout.on('data',b=>{log+=b;});server.stderr.on('data',b=>{log+=b;});
let browser;
try{
 for(let n=0;n<200;n++){
  if(server.exitCode!==null)throw new Error(log);
  try{if((await fetch(host+'/ready')).ok)break;}catch{}
  if(n===199)throw new Error('WB14 fixture failed: '+log);
  await new Promise(r=>setTimeout(r,100));
 }
 browser=await chromium.launch({headless:true});
 const context=await browser.newContext();
 const page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const headers={'Authorization':'Bearer wb14-test-config','Content-Type':'application/json'};
 for(const [id,path,ready,originStorage] of [
  ['ready','/ready','integrated',true],['quiet','/quiet','unconfirmed',false],
  ['wrong','/wrong','integrated',true],['wrong_origin','/wrong-origin','integrated',true],['xfo','/xfo','integrated',true],
  ['csp','/csp','integrated',true],['redirect','/redirect-host','integrated',true],
 ]){
  const response=await fetch(host+'/api/v1/workbench/admin/external-applications',{
   method:'POST',headers,body:JSON.stringify({enabled:true,
    declaration:{schemaVersion:'1.0.0',id,title:id,target:{kind:'external',mode:'embedded',url:external+path}},
    embedding:{origin:external,ready,originStorage,allowForms:false}})});
  assert.equal(response.status,200,await response.text());
 }
 await page.goto(host);
 await page.evaluate(()=>localStorage.setItem('host-secret','must-never-leak'));
 // Edit the real administrator UI; policy fields must round-trip through HTTP.
 const editor=page.getByRole('region',{name:'普通外部应用',exact:true});
 await editor.getByRole('button',{name:'ready',exact:true}).click();
 assert.equal(await editor.getByLabel('打开方式',{exact:true}).inputValue(),'embedded');
 assert.equal(await editor.getByLabel('精确外部 origin',{exact:true}).inputValue(),external);
 assert.equal(await editor.getByLabel('信任此独立 origin，允许其自身存储（集成握手必需）',{exact:true}).isChecked(),true);
 await editor.getByRole('button',{name:'保存外部应用',exact:true}).click();
 await editor.getByText('外部应用已保存',{exact:true}).waitFor();
 // Discovery must not create external frames or fetch sites.
 await page.getByRole('button',{name:'external:ready',exact:true}).waitFor();
 assert.equal(destinations.length,0);
 await page.getByRole('button',{name:'external:ready',exact:true}).click();
 await page.getByText('外部页面已确认就绪',{exact:true}).waitFor();
 const carrier=page.locator('iframe').first();
 assert.equal(await carrier.getAttribute('referrerpolicy'),'no-referrer');
 assert.ok((await carrier.getAttribute('allow')).includes("clipboard-read 'none'"));
 const remote=page.frames().find(f=>f.url()===external+'/ready');
 assert.ok(remote);
 assert.equal(await remote.evaluate(()=>window.hostDenied),true);
 assert.equal(await remote.evaluate(()=>window.popup===null),true);
 assert.equal(await remote.evaluate(()=>document.referrer),'');
 assert.equal(context.pages().length,1);
 // Wrong source cannot claim ready. No plugin/native bridge is installed.
 await page.evaluate(()=>window.postMessage({type:'hugagent.external.ready',nonce:'spoof'},'*'));
 await page.getByRole('button',{name:'Close embed',exact:true}).click();
 assert.equal(await page.locator('iframe').count(),0);
 for(const id of smoke ? [] : ['quiet','wrong','wrong_origin','xfo','csp','redirect']){
  await page.getByRole('button',{name:'external:'+id,exact:true}).click();
  await page.getByText('未确认就绪。页面可能仍在加载，或不允许嵌入。',{exact:true}).waitFor({timeout:12000});
  assert.equal(await page.getByText('外部页面已确认就绪',{exact:true}).count(),0);
  await page.getByRole('button',{name:'Close embed',exact:true}).click();
 }
 await page.getByRole('button',{name:'external:ready',exact:true}).click();
 await page.getByText('外部页面已确认就绪',{exact:true}).waitFor();
 const popupPromise=page.waitForEvent('popup');
 await page.getByRole('button',{name:'在浏览器中打开',exact:true}).click();
 const popup=await popupPromise;
 await popup.waitForURL(external+'/ready');
 assert.equal(await popup.evaluate(()=>window.opener),null);
 assert.equal(await popup.evaluate(()=>document.referrer),'');
 assert.ok(!popup.url().includes('token'));
 await popup.close();
 await page.evaluate(()=>{window.open=()=>null;});
 await page.getByRole('button',{name:'在浏览器中打开',exact:true}).click();
 await page.getByText('浏览器已阻止弹窗，请允许弹窗后重试',{exact:true}).waitFor();
 // Integrated mode must fail closed if the deployment omits host ancestor isolation.
 await page.goto(host+'/unprotected');
 await page.getByRole('button',{name:'external:ready',exact:true}).click();
 await page.getByText('宿主未启用安全嵌入隔离，请在浏览器中打开',{exact:true}).waitFor();
 assert.equal(await page.locator('iframe').count(),0);
 assert.ok(destinations.every(url=>!url.includes('must-never-leak')));
 assert.deepEqual(errors,[]);
 await writeFile(resolve(scratch,'result.json'),JSON.stringify({
  level:'real HTTP routes and cross-origin Chromium; mock identity only',
  status:smoke?'permission_boundary_smoke_not_acceptance':'browser_boundary_only',
  passed:['lazy-load','ready','host-storage','popup-sandbox','safe-fallback','popup-feedback','missing-isolation',
   ...(smoke?[]:['wrong-source/origin/nonce','timeout-unconfirmed','XFO','CSP','redirect-host-blocked'])],
 },null,2));
 console.log((smoke?'WB14 permission smoke passed; acceptance pending: ':'WB14 browser boundary passed: ')+scratch);
}finally{
 await browser?.close();
 await new Promise(r=>site.close(r));
 await new Promise(r=>attacker.close(r));
 if(server.exitCode===null){const done=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await done;}
 await writeFile(resolve(scratch,'http.log'),log);
}
