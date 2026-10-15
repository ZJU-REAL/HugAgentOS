import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
const scratch=resolve(process.env.WB21_SCRATCH || '../../.scratch/workbench-21');
const directory=mkdtempSync(join(scratch,'validation-'));
const cli=resolve('dist/tooling/cli.mjs');
const valid={schemaVersion:'1.0.0',id:'weather',title:'Weather',target:{kind:'external',mode:'browser',url:'https://example.org/weather'}};
const file=join(directory,'application.json');
writeFileSync(file,JSON.stringify(valid));
const run=()=>spawnSync(process.execPath,[cli,'validate',file],{encoding:'utf8'});
let result=run();
assert.equal(result.status,0,result.stdout+result.stderr);
assert.equal(JSON.parse(result.stdout).valid,true);
writeFileSync(file,JSON.stringify({...valid,target:{...valid.target,url:'javascript:alert(1)'}}));
result=run(); assert.equal(result.status,1,result.stdout+result.stderr);
assert.ok(JSON.parse(result.stdout).issues.some(i=>i.path==='/target/url'));
writeFileSync(file,'{');
result=run(); assert.equal(result.status,1);
assert.equal(JSON.parse(result.stdout).issues[0].code,'invalid_json');

const manifest=join(directory,'plugin.json');
const plugin={name:'reports',version:'1.0.0',extensions:{'org.hugagent':{ui:{version:3,contributes:{
 modules:[{id:'main',surface:'workspace',entry:'web/index.html'}],
 applications:[{schemaVersion:'1.0.0',id:'reports',title:'Reports',target:{kind:'plugin',moduleId:'main'}}]
}}}}};
writeFileSync(manifest,JSON.stringify(plugin));
result=spawnSync(process.execPath,[cli,'validate',manifest,'--assets',directory],{encoding:'utf8'});
assert.equal(result.status,1);
assert.ok(JSON.parse(result.stdout).issues.some(i=>i.code==='invalid_asset'));
plugin.extensions['org.hugagent'].ui.contributes.applications[0].target.moduleId='missing';
writeFileSync(manifest,JSON.stringify(plugin));
result=spawnSync(process.execPath,[cli,'validate',manifest],{encoding:'utf8'});
assert.equal(result.status,1);
assert.ok(JSON.parse(result.stdout).issues.some(i=>i.code==='invalid_module'));
result=spawnSync(process.execPath,[cli,'publish'],{encoding:'utf8'});
assert.equal(result.status,2,'There is no publish command');
console.log('manifest CLI boundary passed');
