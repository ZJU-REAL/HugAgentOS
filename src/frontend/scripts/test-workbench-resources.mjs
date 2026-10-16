import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {mkdir} from 'node:fs/promises';
import path from 'node:path';

const require = createRequire(import.meta.url);
const {build} = require('esbuild');
const directory = path.resolve('../../.scratch/workbench-11/frontend-smoke');
await mkdir(directory, {recursive:true});
const output = path.join(directory, 'resource-extension.mjs');
await build({entryPoints:['src/workbench/resourceExtension.ts'], outfile:output,
  bundle:true, platform:'node', format:'esm', target:'es2022'});
const {createResourceExtension} = await import(pathToFileURL(output));
const calls=[];
let tick;
const previousInterval=globalThis.setInterval;
const previousClear=globalThis.clearInterval;
globalThis.setInterval=fn=>{assert.equal(tick,undefined);tick=fn;return 1;};
globalThis.clearInterval=()=>{tick=undefined;};
try {
  const controller = new AbortController();
  const resource = createResourceExtension({applicationKey:'plugin:key', instanceId:'instance',
    installId:'install',revision:'revision',executionScope:'local',moduleId:'page',
    origin:{kind:'local',id:'0123456789abcdef0123456789abcdef'},signal:controller.signal}, async (url,init)=>{
      calls.push({url,init});
      return {resource:{resource_id:'owned',status:'active',expires_at:90}};
    });
  assert.equal(tick,undefined,'unopened apps must own no heartbeat');
  const signal=new AbortController().signal;
  const handlers=resource.extension.handlers;
  await handlers['resource.open']({},signal);
  assert.equal(typeof tick,'function');
  const count=calls.length;
  tick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,count+1,'one host heartbeat renews the lease');
  assert.match(calls.at(-1).url,/owned\/lease$/);
  await assert.rejects(handlers['resource.command']({resourceId:'another',id:'1',action:'state'},signal));
  controller.abort();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(tick,undefined,'abort releases timer immediately');
  assert.equal(calls.at(-1).init.method,'DELETE');
  await assert.rejects(handlers['resource.open']({},signal),/instance_closed/);
  console.log('workbench resource host lifecycle smoke passed (mock transport)');
} finally {
  globalThis.setInterval=previousInterval;
  globalThis.clearInterval=previousClear;
}
