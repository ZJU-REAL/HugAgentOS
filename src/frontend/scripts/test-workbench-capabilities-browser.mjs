import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawn} from 'node:child_process';
import {chromium} from 'playwright';
const root=resolve('../..'),scratch=resolve(root,'.scratch/workbench-09/http-browser'),output=resolve(scratch,'host');
await mkdir(output,{recursive:true});await mkdir(resolve(scratch,'tmp'),{recursive:true});
const origin='http://127.0.0.1:5299';
const python=(process.env.WB_TEST_PYTHON || resolve(root, '.venv/bin/python'));
const env={...process.env,PYTHONPATH:'src/backend',AUTH_MODE:'session',SESSION_STORE:'memory',SESSION_COOKIE_SECURE:'false',
 DATABASE_URL:'sqlite:///'+resolve(scratch,'browser-'+Date.now()+'.db'),STORAGE_PATH:resolve(scratch,'storage'),
 TMPDIR:resolve(scratch,'tmp'),WB09_FRONTEND:output};
await build({stdin:{contents:`
import {createRoot} from 'react-dom/client';
import {WorkspaceRuntime} from './src/workbench/WorkspaceRuntime';
import {WorkspaceApplication} from './src/workbench/WorkspaceApplication';
import {useApplicationDirectory} from './src/workbench/catalog';
import {openWorkspaceApplication} from './src/workbench/workspaceNavigation';
import {BrowserRouter,useNavigate,useLocation} from 'react-router';
import {useEffect} from 'react';
import './src/applicationWorkspaceActions';
import {useAuthStore} from './src/stores/authStore';
import {useComposerStore,readComposer} from './src/stores/composerStore';
import {bindRouter} from './src/routing/navigation';
import {reloadDirectory} from './src/workbench/catalog';
useAuthStore.setState({authUser:{user_id:'wb09',allowed_apps:null}});
useComposerStore.getState().resetForUser('wb09');
useComposerStore.getState().activate('chat:existing',{input:'Old draft'});
function Fixture(){const navigate=useNavigate(),location=useLocation();const directory=useApplicationDirectory();
useEffect(()=>bindRouter({navigate}),[navigate]);
useEffect(()=>{document.getElementById('navigation').textContent=location.pathname;},[location]);
return <><WorkspaceRuntime/>{location.pathname.startsWith('/apps')?<WorkspaceApplication/>:directory.items.filter(item=>item.declaration.target.kind==='plugin').map(item=><button key={item.applicationKey} onClick={()=>openWorkspaceApplication(item.applicationKey)}>{item.declaration.title}</button>)}</>;}
window.fixture={draft:()=>readComposer('chat:existing').input,reload:reloadDirectory};
createRoot(document.getElementById('root')).render(<BrowserRouter><Fixture/></BrowserRouter>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'host.js'),bundle:true,format:'esm',jsx:'automatic',
 loader:{'.css':'empty'},define:{'import.meta.env':'{}'}});
await writeFile(resolve(output,'index.html'),'<div id="root"></div><p id="navigation"></p><script type="module" src="/host.js"></script>');
await build({stdin:{contents:`
import {connect} from '@hugagent/workbench-sdk';
(async()=>{const c=await connect();window.connection=c;document.querySelector('h1').textContent='Ready';
window.run=(method,params)=>{document.querySelector('output').textContent='pending';
return c.request(method,params).then(v=>{document.querySelector('output').textContent=JSON.stringify(v);return v;},
e=>{document.querySelector('output').textContent=e.message;return {error:e.message};});};})();
`,resolveDir:resolve(root,'packages/workbench-sdk')},outfile:resolve(scratch,'plugin.js'),bundle:true,format:'iife'});
let server,log='';
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({acceptDownloads:true});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
async function allow(){await page.getByRole('button',{name:'允许',exact:true}).click();}
async function run(method,params){
 await page.frames()[1].evaluate(({method,params})=>{void window.run(method,params);},{method,params});
}
async function result(){const output=page.frameLocator('iframe').locator('output');await output.filter({hasNotText:'pending'}).waitFor();return output.innerText();}
try{
 server=spawn(python,['-m','uvicorn','tests.features.workbench.workbench_capabilities_http_fixture:app','--host','127.0.0.1','--port','5299'],{cwd:root,env,stdio:['ignore','pipe','pipe']});
 server.stdout.on('data',b=>log+=b);server.stderr.on('data',b=>log+=b);
 for(let n=0;n<100;n++){try{if((await fetch(origin+'/ready')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));if(n===99)throw new Error(log);}
 const login=await fetch(origin+'/fixture/session',{method:'POST'}),cookie=login.headers.get('set-cookie').split(';')[0];
 const split=cookie.indexOf('=');await page.context().addCookies([{name:cookie.slice(0,split),value:cookie.slice(split+1),url:origin,httpOnly:true,sameSite:'Lax'}]);
 const setup=await (await fetch(origin+'/fixture/setup',{method:'POST'})).json();
 const zip=await fetch(origin+'/fixture/package',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({html:'<h1>Loading</h1><output></output><script>'+await readFile(resolve(scratch,'plugin.js'),'utf8')+'</script>'})});
 const form=new FormData();form.append('file',new Blob([await zip.arrayBuffer()]),'plugin.zip');
 const upload=await fetch(origin+'/api/v1/plugins/import',{method:'POST',headers:{Cookie:cookie},body:form});
 assert.equal(upload.status,201,await upload.clone().text());const install=(await upload.json()).data.install_id;
 await page.goto(origin);await page.getByRole('button',{name:'WB09 capabilities',exact:true}).click();
 await page.frameLocator('iframe').getByRole('heading',{name:'Ready'}).waitFor();
 await run('data.query',{source:'rows'});
 await page.getByRole('dialog',{name:'批准插件能力'}).waitFor();
 // A script click cannot approve. The real Playwright click produces a trusted host action.
 await page.getByRole('button',{name:'允许',exact:true}).evaluate(button=>button.click());
 assert.equal(await page.getByRole('dialog').count(),1);
 await allow();
 assert.deepEqual(JSON.parse(await result()),{rows:[{id:7,name:'Real HTTP fixture row'}]});
 await run('data.query',{source:'other'});assert.equal(await result(),'data_source_denied');
 await run('file.pick',{});
 await page.getByRole('dialog',{name:'选择供插件使用的文件'}).waitFor();
 assert.equal(await page.locator('option').filter({hasText:'other-private-file.txt'}).count(),0);
 await page.locator('select').selectOption(setup.fileId);await allow();
 const picked=JSON.parse(await result());assert.equal(picked.fileId,setup.fileId);assert.equal(picked.name,'wb09-real-file.txt');
 assert.deepEqual(Object.keys(picked).sort(),['fileId','mimeType','name','size']);
 await run('file.save',{fileId:picked.fileId});
 await page.getByRole('dialog',{name:'保存插件文件'}).waitFor();
 const downloaded=page.waitForEvent('download');await allow();const download=await downloaded;
 assert.equal(download.suggestedFilename(),'wb09-real-file.txt');
 const bytes=await readFile(await download.path());assert.equal(bytes.toString(),'WB09 real file bytes');assert.deepEqual(JSON.parse(await result()),{saved:true});
 await run('chat.compose',{text:'Plugin draft'});
 await page.getByRole('dialog',{name:'填写聊天草稿'}).waitFor();
 assert.equal(await page.evaluate(()=>window.fixture.draft()),'Old draft');
 await allow();assert.deepEqual(JSON.parse(await result()),{composed:true});
 assert.equal(await page.evaluate(()=>window.fixture.draft()),'Plugin draft');
 await run('navigation.open',{applicationKey:'builtin:settings'});
 await page.getByRole('dialog',{name:'打开另一个应用'}).waitFor();await allow();
 assert.deepEqual(JSON.parse(await result()),{opened:true});assert.equal(await page.locator('#navigation').innerText(),'/settings');
 await run('chat.compose',{text:'Cancelled replacement'});
 await page.getByRole('dialog',{name:'填写聊天草稿'}).waitFor();await page.getByRole('button',{name:'取消',exact:true}).click();
 assert.equal(await result(),'cancelled');assert.equal(await page.evaluate(()=>window.fixture.draft()),'Plugin draft');
 const info=await page.frames()[1].evaluate(()=>window.connection.info);
 assert.equal(JSON.stringify(info).includes('approvalId'),false);assert.deepEqual(Object.keys(info.context).sort(),['applicationKey','executionScope','installId','instanceId','origin','packageRevision']);
 await run('native.invoke',{});assert.equal(await result(),'unknown_method');
 await fetch(origin+'/api/v1/plugins/installed/'+encodeURIComponent(install)+'/enable',{method:'PATCH',
 headers:{Cookie:cookie,'Content-Type':'application/json'},body:'{"enabled":false}'});
 await page.evaluate(()=>window.fixture.reload());await page.waitForFunction(()=>!document.querySelector('iframe'));
 assert.deepEqual(errors,[]);
 await writeFile(resolve(scratch,'results.json'),JSON.stringify({level:'isolated real session HTTP + actual storage/upstream + Chromium',passed:true,data:true,fileBytes:true,hostConfirmation:true,draft:true,navigation:true,revoke:true},null,2));
 console.log('Real session HTTP/browser: data query, precise datasource, file picker/save bytes, trusted host confirmations, draft, navigation, cancel, native denial, disable passed');
}finally{
 await browser.close();
 if(server?.exitCode===null){const stopped=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await stopped;}
 await writeFile(resolve(scratch,'server.log'),log);
}
