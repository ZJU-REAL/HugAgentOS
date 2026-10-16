import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const {build}=require('esbuild');
const result=await build({entryPoints:[resolve('src/workbench/platformAdapter.ts')],
 bundle:true,write:false,platform:'node',format:'esm'});
const adapter=await import('data:text/javascript;base64,'+Buffer.from(result.outputFiles[0].text).toString('base64'));
assert.equal(adapter.projectRuntimeCapabilities('web','',false).mode,'cloud');
assert.equal(adapter.projectRuntimeCapabilities('tauri','dual',false).local,false);
assert.equal(adapter.projectRuntimeCapabilities('electron','dual',true).local,true);
assert.equal(adapter.projectRuntimeCapabilities('tauri','local_only',true).cloud,false);
assert.equal(adapter.projectRuntimeCapabilities('unsupported','dual',true).openExternal,false);
assert.throws(()=>adapter.safePlatformUrl('file:///private/report'),/invalid_url/);
assert.throws(()=>adapter.safePlatformUrl('https://user:secret@example.test'),/invalid_url/);
assert.throws(()=>adapter.safeFileName('../escape'),/invalid_params/);
assert.throws(()=>adapter.safeFileName('C:\\escape'),/invalid_params/);
console.log('WB-17 capability and dangerous URL/path smoke passed (no native-device claim)');
