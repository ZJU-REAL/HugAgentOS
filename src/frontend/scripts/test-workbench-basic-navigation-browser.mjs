import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createServer} from 'node:http';
import {chromium} from 'playwright';

const output=resolve('node_modules/.tmp/workbench-basic-navigation');
await mkdir(output,{recursive:true});
await build({stdin:{contents:`
import {connect,workspaceNavigation} from '../../packages/workbench-sdk/src/index.ts';
connect({requiredCapabilities:['host.info','workspace.navigate'],timeoutMs:2000}).then(connection=>{
 window.connection=connection;window.navigation=workspaceNavigation(connection);
 window.initial=connection.info.context;document.querySelector('p').textContent='ready';
},error=>document.querySelector('p').textContent=error.message);
`,resolveDir:process.cwd(),loader:'ts'},outfile:resolve(output,'sdk.js'),bundle:true,format:'iife'});
await build({stdin:{contents:`
import {attachWorkspaceBridge} from './src/workbench/workspaceBridge';
import {workspaceCapabilities} from './src/workbench/workspaceCapabilities';
import {ownApplicationPath} from './src/workbench/workspaceNavigation';
const key='plugin:cloud:'+ 'a'.repeat(32)+':'+ 'b'.repeat(64)+':20261014T120000.000000Z:reports';
window.fixture={
 attach(withHandler=true){
  window.privileged=0;window.ready=0;
  const frame=document.createElement('iframe');frame.id='plugin';frame.sandbox='allow-scripts';frame.src='/sdk.html';
  let route={path:'/',requestId:'host:initial'};
  const context={applicationKey:key,installId:'exact-install',packageRevision:'b'.repeat(64),instanceId:'host-instance'};
  let bridge;
  const hostExtension={capabilities:['workspace.navigate'],handlers:withHandler?{
   'workspace.navigate':params=>{
    const path=ownApplicationPath(params.path);
    if(params.applicationKey && params.applicationKey!==key)throw Error('invalid_navigation');
    route={path,requestId:params.requestId};bridge.update(info());return route;
   }
  }:{}};
  const privileged=['navigation.open','platform.openExternal','file.pick','file.save','resource.open','resource.command','data.query'];
  const merged={capabilities:[...hostExtension.capabilities,...privileged],handlers:{
   ...hostExtension.handlers,...Object.fromEntries(privileged.map(method=>[method,()=>{window.privileged++;return {unauthorized:true};}]))
  }};
  // A UI3 module with no privileged grants, matching official workspace navigation.
  const effective=workspaceCapabilities(merged,withHandler?[]:['workspace.navigate'],hostExtension);
  const info=()=>({protocolVersion:'1.0.0',theme:'light',locale:'en',context,route,
   capabilities:['host.info',...(effective.capabilities??[])]});
  bridge=attachWorkspaceBridge(frame,info(),()=>window.ready++,code=>window.failed=code,effective);
  window.dispose=()=>{bridge.dispose();frame.remove();};
  document.body.append(frame);
 }
};
`,resolveDir:process.cwd(),loader:'ts'},outfile:resolve(output,'host.js'),bundle:true,format:'iife',define:{'import.meta.env':'{}'}});
const assets={
 '/':'<script src="/host.js"></script>',
 '/sdk.html':'<p>loading</p><script src="/sdk.js"></script>',
 '/sdk.js':await readFile(resolve(output,'sdk.js')),
 '/host.js':await readFile(resolve(output,'host.js')),
};
const server=createServer((request,response)=>{
 const data=assets[request.url.split('?')[0]];response.statusCode=data===undefined?404:200;
 response.setHeader('Content-Type',request.url.endsWith('.js')?'text/javascript':'text/html');response.end(data);
});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const origin='http://127.0.0.1:'+server.address().port;
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage();await page.goto(origin);
 await page.evaluate(()=>window.fixture.attach());
 const frame=page.frameLocator('#plugin');
 await frame.locator('p').filter({hasText:/ready|host_incompatible/}).waitFor();
 assert.equal(await frame.locator('p').textContent(),'ready');
 const child=page.frames().find(frame=>frame.parentFrame());
 const before=await child.evaluate(()=>window.initial);
 const own=await child.evaluate(()=>window.navigation.navigate('/reports/details?q=a%26b#row'));
 assert.equal(own.path,'/reports/details?q=a%26b#row');
 assert.equal(await page.locator('#plugin').count(),1);
 assert.deepEqual(await child.evaluate(()=>window.connection.info.context),before);
 assert.equal(await page.evaluate(()=>window.ready),1);
 for(const path of ['//evil.test','/apps/another','/%2e%2e/settings','/a%2Fb','/a%3Fb']){
  const result=await child.evaluate(async path=>{
   try{await window.navigation.navigate(path);return 'unexpected'}catch(error){return error.message}
  },path);
  assert.equal(result,'invalid_navigation');
 }
 for(const method of ['navigation.open','platform.openExternal','file.pick','file.save','resource.open','resource.command','data.query']){
  const result=await child.evaluate(async method=>{
   try{await window.connection.request(method,{});return 'unexpected'}catch(error){return error.message}
  },method);
  assert.equal(result,'unknown_method');
 }
 assert.equal(await page.evaluate(()=>window.privileged),0);
 await page.evaluate(()=>window.dispose());
 await page.evaluate(()=>window.fixture.attach(false));
 await page.frameLocator('#plugin').locator('p').filter({hasText:'host_incompatible'}).waitFor();
 assert.equal(await page.evaluate(()=>window.ready),0);
 console.log('Basic own-app navigation SDK boundary passed: no grant, exact context, path attacks denied, privileged methods denied, absent host handler incompatible.');
}finally{await browser.close();await new Promise(done=>server.close(done));}
