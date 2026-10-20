import assert from 'node:assert/strict';
import {workbenchRequest, preferenceSource, WorkbenchSourceError,setHostedWorkbenchOrigin} from '../src/workbench/sourceRequest';
import {useDeploymentModeStore} from '../src/stores/deploymentModeStore';
import {applicationOrigin,packageAssetPath,sourceTargets,requestOrigin} from '../src/workbench/sourceIdentity';

const scope='a'.repeat(32), hash='b'.repeat(64);
const local='plugin:local:'+scope+':'+hash+':20261014T120000.123456Z:reports';
const cloud=local.replace(':local:',':cloud:');
const calls:Array<{path:string;headers:Headers}>=[];
globalThis.fetch=async (path,options)=>{
  calls.push({path:String(path),headers:new Headers(options?.headers)});
  return new Response(JSON.stringify({data:{applicationKey:local}}),{status:200});
};
function mode(provisionMode:string,localReady=true) {
  useDeploymentModeStore.setState({isDesktop:!!provisionMode,provisionMode,localReady});
}
mode('dual');
for(const key of [local,cloud]) {
  await workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(key)+'/launch',{method:'POST'});
  assert.equal(calls.at(-1)?.headers.get('x-hugagent-target'),key===local?'local':'cloud');
  assert.equal(calls.at(-1)?.headers.get('x-workbench-origin'),scope);
}
assert.equal(calls.length,2,'same-name installs each make exactly one source-scoped call');
await workbenchRequest('/v1/workbench/me/application-state/'+encodeURIComponent(local));
assert.equal(calls.at(-1)?.headers.get('x-hugagent-target'),'local');
await workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(local)+'/instances/i/resources');
assert.equal(calls.at(-1)?.headers.get('x-hugagent-target'),'local');
const before=calls.length;
await assert.rejects(workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(local)+'/launch',undefined,'cloud'),
  error=>error instanceof WorkbenchSourceError && error.code==='origin_mismatch');
assert.equal(calls.length,before);
mode('dual',false);
await assert.rejects(workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(local)+'/launch'),
  error=>error instanceof WorkbenchSourceError && error.code==='execution_plane_not_ready');
assert.equal(calls.length,before,'not-ready local never reaches cloud');
mode('');
await assert.rejects(workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(local)+'/launch'));
assert.equal(calls.length,before,'web cannot choose local execution');
setHostedWorkbenchOrigin({kind:'local',id:scope});
await workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(local)+'/launch');
assert.equal(calls.at(-1)?.headers.get('x-workbench-origin'),scope,'standalone local web uses its authenticated hosting identity');
const hostedCount=calls.length;
await assert.rejects(workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(local.replace(scope,'c'.repeat(32)))+'/launch'));
assert.equal(calls.length,hostedCount,'standalone web cannot address another local service');
setHostedWorkbenchOrigin();
mode('local_only');
assert.equal(preferenceSource(),'local');
assert.deepEqual(sourceTargets(useDeploymentModeStore.getState()),['local']);
mode('dual');
assert.equal(preferenceSource(),'local','dual private preferences stay on device');
globalThis.fetch=async(path,options)=>{calls.push({path:String(path),headers:new Headers(options?.headers)});throw new TypeError('network');};
await assert.rejects(workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(cloud)+'/launch'),
  error=>error instanceof WorkbenchSourceError && error.source==='cloud' && error.code==='origin_unavailable');
assert.equal(calls.length,hostedCount+1,'failed cloud does not retry local by slug');
assert.equal(calls.at(-1)?.headers.get('x-hugagent-target'),'cloud');
const path='/v1/workbench/applications/'+encodeURIComponent(local)+'/packages/rev/web/index.html';
assert.equal(new URL(packageAssetPath(path,local),'https://host.invalid').searchParams.get('hg_target'),'local');
assert.throws(()=>packageAssetPath(path,cloud),/package_binding_mismatch/);
assert.deepEqual(applicationOrigin(local),{kind:'local',id:scope});
assert.equal(applicationOrigin('plugin:reports'),undefined);
// Installation IDs belong to the upgrade endpoint, not the application namespace.
for (const prefix of ['/v1/workbench/', '/v1/workbench/admin/']) {
  for (const id of ['plugin:local:visualize', 'plugin:account_123:visualize', 'visualize@owner']) {
    for (const suffix of ['', '/decision', '?fresh=1']) {
      assert.equal(requestOrigin(prefix+'plugin-upgrades/'+encodeURIComponent(id)+suffix),undefined);
    }
  }
}
for (const route of ['applications/', 'me/application-state/']) {
  assert.throws(()=>requestOrigin('/v1/workbench/'+route+encodeURIComponent('plugin:local:visualize')),
    /invalid_application_key/,'installation IDs must still be rejected as application keys');
}
console.log('workbench source HTTP boundary: passed (simulated transport; not native/E2E)');
