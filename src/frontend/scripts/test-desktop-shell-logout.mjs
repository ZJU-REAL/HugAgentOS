import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const {build}=require('esbuild');
const result=await build({entryPoints:[resolve('src/desktop/shellLogout.ts')],
 bundle:true,write:false,platform:'node',format:'esm'});
const {logoutViaHostPlatform}=await import('data:text/javascript;base64,'+Buffer.from(result.outputFiles[0].text).toString('base64'));
let calls=0;
const host={__HG_PLATFORM__:{platform:'tauri',key:'host-only-probe'}};
host.self=host;host.top=host;globalThis.window=host;
globalThis.fetch=async (url,options)=>{
 calls++;assert.equal(url,'/__desktop/workbench/platform');
 assert.equal(options.method,'POST');assert.equal(options.headers['X-Workbench-Native'],'host-only-probe');
 assert.deepEqual(JSON.parse(options.body),{op:'logout'});
 return {ok:true,json:async()=>({cleared:true})};
};
assert.equal(await logoutViaHostPlatform(),true);assert.equal(calls,1);
window={self:{},top:{},__HG_PLATFORM__:host.__HG_PLATFORM__};
assert.equal(await logoutViaHostPlatform(),null);assert.equal(calls,1);
window={self:host,top:host,__HG_PLATFORM__:{platform:'electron',key:'probe'}};
assert.equal(await logoutViaHostPlatform(),null);assert.equal(calls,1);
window=host;fetch=async()=>({ok:false});
await assert.rejects(logoutViaHostPlatform(),/desktop_logout_failed/);
console.log('trusted-host logout endpoint smoke passed; no native IPC/device claim');
