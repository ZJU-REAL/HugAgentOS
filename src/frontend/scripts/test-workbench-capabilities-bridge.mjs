import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {chromium} from 'playwright';
const root=resolve('../..');
const scratch=resolve(root,'.scratch/workbench-09/bridge');
await mkdir(scratch,{recursive:true});
const consumer=resolve(scratch,'consumer');
await mkdir(consumer,{recursive:true});
await writeFile(resolve(consumer,'package.json'),'{"private":true,"type":"module"}');
const pack=spawnSync('npm',['pack','--json','--pack-destination',consumer],{cwd:resolve(root,'packages/workbench-sdk'),encoding:'utf8'});
assert.equal(pack.status,0,pack.stdout+pack.stderr);
const archive=JSON.parse(pack.stdout)[0].filename;
const install=spawnSync('npm',['install','--ignore-scripts','--offline',resolve(consumer,archive)],{cwd:consumer,encoding:'utf8'});
assert.equal(install.status,0,install.stdout+install.stderr);
const sdkCode=`
import {connect} from '@hugagent/workbench-sdk';
window.outcome=p=>p.then(data=>({ok:true,data}),e=>({ok:false,code:e.message}));
connect({timeoutMs:3000}).then(c=>{window.connection=c;window.ready=true;},e=>{window.failure=e.message;});
`;
await build({stdin:{contents:sdkCode,resolveDir:consumer},outfile:resolve(scratch,'sdk.js'),bundle:true,format:'iife',target:'es2022'});
const bridgeSource=process.env.WB09_BRIDGE_SOURCE || resolve('src/workbench/workspaceBridge.ts');
const hostCode=`
import {attachWorkspaceBridge} from ${JSON.stringify(bridgeSource)};
import {ApiResponseError} from './src/utils/apiError';
import {WorkbenchSourceError} from './src/workbench/sourceRequest';
window.stats={ready:0,failed:[],calls:[],aborts:0,resets:0};
window.pending=new Map();
window.fixture={
 create(mode='sdk',delay=0,opaque=true){
  const frame=document.createElement('iframe');frame.id='workspace';if(opaque)frame.sandbox='allow-scripts';
  const info={protocolVersion:'1.0.0',theme:'light',locale:'en',capabilities:['host.info','data.query'],context:{applicationKey:'plugin:test:1:app',installId:'test',packageRevision:'1'}};
  const extension={capabilities:['data.query'],reset:()=>window.stats.resets++,handlers:{
   'data.query':async(params,signal)=>{
    window.stats.calls.push(params); signal.addEventListener('abort',()=>window.stats.aborts++,{once:true});
    if(params.sourceDenied)throw new WorkbenchSourceError('local',params.code);
    if(params.apiDenied){
     const error=new ApiResponseError('synthetic_secret_message',403,{detail:{code:params.code,message:'synthetic_secret_detail',diagnosticId:'private_metadata'}});
     window.stats.originalError=error;
     throw error;
    }
    if(params.binaryResult)return new Uint8Array(4);
    if(params.hold)return await new Promise(resolve=>window.pending.set(params.key,resolve));
    return {echo:params};
   },
  }};
  const attach=()=>{window.bridge=attachWorkspaceBridge(frame,info,()=>window.stats.ready++,code=>window.stats.failed.push(code),extension);};
  frame.src='/'+mode+'.html';
  if(delay)frame.addEventListener('load',()=>setTimeout(attach,delay),{once:true});else attach();
  document.body.append(frame);
 },
 finish(key,value){window.pending.get(key)?.(value);},
 dispose(){window.bridge?.dispose();document.querySelector('#workspace')?.remove();},
};
`;
await build({stdin:{contents:hostCode,resolveDir:process.cwd(),loader:'ts'},outfile:resolve(scratch,'host.js'),bundle:true,format:'esm',target:'es2022',jsx:'automatic',define:{'import.meta.env':'{}'},
 alias:{'@hugagent/workbench-protocol':resolve(root,'packages/workbench-protocol/dist/index.js')}});
const rawCode=`
window.sessions=[];window.helloNonce='a'.repeat(32);window.sequence=0;
window.hello=()=>parent.postMessage({type:'workbench:hello',protocolVersion:'1.0.0',clientNonce:window.helloNonce},'*');
window.addEventListener('message',e=>{
 if(e.source!==parent || e.data?.type!=='workbench:init')return;
 const session={nonce:e.data.nonce,port:e.ports[0],messages:[]};window.sessions.push(session);window.current=session;
 session.port.onmessage=e=>session.messages.push(e.data);session.port.start();
 session.port.postMessage({type:'ready',protocolVersion:'1.0.0',nonce:session.nonce});
});
window.send=(method,params={},id=String(++window.sequence))=>{window.current.port.postMessage({type:'request',id,method,params,nonce:window.current.nonce});return id;};
window.hello();setInterval(window.hello,200);
`;
const assets={
 '/':'<script type="module" src="/host.js"></script>',
 '/sdk.html':'<script src="/sdk.js"></script>',
 '/raw.html':'<script src="/raw.js"></script>',
 '/raw.js':rawCode,
 '/evil.html':'<script>parent.postMessage({type:"workbench:hello",protocolVersion:"1.0.0",clientNonce:"b".repeat(32)},"*");</script>',
 '/host.js':await readFile(resolve(scratch,'host.js')),
 '/sdk.js':await readFile(resolve(scratch,'sdk.js')),
};
const server=createServer((req,res)=>{const body=assets[req.url.split('?')[0]];if(body===undefined){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/html');res.end(body);});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port;
const browser=await chromium.launch({headless:true});
const results=[];
async function run(name,fn){
 if(process.env.WB09_CASE && !name.includes(process.env.WB09_CASE))return;
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{await page.goto(origin);await page.waitForFunction(()=>window.fixture);await fn(page);assert.deepEqual(errors,[]);results.push({name,passed:true});console.log('PASS '+name);}
 catch(error){results.push({name,passed:false,error:error.message});console.log('FAIL '+name+': '+error.message);}
 finally{await page.close();}
}
async function start(page,mode='sdk',delay=0){
 await page.evaluate(({mode,delay})=>window.fixture.create(mode,delay),{mode,delay});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(mode==='sdk'?'window.ready || window.failure':'window.current',{timeout:5000});
 if(mode==='raw')await page.waitForTimeout(300);
 if(mode==='sdk')assert.equal(await frame.evaluate(()=>window.failure),undefined);
 return frame;
}
try{
 await run('typed source denial reaches SDK without source or raw message',async page=>{
  const frame=await start(page);
  for(const code of ['origin_mismatch','execution_plane_not_ready','source_not_supported','origin_unavailable']) {
   const denied=await frame.evaluate(code=>window.outcome(window.connection.request('data.query',{sourceDenied:true,code})),code);
   assert.deepEqual(denied,{ok:false,code});
  }
  for(const code of ['synthetic_secret','local:origin_unavailable','https://secret.invalid/token']) {
   const denied=await frame.evaluate(code=>window.outcome(window.connection.request('data.query',{sourceDenied:true,code})),code);
   assert.deepEqual(denied,{ok:false,code:'bridge_error'});
  }
 });
 await run('HTTP denial code reaches real SDK without raw message or metadata',async page=>{
  const frame=await start(page);
  const denied=await frame.evaluate(()=>window.outcome(window.connection.request('data.query',{apiDenied:true,code:'capability_not_granted'})));
  assert.deepEqual(denied,{ok:false,code:'capability_not_granted'});
  const unknown=await frame.evaluate(()=>window.outcome(window.connection.request('data.query',{apiDenied:true,code:'synthetic_secret'})));
  assert.deepEqual(unknown,{ok:false,code:'bridge_error'});
  const hostile=await frame.evaluate(()=>window.outcome(window.connection.request('data.query',{apiDenied:true,code:'https://secret.invalid/token'})));
  assert.deepEqual(hostile,{ok:false,code:'bridge_error'});
  assert.deepEqual(await page.evaluate(()=>({name:window.stats.originalError.name,status:window.stats.originalError.status,payload:window.stats.originalError.payload})),
   {name:'ApiResponseError',status:403,payload:{detail:{code:'https://secret.invalid/token',message:'synthetic_secret_detail',diagnosticId:'private_metadata'}}});
 });
 await run('async extension preserves params and cancellation signal',async page=>{
  const frame=await start(page);
  assert.deepEqual(await frame.evaluate(()=>window.connection.query('files',{limit:1})),{echo:{source:'files',params:{limit:1}}});
  const outcome=await frame.evaluate(async()=>{
   const abort=new AbortController();const promise=window.outcome(window.connection.request('data.query',{hold:true,key:'cancel'},abort.signal));
   setTimeout(()=>abort.abort(),30);return await promise;
  });
  assert.equal(outcome.code,'cancelled');
  await page.waitForFunction(()=>window.stats.aborts===1);
 });
 await run('host extension updates retain the same session and use the current route',async page=>{
  const frame=await start(page);
  const ready=await page.evaluate(()=>window.stats.ready);
  const info=await frame.evaluate(()=>window.connection.request('host.info'));
  await page.evaluate(()=>window.bridge.updateExtension({handlers:{
   'workspace.navigate':async params=>({path:'/updated/'+params.path}),
  }}));
  assert.deepEqual(await frame.evaluate(()=>window.connection.request('workspace.navigate',{path:'page'})),{path:'/updated/page'});
  const old=await frame.evaluate(()=>window.outcome(window.connection.request('data.query',{})));
  assert.equal(old.code,'unknown_method');
  assert.equal(await page.evaluate(()=>window.stats.ready),ready);
  assert.deepEqual(await frame.evaluate(()=>window.connection.request('host.info')),info);
 });
 await run('bridge can attach 500ms after document load',async page=>{
  const frame=await start(page,'sdk',500);
  assert.equal((await frame.evaluate(()=>window.connection.request('host.info'))).locale,'en');
 });
 await run('forged source and non-opaque origin cannot create sessions',async page=>{
  const frame=await start(page,'raw');
  const ready=await page.evaluate(()=>window.stats.ready);
  const sessions=await frame.evaluate(()=>window.sessions.length);
  await page.evaluate(()=>{
   const frame=document.querySelector('#workspace');
   window.dispatchEvent(new MessageEvent('message',{source:window,origin:'null',data:{type:'workbench:hello',protocolVersion:'1.0.0',clientNonce:'c'.repeat(32)}}));
   window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,origin:location.origin,data:{type:'workbench:hello',protocolVersion:'1.0.0',clientNonce:'d'.repeat(32)}}));
   const evil=document.createElement('iframe');evil.sandbox='allow-scripts';evil.src='/evil.html';document.body.append(evil);
  });
  await page.waitForTimeout(350);
  assert.equal(await page.evaluate(()=>window.stats.ready),ready);
  assert.equal(await frame.evaluate(()=>window.sessions.length),sessions);
 });
 await run('actual non-opaque document cannot negotiate a channel',async page=>{
  await page.evaluate(()=>window.fixture.create('raw',0,false));
  await page.waitForTimeout(550);
  assert.equal(await page.evaluate(()=>window.stats.ready),0);
  assert.equal(await page.frames()[1].evaluate(()=>window.sessions.length),0);
 });
 await run('binary handler results rejected and prototype methods unavailable',async page=>{
  const frame=await start(page);
  const binary=await frame.evaluate(()=>window.outcome(window.connection.request('data.query',{binaryResult:true})));
  assert.equal(binary.code,'invalid_result');
  const prototype=await frame.evaluate(()=>window.outcome(window.connection.request('constructor')));
  assert.equal(prototype.code,'unknown_method');
 });
 await run('direct binary and oversized messages rejected before handler',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>{
   window.send('data.query',{binary:new ArrayBuffer(1024)});
   window.send('data.query',{binary:new Uint8Array(1024)});
   window.send('data.query',{text:'x'.repeat(300000)});
   window.send('data.query',{nodes:Array(5000).fill(1)});
  });
  await page.waitForFunction(()=>window.stats.failed.length>=4);
  assert.equal(await page.evaluate(()=>window.stats.calls.length),0);
  assert.ok((await page.evaluate(()=>window.stats.failed)).every(code=>code==='invalid_message'));
 });
 await run('request IDs strictly increase and replay never invokes handler',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>{
   window.send('data.query',{marker:'first'},'1');
   window.send('data.query',{marker:'replay'},'1');
   window.send('data.query',{marker:'zero'},'0');
   window.send('data.query',{marker:'decimal'},'1.5');
   window.send('data.query',{marker:'negative'},'-1');
   window.send('data.query',{marker:'second'},'2');
  });
  await page.waitForFunction(()=>window.stats.calls.length===2);
  assert.deepEqual(await page.evaluate(()=>window.stats.calls),[{marker:'first'},{marker:'second'}]);
 });
 await run('host enforces concurrent requests without SDK guard',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>{for(let i=0;i<9;i++)window.send('data.query',{hold:true,key:'c'+i});});
  await frame.waitForFunction(()=>window.current.messages.some(m=>m.code==='concurrency_limit'));
  assert.equal(await page.evaluate(()=>window.stats.calls.length),8);
 });
 await run('host enforces rate budget without SDK guard',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>{for(let i=0;i<31;i++)window.send('not.implemented',{});});
  await frame.waitForFunction(()=>window.current.messages.length===31);
  assert.equal(await frame.evaluate(()=>window.current.messages.filter(m=>m.code==='rate_limited').length),1);
 });
 await run('host timeout aborts operation and ignores eventual success',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>window.send('data.query',{hold:true,key:'timeout'}));
  await frame.waitForFunction(()=>window.current.messages.some(m=>m.code==='bridge_timeout'),{timeout:12000});
  assert.equal(await page.evaluate(()=>window.stats.aborts),1);
  await page.evaluate(()=>window.fixture.finish('timeout',{leaked:true}));
  await page.waitForTimeout(100);
  assert.equal(await frame.evaluate(()=>window.current.messages.length),1);
 });
 await run('new hello aborts old operation and drops old Promise completion',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>window.send('data.query',{hold:true,key:'old'}));
  await page.waitForFunction(()=>window.stats.calls.length===1);
  const sessions=await frame.evaluate(()=>window.sessions.length);
  await frame.evaluate(()=>{window.helloNonce='e'.repeat(32);window.sequence=0;window.hello();});
  await frame.waitForFunction(count=>window.sessions.length===count+1,sessions);
  await page.waitForFunction(()=>window.stats.aborts===1);
  await page.evaluate(()=>window.fixture.finish('old',{secret:'old-document'}));
  await frame.evaluate(()=>window.send('data.query',{marker:'fresh'}));
  await frame.waitForFunction(()=>window.current.messages.length===1);
  assert.deepEqual(await frame.evaluate(()=>window.current.messages[0].data),{echo:{marker:'fresh'}});
 });
 await run('iframe reload invalidates transferred old port and stale result',async page=>{
  const frame=await start(page,'raw');
  await frame.evaluate(()=>window.send('data.query',{hold:true,key:'reload'}));
  await page.waitForFunction(()=>window.stats.calls.length===1);
  await page.evaluate(()=>new Promise(resolve=>{
   const thief=document.createElement('iframe');thief.id='thief';thief.sandbox='allow-scripts';
   thief.srcdoc='<script>window.messages=[];addEventListener("message",e=>{window.stolen=e.ports[0];window.stolen.onmessage=e=>window.messages.push(e.data);window.stolen.start();});</script>';
   thief.onload=resolve;document.body.append(thief);
  }));
  await frame.evaluate(()=>{
   const current=window.current;
   parent.frames[1].postMessage({nonce:current.nonce},'*',[current.port]);
   window.stolenNonce=current.nonce;
  });
  const thief=page.frames().find(f=>f!==frame && f.parentFrame());
  await thief.waitForFunction(()=>window.stolen);
  const nonce=await frame.evaluate(()=>window.stolenNonce);
  await page.evaluate(()=>{document.querySelector('#workspace').src='/raw.html?reload=1';});
  // The exact URL below is served as the same document.
  await page.waitForFunction(()=>window.stats.aborts===1);
  await page.evaluate(()=>window.fixture.finish('reload',{secret:'stale'}));
  await thief.evaluate(nonce=>window.stolen.postMessage({type:'request',nonce,id:'2',method:'data.query',params:{marker:'stolen'}}),nonce);
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(()=>window.stats.calls.length),1);
  assert.equal(await thief.evaluate(()=>window.messages.length),0);
  await frame.waitForFunction(()=>window.current);
  await frame.evaluate(()=>window.send('data.query',{marker:'new-document'}));
  await page.waitForFunction(()=>window.stats.calls.length===2);
  await frame.waitForFunction(()=>window.current.messages.length===1);
  assert.deepEqual(await frame.evaluate(()=>window.current.messages[0].data),{echo:{marker:'new-document'}});

 });
}finally{
 await browser.close();await new Promise(resolve=>server.close(resolve));
 await writeFile(resolve(scratch,process.env.WB09_BRIDGE_SOURCE?'baseline-results.json':'results.json'),JSON.stringify(results,null,2));
}
assert.ok(results.every(result=>result.passed),JSON.stringify(results.filter(r=>!r.passed),null,2));
console.log('Isolated Chromium bridge boundary matrix passed');
