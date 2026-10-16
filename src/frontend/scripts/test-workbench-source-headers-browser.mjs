import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdtemp,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {chromium} from 'playwright';
const root=resolve('../..'),scratch=await mkdtemp(resolve(root,'.scratch/workbench-final-review/headers-'));
await build({stdin:{contents:`
import {workbenchRequest,workbenchFetch,setHostedWorkbenchOrigin} from './src/workbench/sourceRequest';
window.probe=async(kind,name,value,revision,target='cloud',authorized=true)=>{
 let headers={...(name?{[name]:value}:{}),Authorization:authorized?'Bearer header-user':'invalid',
  'X-HugAgentOS-Target':target==='cloud'?'local':'cloud'};
 if(kind==='headers')headers=new Headers(headers);
 if(kind==='tuples')headers=Object.entries(headers);
 if(target==='local')setHostedWorkbenchOrigin({kind:'local',id:'a'.repeat(32)});
 try{return {ok:true,result:await workbenchRequest('/v1/workbench/me/navigation-preferences',{
   method:'PUT',headers,body:JSON.stringify({revision,pinned:['builtin:projects'],order:['builtin:projects']})},target)};}
 catch(error){return {ok:false,status:error.status,code:error.payload?.detail?.code};}
};
window.rawProbe=async()=> (await workbenchFetch('/v1/workbench/me/navigation-preferences',{
 method:'PUT',headers:new Headers({'Content-Type':'text/plain',Authorization:'Bearer header-user'}),
 body:JSON.stringify({revision:0,pinned:[],order:[]})},'cloud')).status;
window.exactProbe=async()=>{
 const key='plugin:cloud:'+ 'a'.repeat(32)+':'+ 'b'.repeat(64)+':20261014T120000.000000Z:reports';
 try{await workbenchRequest('/v1/workbench/applications/'+encodeURIComponent(key),{
 headers:[['Authorization','Bearer header-user'],['X-HugAgentOS-Target','local'],['X-Workbench-Origin','forged']]});}
 catch(error){return error.status;}
};
`,resolveDir:process.cwd(),loader:'ts'},outfile:resolve(scratch,'fixture.js'),bundle:true,format:'esm',define:{'import.meta.env':'{}'}});
await writeFile(resolve(scratch,'backend.py'),`
import sys
from pathlib import Path
root=Path(sys.argv[1]);scratch=Path(sys.argv[2]);port=int(sys.argv[3])
sys.path.insert(0,str(root/'src/backend'))
import dotenv
dotenv.dotenv_values=lambda *args,**kwargs:{}
dotenv.load_dotenv=lambda *args,**kwargs:False
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,HTMLResponse
from core.db.engine import Base,engine,SessionLocal
from core.db.models import UserShadow,ContentBlock
from core.auth.backend import UserContext,get_current_user
from api.routes.v1.workbench import router
Base.metadata.create_all(engine)
with SessionLocal() as db:
 db.add(UserShadow(user_id='header-user',username='Header user',extra_data={}))
 db.add(ContentBlock(id='workbench_origin_identity',payload={'id':'a'*32}));db.commit()
app=FastAPI();app.include_router(router,prefix='/api');traces=[]
def authenticated(request:Request):
 if request.headers.get('authorization')!='Bearer header-user':
  raise HTTPException(403,detail={'code':'permission_denied'})
 return UserContext(user_id='header-user',user_center_id='header-user',username='Header user')
app.dependency_overrides[get_current_user]=authenticated
@app.middleware('http')
async def trace(request,call_next):
 if request.url.path.startswith('/api/'):
  body=await request.body()
  traces.append({'method':request.method,'contentType':request.headers.get('content-type'),
   'rawTypes':[v.decode() for k,v in request.scope['headers'] if k==b'content-type'],
   'target':request.headers.get('x-hugagent-target'),'origin':request.headers.get('x-workbench-origin'),
   'authorization':request.headers.get('authorization'),'body':body.decode()})
 return await call_next(request)
@app.get('/')
def index():return HTMLResponse('<script type="module" src="/fixture.js"></script>')
@app.get('/fixture.js')
def script():return FileResponse(scratch/'fixture.js',media_type='application/javascript')
@app.get('/trace')
def trace_data():return traces
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=port,log_level='error')
`);
const listener=createServer();await new Promise(done=>listener.listen(0,'127.0.0.1',done));
const port=listener.address().port;await new Promise(done=>listener.close(done));
const origin='http://127.0.0.1:'+port;
const env={PATH:process.env.PATH,HOME:process.env.HOME,LANG:'C.UTF-8',
 DATABASE_URL:'sqlite:///'+resolve(scratch,'isolated.sqlite'),AUTH_MODE:'mock',APP_EDITION:'ee',KNOWLEDGE_BASE:'none'};
const python=process.env.WB_TEST_PYTHON||(process.env.WB_TEST_PYTHON || resolve(root, '.venv/bin/python'));
const server=spawn(python,[resolve(scratch,'backend.py'),root,scratch,String(port)],{cwd:root,env,stdio:['ignore','pipe','pipe']});
let log='';server.stdout.on('data',data=>log+=data);server.stderr.on('data',data=>log+=data);
let browser;
try{
 for(let n=0;n<80;n++){
  if(await fetch(origin).then(response=>response.ok).catch(()=>false))break;
  if(server.exitCode!==null)throw Error(log);
  await new Promise(done=>setTimeout(done,100));
 }
 browser=await chromium.launch({headless:true});const page=await browser.newPage();await page.goto(origin);
 let revision=0;
 for(const kind of ['record','headers','tuples'])for(const name of ['Content-Type','content-type','CONTENT-TYPE']){
  const result=await page.evaluate(({kind,name,revision})=>window.probe(kind,name,'application/json',revision),{kind,name,revision});
  assert.equal(result.ok,true,JSON.stringify(result));revision++;assert.equal(result.result.data.revision,revision);
 }
 assert.equal((await page.evaluate(revision=>window.probe('record',null,null,revision),revision)).ok,true);revision++;
 assert.equal((await page.evaluate(()=>window.probe('record','content-type','application/json',0))).status,409);
 assert.equal((await page.evaluate(revision=>window.probe('tuples','content-type','text/plain',revision),revision)).status,415);
 assert.equal((await page.evaluate(revision=>window.probe('headers','CONTENT-TYPE','application/json',revision,'cloud',false),revision)).status,403);
 assert.equal(await page.evaluate(()=>window.rawProbe()),415);
 assert.equal((await page.evaluate(revision=>window.probe('record','content-type','application/json',revision,'local'),revision)).ok,true);
 assert.equal(await page.evaluate(()=>window.exactProbe()),404);
 const traces=await fetch(origin+'/trace').then(response=>response.json());
 for(const trace of traces.filter(trace=>trace.method==='PUT')){
  assert.equal(trace.rawTypes.length,1);assert.ok(!trace.contentType.includes(','));
  assert.ok(Array.isArray(JSON.parse(trace.body).pinned));
 }
 assert.equal(traces.filter(trace=>trace.method==='PUT').at(-1).target,'local');
 assert.equal(traces.at(-1).target,'cloud');assert.equal(traces.at(-1).origin,'a'.repeat(32));
 assert.ok(traces.slice(0,9).every(trace=>trace.target==='cloud'&&trace.authorization==='Bearer header-user'));
 console.log('Browser → real strict Workbench HTTP passed: 9 HeadersInit/casing JSON writes, default, CAS, 403/415, body and exact cloud/local headers.');
}finally{
 await browser?.close();server.kill('SIGTERM');
 await new Promise(done=>{if(server.exitCode!==null)return done();server.once('exit',done);});
}
