/** Opt-in: native ACP CLI + real isolated Engine/model in an existing test container. */
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {Readable,Writable} from 'node:stream';
import {client,ndJsonStream} from '@agentclientprotocol/sdk';
import {writeFileSync} from 'node:fs';
const container=process.env.ACP_TEST_CONTAINER;
if(!container || !container.startsWith('hugagent-acp-test-')) throw new Error('Isolated test container required');
const child=spawn('docker',['exec','-i',container,'hugagent','--mode','local','--output','acp'],{stdio:['pipe','pipe','pipe']});
let stderr='';child.stderr.on('data',x=>{stderr+=x;});
const updates=[];let approvals=0;let reads=0;let writes=0;let holdPermission=false;let permissionArrived;let terminalCalls=0;
const peer=client({name:'native-acp-acceptance'})
 .onNotification('session/update',({params})=>updates.push(params))
 .onRequest('session/request_permission',({params})=>{approvals++;if(holdPermission){permissionArrived?.();return new Promise(()=>{});}return {outcome:{outcome:'selected',optionId:params.options.find(x=>x.kind==='allow_once').optionId}};})
 .onRequest('fs/write_text_file',({params})=>{assert.equal(params.path,'/attempt/workspace/client-written.txt');assert.equal(params.content.trim(),'CLIENT_WRITE_6042');writes++;execFileSync('docker',['exec',container,'/opt/acp-test/engine-python','-c','import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',params.path,params.content]);return {};})
 .onRequest('fs/read_text_file',({params})=>{assert.equal(params.path,'/attempt/workspace/client-only.txt');reads++;return {content:'ACP_CLIENT_SENTINEL_6042'};})
 .onRequest('terminal/create',({params})=>{assert.equal(params.command,'/bin/echo');assert.deepEqual(params.args,['ACP_TERMINAL_6042']);terminalCalls++;const output=execFileSync('docker',['exec',container,params.command,...params.args],{encoding:'utf8'});terminalOutput=output;return {terminalId:'live-terminal'};})
 .onRequest('terminal/wait_for_exit',()=>({exitCode:0}))
 .onRequest('terminal/output',()=>({output:terminalOutput,truncated:false,exitStatus:{exitCode:0}}))
 .onRequest('terminal/kill',()=>({}))
 .onRequest('terminal/release',()=>({}));
let terminalOutput='';
const engineCall=(method,path)=>JSON.parse(execFileSync('docker',['exec',container,'/opt/acp-test/engine-python','-c',`
import json,urllib.request,sys
r=json.load(open('/tmp/acp-private-home/engine.json'))
req=urllib.request.Request(r['url']+sys.argv[2],headers={'Authorization':'Bearer '+r['token']},method=sys.argv[1])
print(json.dumps(json.load(urllib.request.urlopen(req))))
`,method,path],{encoding:'utf8'}));
const connection=peer.connect(ndJsonStream(Writable.toWeb(child.stdin),Readable.toWeb(child.stdout)));
const agent=connection.agent;
const report={tests:[],approvals:0,clientReads:0};
const request=(method,params)=>agent.request(method,params,{timeout:240000});
try {
 const init=await request('initialize',{protocolVersion:1,clientCapabilities:{terminal:true,fs:{readTextFile:true,writeTextFile:true}}});
 assert.equal(init.protocolVersion,1);report.tests.push('initialize');
 const s=await request('session/new',{cwd:'/attempt/workspace',mcpServers:[]});
 const turn=await request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'Create the file /attempt/workspace/acp-acceptance.txt containing exactly ACP_NATIVE_ACCEPTANCE_6042. Use a write tool and actually save the file; do not just describe it.'}]});
 assert.equal(turn.stopReason,'end_turn');
 const saved=execFileSync('docker',['exec',container,'cat','/attempt/workspace/acp-acceptance.txt'],{encoding:'utf8'});
 assert.equal(saved.trim(),'ACP_NATIVE_ACCEPTANCE_6042');report.tests.push('native workspace write');
 const beforeWrite=approvals;
 await request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'Use client_write_text_file specifically to save exactly CLIENT_WRITE_6042 to /attempt/workspace/client-written.txt. This file belongs to the ACP client; do not use native file tools.'}]});
 assert.ok(approvals>beforeWrite,'Default ask must request permission for client write');assert.equal(writes,1);assert.equal(execFileSync('docker',['exec',container,'cat','/attempt/workspace/client-written.txt'],{encoding:'utf8'}).trim(),'CLIENT_WRITE_6042');report.tests.push('client write + ask permission');
 const before=approvals;
 await request('session/set_mode',{sessionId:s.sessionId,modeId:'full'});
 await request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'Use client_read_text_file specifically to read /attempt/workspace/client-only.txt. This virtual file only exists on the ACP client, so other read tools cannot read it. Return its exact content.'}]});
 assert.ok(reads>0,'Engine must execute negotiated ACP client tool');assert.equal(approvals,before);report.tests.push('client filesystem MCP bridge + full mode');
 await request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'Use client_run_command specifically to execute command /bin/echo with args ["ACP_TERMINAL_6042"]. Return its output. Do not use other tools.'}]});
 assert.equal(terminalCalls,1);report.tests.push('client terminal actual execution');
 await request('session/close',{sessionId:s.sessionId});
 const offset=updates.length;
 await request('session/load',{sessionId:s.sessionId,cwd:'/attempt/workspace',mcpServers:[]});
 assert.ok(updates.slice(offset).some(x=>x.update.sessionUpdate==='agent_message_chunk'));report.tests.push('history reload');
 await request('session/delete',{sessionId:s.sessionId});report.tests.push('delete');
 for (const [transport,name,port] of [['streamable-http','acceptance_http',18101],['sse','acceptance_sse',18102]]) {
  execFileSync('docker',['exec','-d',container,'/opt/acp-test/engine-python','/opt/hugagent/packages/cli/test/fixtures/mcp-echo.py',transport,name,String(port)]);
 }
 execFileSync('docker',['exec',container,'/opt/acp-test/engine-python','-c',`
import socket,time
for port in [18101,18102]:
 for _ in range(100):
  try:
   s=socket.create_connection(('127.0.0.1',port),timeout=1);s.close();break
  except OSError:time.sleep(.1)
 else:raise RuntimeError('MCP fixture did not start')
`]);
 const external=await request('session/new',{cwd:'/attempt/workspace',mcpServers:[
  {name:'test_stdio',command:'/opt/acp-test/engine-python',args:['/opt/hugagent/packages/cli/test/fixtures/mcp-echo.py','stdio','acceptance_stdio'],env:[]},
  {type:'http',name:'test_http',url:'http://127.0.0.1:18101/mcp',headers:[]},
  {type:'sse',name:'test_sse',url:'http://127.0.0.1:18102/sse',headers:[]},
 ]});
 await request('session/set_mode',{sessionId:external.sessionId,modeId:'full'});
 await request('session/prompt',{sessionId:external.sessionId,prompt:[{type:'text',text:'Call each of acceptance_stdio, acceptance_http, acceptance_sse exactly once with value="MCP_TRANSPORT_6042". Use these three tools directly; do not write any files yourself. Report all results.'}]});
 for(const name of ['acceptance_stdio','acceptance_http','acceptance_sse']) assert.equal(execFileSync('docker',['exec',container,'cat','/attempt/workspace/'+name+'.txt'],{encoding:'utf8'}),'MCP_TRANSPORT_6042');
 await request('session/delete',{sessionId:external.sessionId});report.tests.push('external stdio + HTTP + SSE MCP actual invocation');
 const c=await request('session/new' ,{cwd:'/attempt/workspace',mcpServers:[]});
 holdPermission=true;
 const arrived=new Promise(r=>{permissionArrived=r;});
 const writesBeforeCancel=writes;
 const pending=request('session/prompt',{sessionId:c.sessionId,prompt:[{type:'text',text:'Use client_write_text_file specifically to save exactly CLIENT_WRITE_6042 to /attempt/workspace/client-written.txt. This file belongs to the ACP client; do not use native file tools.'}]});
 await Promise.race([arrived,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error('No permission request')),90000);timer.unref();})]);
 await agent.notify('session/cancel',{sessionId:c.sessionId});assert.equal((await pending).stopReason,'cancelled');
 assert.equal(writes,writesBeforeCancel);
 await request('session/close',{sessionId:c.sessionId});report.tests.push('real permission wait cancellation prevents write');
 report.terminalCalls=terminalCalls;
 report.approvals=approvals;report.clientReads=reads;report.clientWrites=writes;report.ok=true;
} catch(error) {report.ok=false;report.error=String(error);report.updates=updates.slice(-12);report.stderr=stderr.slice(-2000);process.exitCode=1;}
finally {report.approvals=approvals;report.clientReads=reads;report.clientWrites=writes;connection.close();child.kill();writeFileSync(process.env.ACP_TEST_REPORT,JSON.stringify(report,null,2));console.log(JSON.stringify(report));}
