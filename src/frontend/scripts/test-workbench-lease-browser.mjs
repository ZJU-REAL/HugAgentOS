import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createServer} from 'node:http';
import {chromium} from 'playwright';
const folder=resolve('../../.scratch/workbench-structure-20261015/lease-browser');
await mkdir(folder,{recursive:true});
const key='plugin:cloud:'+'a'.repeat(32)+':'+'b'.repeat(64)+':20261014T000000.000000Z:app';
const launch={applicationKey:key,revision:'b'.repeat(64),
 binding:{installId:'isolated-install',packageRevision:'b'.repeat(64),slug:'lease-fixture',
 origin:{kind:'cloud',id:'a'.repeat(32)},executionScope:'cloud'},
 module:{id:'page',title:'Lease app',grants:['file.pick']},
 assetPath:'/v1/workbench/applications/'+encodeURIComponent(key)+'/packages/page',
 context:{applicationKey:key,installId:'isolated-install',packageRevision:'b'.repeat(64),instanceId:'instance-00000001'}};
const fixture=[
 'import {createRoot} from "react-dom/client";',
 'import {PluginWorkspaceFrame} from "./src/workbench/PluginWorkspaceFrame";',
 'window.mode="ok";window.calls=0;',
 'createRoot(document.getElementById("root")).render(<PluginWorkspaceFrame launch={'+JSON.stringify(launch)+'} theme="light" locale="zh-CN" instanceId="instance-00000001" onRetry={()=>{}}/>);',
].join('\n');
const mocks={
 'sourceRequest.ts':[
 'import {ApiResponseError} from "../utils/apiError";',
 'export class WorkbenchSourceError extends Error {}',
 'export async function workbenchRequest(path,options){',
 'if(path.endsWith("/close"))return {data:{closed:true}};',
 'const body=JSON.parse(options.body);',
 'if(options.method==="PUT"&&window.mode==="offline")throw new TypeError("offline");',
 'if(options.method==="PUT"&&window.mode==="denied")throw new ApiResponseError("denied",403,{});',
 'window.nextRenew=Date.now()+30000;return {data:{...body,expiresAt:Date.now()/1000+90}};}',
 ].join('\n'),
 'catalog.ts':'export function useApplicationDirectory(){return {userId:"alice"};}',
 'invalidation.ts':'export function identityKey(){return "isolated-owner";} export function registerWorkbenchSession(){return ()=>{};} export function invalidateWorkbenchDirectory(){}',
 'WorkspaceCapabilityDialog.tsx':'const confirm=async()=>true; export function useWorkspaceConfirmation(){return {confirm,dialog:null};}',
 'usePlatformExtension.tsx':'const platform={adapter:{},approve:async()=>{},extension:{},dialog:null}; export function usePlatformExtension(){return platform;}',
 'capabilityHost.ts':[
 'const host={attachPlatform(){},authorize:async()=>{},capabilities:["file.pick"],handlers:{',
 '"file.pick":async(params,signal)=>{',
 'if(params.hold)return new Promise((_,reject)=>signal.addEventListener("abort",()=>reject(new Error("cancelled")),{once:true}));',
 'window.calls++;return {picked:true};}}};',
 'export function createCapabilityHost(){return host;}',
 ].join('\n'),
};
await build({stdin:{contents:fixture,resolveDir:process.cwd(),loader:'tsx'},
 outfile:resolve(folder,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',
 define:{'import.meta.env':'{}'},loader:{'.css':'empty'},
 plugins:[{name:'controlled-transport-and-optional-hosts',setup(builder){
 builder.onLoad({filter:/[\\/]workbench[\\/][^\\/]+$/},args=>{
 const name=args.path.split(/[\\/]/).at(-1);
 return name in mocks?{contents:mocks[name],loader:'tsx',resolveDir:resolve('src/workbench')}:undefined;
 });}}]});
const frameHtml='<script>'+[
 'window.documentId=crypto.randomUUID();window.nextId=0;window.pending=new Map();',
 'window.addEventListener("message",e=>{',
 'if(e.source!==parent||e.data?.type!=="workbench:init")return;',
 'window.port=e.ports[0];window.nonce=e.data.nonce;',
 'port.onmessage=e=>{const m=e.data;if(m.type==="result"){window.pending.get(m.id)?.(m);window.pending.delete(m.id);}};',
 'port.start();port.postMessage({type:"ready",protocolVersion:"1.0.0",nonce});});',
 'window.invoke=params=>new Promise(resolve=>{const id=String(++window.nextId);window.pending.set(id,resolve);',
 'port.postMessage({type:"request",id,method:"file.pick",params,nonce});});',
 'const clientNonce="c".repeat(32);',
 'setInterval(()=>parent.postMessage({type:"workbench:hello",protocolVersion:"1.0.0",clientNonce},"*"),100);',
].join('\n')+'</script>';
const body=await readFile(resolve(folder,'fixture.js'));
const server=createServer((req,res)=>{
 res.setHeader('Content-Type',req.url==='/fixture.js'?'text/javascript':'text/html');
 res.end(req.url==='/fixture.js'?body:req.url.startsWith('/api/')?frameHtml:
 '<div id="root"></div><script type="module" src="/fixture.js"></script>');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage();
 const baseTime=Date.now();
 await page.clock.install({time:new Date(baseTime)});
 await page.goto('http://127.0.0.1:'+server.address().port);
 await page.waitForFunction(()=>document.querySelector('iframe'));
 await page.clock.pauseAt(new Date(baseTime+5000));
 await page.clock.runFor(200);
 const frame=page.frames().find(value=>value.parentFrame());
 await frame.waitForFunction(()=>window.port);
 const id=await frame.evaluate(()=>window.documentId);
 assert.equal((await frame.evaluate(()=>window.invoke({}))).ok,true);
 const untilRenew=await page.evaluate(()=>window.nextRenew-Date.now());
 await page.clock.runFor(untilRenew-1000);
 await frame.evaluate(()=>{window.held=window.invoke({hold:true});});
 await page.evaluate(()=>{window.mode='offline';});
 await page.clock.runFor(1000);
 await page.getByRole('status').filter({hasText:'连接中断'}).waitFor();
 assert.equal((await frame.evaluate(()=>window.invoke({}))).code,'connection_recovering');
 assert.equal((await frame.evaluate(()=>window.held)).code,'cancelled');
 assert.equal(await page.evaluate(()=>window.calls),1);
 await page.evaluate(()=>{window.mode='ok';});
 await page.clock.runFor(2000);
 await page.waitForFunction(()=>!document.body.textContent.includes('连接中断'));
 assert.equal(await frame.evaluate(()=>window.documentId),id);
 assert.equal((await frame.evaluate(()=>window.invoke({}))).ok,true);
 await page.evaluate(()=>{window.mode='denied';});
 await page.clock.runFor(30000);
 await page.getByRole('alert').waitFor();
 assert.equal(await page.locator('iframe').count(),0);
 await writeFile(resolve(folder,'result.json'),JSON.stringify({passed:true,checks:[
 'opaque iframe handshake','in-flight cancellation','recovery capability gate',
 'same-document recovery','terminal denial']},null,2));
 console.log('Lease browser integration: handshake, recovery gate, cancellation, preserved document and denial passed');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
