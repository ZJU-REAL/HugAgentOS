import {Client as McpClient} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { Readable, Writable } from 'node:stream';
import { startFakeServer } from './engine-fake-server.mjs';
import { client, ndJsonStream } from '@agentclientprotocol/sdk';

export function launch(t, extra = [], env = {}, permissionHandler = () => ({ outcome: { outcome: 'selected', optionId: 'deny' } }), handlers = {}) {
  const child = spawn(process.execPath, ['dist/main.js', '--output', 'acp', ...extra], {
    env: { ...process.env, ...env }, stdio: ['pipe', 'pipe', 'pipe'],
  });
  let errors = '';
  child.stderr.on('data', b => { errors += b; });
  const updates = [];
  const app = client({ name: 'hugagent-protocol-test' })
    .onRequest('session/request_permission', permissionHandler)
    .onNotification('session/update', ({ params }) => { updates.push(params); });
  for (const [method, handler] of Object.entries(handlers)) app.onRequest(method, handler);
  const connection = app.connect(ndJsonStream(Writable.toWeb(child.stdin), Readable.toWeb(child.stdout)));
  t.after(() => { connection.close(); child.kill(); });
  return { child, app, connection, agent: connection.agent, updates, errors: () => errors };
}

test('ACP initializes without launching an engine and rejects pre-initialization sessions', { timeout: 5000 }, async t => {
  const { agent } = launch(t, [], { HUGAGENT_ENGINE_CMD: '["/nonexistent-engine"]' });
  await assert.rejects(agent.request('session/new', { cwd: process.cwd(), mcpServers: [] }));
  const result = await agent.request('initialize', { protocolVersion: 1, clientCapabilities: {} });
  assert.equal(result.protocolVersion, 1);
  assert.equal(result.agentInfo.name, 'hugagent');
  await assert.rejects(agent.request('initialize', { protocolVersion: 1, clientCapabilities: {} }));
});

test('ACP runs multiple turns, isolates session models, reports tools, and cancels a running turn', { timeout: 10000 }, async t => {
  const server = await startFakeServer();
  t.after(() => server.close());
  const { agent, updates } = launch(t, ['--mode', 'cloud', '--server', server.url], {
    HUGAGENT_API_KEY: 'sk-jx-test-token', HUGAGENT_ALLOW_HTTP: '1',
  });
  await agent.request('initialize', { protocolVersion: 1, clientCapabilities: {} });
  const a = await agent.request('session/new', { cwd: process.cwd(), mcpServers: [] });
  const b = await agent.request('session/new', { cwd: process.cwd(), mcpServers: [] });
  assert.notEqual(a.sessionId, b.sessionId);
  await agent.request('session/set_config_option', { sessionId: a.sessionId, configId: 'model', value: 'p2:two' });
  for (const session of [a, a, b]) {
    const result = await agent.request('session/prompt', { sessionId: session.sessionId, prompt: [{ type: 'text', text: 'hello' }] });
    assert.equal(result.stopReason, 'end_turn');
  }
  const sends = server.state.requests.filter(x => x.path === '/v1/agents/responses');
  assert.equal(sends[0].body.model_provider_id, 'p2');
  assert.equal(sends[2].body.model_provider_id, 'p1');
  assert.ok(updates.some(x => x.update.sessionUpdate === 'tool_call'));
  assert.ok(updates.some(x => x.update.sessionUpdate === 'tool_call_update'));
  const pending = agent.request('session/prompt', { sessionId: a.sessionId, prompt: [{ type: 'text', text: 'hang' }] });
  while (!server.state.waiters.has('cancel')) await new Promise(r => setTimeout(r, 10));
  await assert.rejects(agent.request('session/prompt', { sessionId: a.sessionId, prompt: [{ type: 'text', text: 'overlap' }] }));
  await agent.notify('session/cancel', { sessionId: a.sessionId });
  assert.equal((await pending).stopReason, 'cancelled');
  assert.equal(server.state.cancels.length, 1);
  assert.equal(server.state.requests.filter(x => x.method === 'PUT' && x.path === '/v1/tool-approval').length, 0);
});

test('ACP approval waits are cancellable and reject late allow replies', { timeout: 10000 }, async t => {
  const server = await startFakeServer(); t.after(() => server.close());
  let asked; const arrived = new Promise(r => { asked = r; });
  let answer;
  const peer = launch(t, ['--mode', 'cloud', '--server', server.url], {
    HUGAGENT_API_KEY: 'sk-jx-test-token', HUGAGENT_ALLOW_HTTP: '1',
  }, () => { asked(); return new Promise(r => { answer = r; }); });
  await peer.agent.request('initialize', { protocolVersion: 1, clientCapabilities: {} });
  const s = await peer.agent.request('session/new', { cwd: process.cwd(), mcpServers: [] });
  const pending = peer.agent.request('session/prompt', { sessionId: s.sessionId, prompt: [{ type: 'text', text: 'approve' }] });
  await arrived;
  await peer.agent.notify('session/cancel', { sessionId: s.sessionId });
  assert.equal((await pending).stopReason, 'cancelled');
  answer({ outcome: { outcome: 'selected', optionId: 'allow' } });
  const next = await peer.agent.request('session/prompt', { sessionId: s.sessionId, prompt: [{ type: 'text', text: 'hello' }] });
  assert.equal(next.stopReason, 'end_turn');
  assert.equal(server.state.confirms.length, 0);
});

test('ACP loads, lists, closes and deletes sessions through authorized engine APIs', { timeout: 10000 }, async t => {
  const server = await startFakeServer(); t.after(() => server.close());
  const { agent, updates } = launch(t, ['--mode', 'cloud', '--server', server.url], {
    HUGAGENT_API_KEY: 'sk-jx-test-token', HUGAGENT_ALLOW_HTTP: '1',
  });
  const init = await agent.request('initialize', { protocolVersion: 1, clientCapabilities: {} });
  assert.equal(init.agentCapabilities.loadSession, true);
  await agent.request('session/load', { sessionId: 'cloud1', cwd: process.cwd(), mcpServers: [] });
  assert.deepEqual(updates.filter(x => x.update.sessionUpdate.endsWith('message_chunk')).map(x => x.update.content.text), ['first', 'second']);
  const listed = await agent.request('session/list', { cwd: process.cwd() });
  assert.equal(listed.sessions[0].sessionId, 'cloud1');
  await agent.request('session/close', { sessionId: 'cloud1' });
  await assert.rejects(agent.request('session/prompt', { sessionId: 'cloud1', prompt: [{ type: 'text', text: 'hello' }] }));
  await agent.request('session/resume', { sessionId: 'cloud1', cwd: process.cwd(), mcpServers: [] });
  await agent.request('session/delete', { sessionId: 'cloud1' });
  assert.ok(server.state.requests.some(x => x.path === '/v1/chats/cloud1' && x.method === 'DELETE'));
});


test('ACP permission presets are leased per session and supplied on each turn', {timeout: 10000}, async t => {
  const server = await startFakeServer(); t.after(() => server.close());
  const {agent} = launch(t, ['--mode', 'cloud', '--server', server.url], {HUGAGENT_API_KEY: 'sk-jx-test-token'});
  await agent.request('initialize', {protocolVersion: 1, clientCapabilities: {}});
  const a = await agent.request('session/new', {cwd: process.cwd(), mcpServers: []});
  const b = await agent.request('session/new', {cwd: process.cwd(), mcpServers: []});
  assert.equal(server.state.leases.get(a.sessionId)?.mode, 'ask');
  await agent.request('session/set_mode', {sessionId: a.sessionId, modeId: 'full'});
  assert.equal(server.state.leases.get(a.sessionId).mode, 'full');
  assert.equal(server.state.leases.get(b.sessionId).mode, 'ask');
  await agent.request('session/prompt', {sessionId: a.sessionId, prompt: [{type:'text',text:'hello'}]});
  assert.equal(server.state.requests.find(x => x.path === '/v1/agents/responses').body.client_session_id, 'a'.repeat(32));
  await agent.request('session/close', {sessionId: a.sessionId});
  assert.equal(server.state.leases.has(a.sessionId), false);
});


test('ACP cancellation before run_started waits for admission and cancels the accepted run', {timeout: 10000}, async t => {
  const server = await startFakeServer(); t.after(() => server.close());
  const {agent} = launch(t, ['--mode','cloud','--server',server.url], {HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const s = await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  const pending = agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'delayed-admission'}]});
  while (!server.state.waiters.has('admit')) await new Promise(r => setTimeout(r,5));
  await agent.notify('session/cancel',{sessionId:s.sessionId});
  await new Promise(r => setTimeout(r,30));
  server.state.waiters.get('admit')();
  assert.equal((await pending).stopReason,'cancelled');
  assert.deepEqual(server.state.cancels,['run-'+s.sessionId]);
});

test('ACP rejects concurrent loads of the same session', {timeout:10000}, async t => {
  const server = await startFakeServer(); t.after(() => server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const params={sessionId:'cloud1',cwd:process.cwd(),mcpServers:[]};
  const results=await Promise.allSettled([agent.request('session/resume',params),agent.request('session/resume',params)]);
  assert.equal(results.filter(x=>x.status==='fulfilled').length,1);
});


test('ACP questions are answered through negotiated elicitation or explicitly skipped', {timeout:10000}, async t => {
  const server=await startFakeServer(); t.after(()=>server.close());
  for (const enabled of [false,true]) {
    const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'},undefined,
      {'elicitation/create':()=>({action:'accept',content:{a:'yes',b:'because'}})});
    await agent.request('initialize',{protocolVersion:1,clientCapabilities:enabled?{elicitation:{form:{}}}:{}});
    const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
    const result=await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'question'}]});
    assert.equal(result.stopReason,'end_turn');
    const answers=server.state.answers.at(-1).answers;
    if (enabled) assert.equal(answers[0].custom,'yes'); else assert.equal(answers[0].skipped,true);
  }
});


test('ACP mounts supplied MCP servers on the session lease without global writes', {timeout:10000}, async t => {
  const server=await startFakeServer(); t.after(()=>server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  const init=await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  assert.equal(init.agentCapabilities.mcpCapabilities.http,true);
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[{name:'demo',command:'/bin/demo',args:['--stdio'],env:[{name:'DEMO',value:'test'}]}]});
  assert.deepEqual(server.state.leases.get(s.sessionId).servers.demo,{transport:'stdio',command:'/bin/demo',args:['--stdio'],env:{DEMO:'test'}});
  await assert.rejects(agent.request('session/new',{cwd:process.cwd(),mcpServers:[{name:'same',command:'/bin/a',args:[],env:[]},{name:'same',command:'/bin/b',args:[],env:[]}]}));
});


test('ACP negotiated file tools route through the client and reject workspace escape', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const reads=[];
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'},undefined,
    {'fs/read_text_file':({params})=>{reads.push(params);return {content:'client file'};}});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{fs:{readTextFile:true}}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  const bridge=server.state.leases.get(s.sessionId).servers.acp_client;
  assert.ok(bridge);
  const mcp=new McpClient({name:'test',version:'1'});
  await mcp.connect(new StreamableHTTPClientTransport(new URL(bridge.url),{requestInit:{headers:bridge.headers}}));
  t.after(()=>mcp.close());
  assert.deepEqual((await mcp.listTools()).tools.map(x=>x.name),['client_read_text_file']);
  const pending=agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'hang'}]});
  while (!server.state.waiters.has('cancel')) await new Promise(r=>setTimeout(r,5));
  const response=await mcp.callTool({name:'client_read_text_file',arguments:{path:process.cwd()+'/file.txt'}});
  assert.equal(response.isError,undefined);
  assert.match(response.content[0].text,/client file/);
  const denied=await mcp.callTool({name:'client_read_text_file',arguments:{path:'/etc/passwd'}});
  assert.equal(denied.isError,true);
  assert.equal(reads.length,1);
  await agent.notify('session/cancel',{sessionId:s.sessionId}); await pending;
  assert.equal((await mcp.callTool({name:'client_read_text_file',arguments:{path:process.cwd()+'/file.txt'}})).isError,true);
});


test('ACP deletes a listed session without loading it', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  await agent.request('session/list',{cwd:process.cwd()});
  await agent.request('session/delete',{sessionId:'cloud1'});
  assert.ok(server.state.requests.some(x=>x.method==='DELETE'&&x.path==='/v1/chats/cloud1'));
});


test('ACP uploads inline images as attachments instead of base64 prompt text', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  const init=await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  assert.equal(init.agentCapabilities.promptCapabilities.image,true);
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'describe'},{type:'image',data:'iVBORw0KGgo=',mimeType:'image/png'}]});
  const request=server.state.requests.find(x=>x.path==='/v1/agents/responses');
  assert.equal(request.body.attachments[0].file_id,'upload1');
  assert.doesNotMatch(request.body.message,/iVBOR/);
  // The personal-file namespace is shared across turns and chats. Repeated inline
  // images must not collide with previously uploaded files or overwrite them.
  await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'image',data:'iVBORw0KGgo=',mimeType:'image/png'}]});
  const other=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  await agent.request('session/prompt',{sessionId:other.sessionId,prompt:[{type:'image',data:'iVBORw0KGgo=',mimeType:'image/png'}]});
  assert.equal(new Set(server.state.uploads).size,3);
  assert.ok(server.state.uploads.every(name=>name.endsWith('.png')));
  const sends=server.state.requests.filter(x=>x.path==='/v1/agents/responses');
  assert.deepEqual(sends.map(x=>x.body.attachments[0].file_id),['upload1','upload2','upload3']);
  await assert.rejects(agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'image',data:'!!!',mimeType:'image/png'}]}));
});


test('ACP publishes plan and context usage and executes compact without a model turn', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent,updates}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'events'}]});
  assert.equal(updates.find(x=>x.update.sessionUpdate==='plan').update.entries[0].status,'completed');
  assert.equal(updates.find(x=>x.update.sessionUpdate==='usage_update').update.used,120);
  await agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'/compact'}]});
  assert.ok(server.state.requests.some(x=>x.path.endsWith('/compact')));
  assert.equal(server.state.requests.filter(x=>x.path==='/v1/agents/responses').length,1);
});


test('ACP EOF cancels an owned run and releases its lease', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent,child}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  const pending=agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'hang'}]}).catch(()=>{});
  while(!server.state.waiters.has('cancel')) await new Promise(r=>setTimeout(r,5));
  const exited=new Promise(r=>child.once('exit',r));child.stdin.end();await exited;await pending;
  assert.deepEqual(server.state.cancels,['run-'+s.sessionId]);
  assert.equal(server.state.leases.has(s.sessionId),false);
});

test('ACP terminals are negotiated and killed/released when a turn is cancelled', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const terminalCalls=[];
  const handlers={
    'terminal/create':()=>{terminalCalls.push('create');return {terminalId:'terminal-1'};},
    'terminal/wait_for_exit':()=>{terminalCalls.push('wait');return new Promise(()=>{});},
    'terminal/kill':()=>{terminalCalls.push('kill');return {};},
    'terminal/release':()=>{terminalCalls.push('release');return {};},
  };
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'},undefined,handlers);
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{terminal:true}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  const bridge=server.state.leases.get(s.sessionId).servers.acp_client;
  const mcp=new McpClient({name:'terminal-test',version:'1'});
  await mcp.connect(new StreamableHTTPClientTransport(new URL(bridge.url),{requestInit:{headers:bridge.headers}}));t.after(()=>mcp.close());
  const pending=agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'hang'}]});
  while(!server.state.waiters.has('cancel')) await new Promise(r=>setTimeout(r,5));
  const tool=mcp.callTool({name:'client_run_command',arguments:{command:'test-command',args:[]}});
  while(!terminalCalls.includes('wait')) await new Promise(r=>setTimeout(r,5));
  await agent.notify('session/cancel',{sessionId:s.sessionId});await pending;await tool;
  assert.deepEqual(terminalCalls,['create','wait','kill','release']);
});


test('ACP client permission errors cancel the backend run and do not expose peer error details', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'},()=>{throw new Error('SENSITIVE_TEST_VALUE');});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  await assert.rejects(agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'approve'}]}),error=>!JSON.stringify(error).includes('SENSITIVE_TEST_VALUE'));
  assert.deepEqual(server.state.cancels,['run-'+s.sessionId]);
});


test('ACP explicit initial permission policy is applied to a new session', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url,'--permission-mode','full'],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  assert.equal(s.modes.currentModeId,'full');assert.equal(server.state.leases.get(s.sessionId).mode,'full');
});


test('ACP load replays all history pages in order', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent,updates}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  await agent.request('session/load',{sessionId:'longhistory',cwd:process.cwd(),mcpServers:[]});
  const text=updates.filter(x=>x.update.sessionUpdate.endsWith('message_chunk')).map(x=>x.update.content.text);
  assert.equal(text.length,102);assert.equal(text[0],'message-0');assert.equal(text[101],'message-101');
});


test('ACP cancellation with a lost admission receipt revokes the lease even on clean EOF', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'});
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  const pending=agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'lost-admission'}]});
  while(!server.state.waiters.has('lost')) await new Promise(r=>setTimeout(r,5));
  await agent.notify('session/cancel',{sessionId:s.sessionId});
  await new Promise(r=>setTimeout(r,30));server.state.waiters.get('lost')();
  assert.equal((await pending).stopReason,'cancelled');
  assert.equal(server.state.leases.has(s.sessionId),false);
  await assert.rejects(agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'hello'}]}));
});


test('ACP terminal cleanup is bounded when the client stops replying', {timeout:10000}, async t=>{
  const server=await startFakeServer();t.after(()=>server.close());
  const terminalCalls=[];
  const handlers={
    'terminal/create':()=>{terminalCalls.push('create');return {terminalId:'terminal-1'};},
    'terminal/wait_for_exit':()=>{terminalCalls.push('wait');return new Promise(()=>{});},
    'terminal/kill':()=>{terminalCalls.push('kill');return new Promise(()=>{});},
    'terminal/release':()=>{terminalCalls.push('release');return new Promise(()=>{});},
  };
  const {agent}=launch(t,['--mode','cloud','--server',server.url],{HUGAGENT_API_KEY:'sk-jx-test-token'},undefined,handlers);
  await agent.request('initialize',{protocolVersion:1,clientCapabilities:{terminal:true}});
  const s=await agent.request('session/new',{cwd:process.cwd(),mcpServers:[]});
  const bridge=server.state.leases.get(s.sessionId).servers.acp_client;
  const mcp=new McpClient({name:'terminal-test',version:'1'});
  await mcp.connect(new StreamableHTTPClientTransport(new URL(bridge.url),{requestInit:{headers:bridge.headers}}));t.after(()=>mcp.close());
  const pending=agent.request('session/prompt',{sessionId:s.sessionId,prompt:[{type:'text',text:'hang'}]});
  while(!server.state.waiters.has('cancel')) await new Promise(r=>setTimeout(r,5));
  const tool=mcp.callTool({name:'client_run_command',arguments:{command:'test-command',args:[]}}).then(value=>({value}),error=>({error}));
  while(!terminalCalls.includes('wait')) await new Promise(r=>setTimeout(r,5));
  await agent.notify('session/cancel',{sessionId:s.sessionId});await pending;const start=Date.now();await agent.request('session/close',{sessionId:s.sessionId});assert.ok(Date.now()-start<6000);const outcome=await tool;assert.ok(outcome.error || outcome.value?.isError);
  assert.deepEqual(terminalCalls.slice(0,4),['create','wait','kill','release']);
});
