// Isolated public registration behavior. Transport/browser/worker matrix runs in WB23.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const directory = await mkdtemp(join(process.cwd(), '../../.scratch/workbench-12/registry-'));
try {
 const outfile=join(directory,'behavior.mjs');
 await build({entryPoints:['src/workbench/invalidation.ts'],bundle:true,format:'esm',platform:'node',outfile,
  plugins:[{name:'trusted-host-fixture',setup(builder){
   builder.onResolve({filter:/stores\/(authStore|deploymentModeStore)$/},args=>({path:args.path,namespace:'fixture'}));
   builder.onResolve({filter:/^\.\.\/api$/},()=>({path:'api',namespace:'fixture'}));
   builder.onLoad({filter:/.*/,namespace:'fixture'},args=>({contents:args.path==='api'
    ?'export const getApiUrl=()=>"/api";'
    :args.path.endsWith('authStore')
      ?'export const useAuthStore=globalThis.auth;'
      :'export const useDeploymentModeStore=globalThis.mode;',loader:'js'}));
  }}]});
 const makeStore=state=>({state,listeners:new Set(),getState(){return this.state;},subscribe(fn){this.listeners.add(fn);return()=>this.listeners.delete(fn);},set(next){this.state={...this.state,...next};for(const fn of this.listeners)fn();}});
 globalThis.auth=makeStore({authUser:{user_id:'alice'},loggingOut:false});
 globalThis.mode=makeStore({serverBase:'https://host.example',localBase:'',provisionMode:'cloud_only',activeLocal:false});
 globalThis.window={dispatchEvent(){}};
 globalThis.localStorage={getItem:()=>null,setItem(){}};
 globalThis.CustomEvent=class {constructor(type,init){this.type=type;this.detail=init?.detail;}};
 const wb=await import(pathToFileURL(outfile));
 const revoked=[];
 wb.registerWorkbenchSession({identity:wb.identityKey(),applicationKey:'plugin:one',revision:'r1',revoke:r=>revoked.push(r)});
 wb.reconcileWorkbenchSessions([{applicationKey:'plugin:one',revision:'r1',availability:{available:true}}]);
 assert.equal(revoked.length,0);
 wb.reconcileWorkbenchSessions([{applicationKey:'plugin:one',revision:'r2',availability:{available:true}}]);
 assert.deepEqual(revoked,['directory']);
 const register=wb.createSessionRegistration();
 register({identity:{userId:'alice',applicationKey:'plugin:two',packageRevision:'r1'}},()=>revoked.push('pool'));
 auth.set({loggingOut:true});
 assert.equal(revoked.at(-1),'pool');
 let stale=false;
 register({identity:{userId:'alice',applicationKey:'plugin:late',packageRevision:'r1'}},()=>{stale=true;});
 assert.equal(stale,true);
 auth.set({loggingOut:false});
 wb.registerWorkbenchSession({identity:wb.identityKey(),applicationKey:'plugin:three',revision:'r1',revoke:r=>revoked.push(r)});
 mode.set({serverBase:'https://other.example'});
 assert.equal(revoked.at(-1),'identity');
 const before=revoked.length;
 wb.registerWorkbenchSession({identity:wb.identityKey(),applicationKey:'plugin:four',revision:'r1',revoke:r=>revoked.push(r)});
 auth.set({authUser:{user_id:'bob'}});
 assert.equal(revoked.length,before+1);
 console.log('WB12 registration boundary checks passed (host store fixture; not browser E2E)');
} finally {await rm(directory,{recursive:true,force:true});}
