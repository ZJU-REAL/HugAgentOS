import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawn} from 'node:child_process';
import {chromium} from 'playwright';

// Real components + actual diagnostic HTTP routes; mocked identity, real SQLite.
const root=resolve('../..');
const folder=resolve(root,'.scratch/workbench-19/browser-'+Date.now());
const assets=resolve(folder,'assets');
const origin='http://127.0.0.1:5219';
await mkdir(assets,{recursive:true});
await build({stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {useState} from 'react';
import {DiagnosticsPanel} from './src/workbench/DiagnosticsPanel';
import {DiagnosticFailure} from './src/workbench/DiagnosticFailure';
import {apiRequest} from './src/api';
function Fixture(){
 const [error,setError]=useState(null);
 const [attempts,setAttempts]=useState(0);
 async function open(){setAttempts(n=>n+1);try{await apiRequest('/v1/workbench/applications/external:absent/launch',{method:'POST',body:'{}'});}catch(e){setError(e);}}
 return <><button onClick={open}>Launch missing app</button><output aria-label="Attempts">{attempts}</output>
 {error && <DiagnosticFailure error={error} onRetry={open}/>}<DiagnosticsPanel token="wb19-http-config"/></>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(assets,'fixture.js'),
 bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},loader:{'.css':'empty'}});
await writeFile(resolve(assets,'index.html'),'<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
const server=spawn((process.env.WB_TEST_PYTHON || resolve(root, '.venv/bin/python')),
 ['-m','uvicorn','tests.features.workbench.workbench_diagnostic_http_fixture:app','--host','127.0.0.1','--port','5219'],
 {cwd:root,env:{...process.env,PYTHONPATH:'src/backend',AUTH_MODE:'mock',CONFIG_TOKEN:'wb19-http-config',
 DATABASE_URL:'sqlite:///'+resolve(folder,'db.sqlite'),STORAGE_PATH:resolve(folder,'storage'),
 TMPDIR:folder,WB19_ASSET_PATH:assets},stdio:['ignore','pipe','pipe']});
let serverLog='';server.stdout.on('data',b=>{serverLog+=b;});server.stderr.on('data',b=>{serverLog+=b;});
let browser;
try {
 for(let n=0;n<200;n++){
  if(server.exitCode!==null)throw new Error('Fixture exited: '+serverLog);
  try{if((await fetch(origin+'/ready')).ok)break;}catch{}
  if(n===199)throw new Error('Fixture did not start');
  await new Promise(r=>setTimeout(r,100));
 }
 browser=await chromium.launch({headless:true});
 const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']});
 const page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);
 await page.getByRole('button',{name:'Launch missing app'}).click();
 await page.getByText('诊断编号：',{exact:false}).waitFor();
 const id=await page.locator('[role="alert"] code').textContent();
 assert.match(id,/^[a-f0-9]{32}$/);
 await page.getByRole('button',{name:'复制编号',exact:true}).click();
 assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),id);
 await page.getByRole('button',{name:'重试',exact:true}).click();
 await page.getByLabel('Attempts').filter({hasText:'2'}).waitFor();
 const panel=page.getByRole('region',{name:'应用诊断',exact:true});
 await panel.getByRole('button',{name:'刷新',exact:true}).click();
 await panel.getByText(id,{exact:true}).waitFor();
 await panel.getByLabel('用于权限检查的用户 ID').fill('wb19-http');
 await panel.getByRole('button',{name:'检查权限',exact:true}).click();
 await panel.getByText(/authorized/).first().waitFor();
 await panel.getByLabel('外部应用声明预检查（JSON）').fill(JSON.stringify({
 schemaVersion:'1.0.0',id:'valid_preview',title:'Preview',
 target:{kind:'external',mode:'browser',url:'https://reports.example.org'}}));
 await panel.getByRole('button',{name:'校验但不保存',exact:true}).click();
 await panel.getByText('配置有效。未保存任何变更。',{exact:true}).waitFor();
 const headers={'Authorization':'Bearer wb19-http-config','Content-Type':'application/json'};
 const result=await fetch(origin+'/api/v1/workbench/admin/external-applications',{headers});
 assert.equal((await result.json()).data.items.length,0);
 assert.deepEqual(errors,[]);
 console.log('PASS WB19 real HTTP/SQLite: admin permissions, preview without writes, failure ID, clipboard, executable retry');
}finally{
 if(browser)await browser.close();
 if(server.exitCode===null){const done=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await done;}
}
