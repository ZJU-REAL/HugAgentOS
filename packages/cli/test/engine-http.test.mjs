import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { HugAgentOSHttp } from '../dist/engine/http.js';
import { HugAgentOSHttpError, UnsupportedError } from '../dist/engine/errors.js';
import { DeviceEngine, RemoteEngine } from '../dist/engine/remote.js';
import { validateServerUrl } from '../dist/config.js';
import { startFakeServer, TOKEN } from './engine-fake-server.mjs';

let server;
before(async () => { server = await startFakeServer(); });
after(() => server.close());

const http = () => new HugAgentOSHttp({ baseUrl: server.url, token: TOKEN });

async function rejectsWith(promise, check) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof HugAgentOSHttpError, `expected HugAgentOSHttpError, got ${error}`);
    check(error);
    return true;
  });
}

test('unwraps the {code,data} envelope', async () => {
  assert.equal(await http().getToolApproval(), 'ask');
  const project = await http().resolveLocalProject('/tmp/repo');
  assert.equal(project.project_id, 'proj1');
  assert.equal(server.state.requests.at(-1).body.create, true);
});

test('errors carry status and the server detail', async () => {
  await rejectsWith(http().messages('busy'), (error) => {
    assert.equal(error.status, 409);
    assert.equal(error.message, 'chat is busy (HTTP 409)');
    assert.deepEqual(error.detail, { code: 'busy', message: 'chat is busy' });
  });
  await rejectsWith(http().json('GET', '/v1/chats/envelope/messages-bad'), (error) => {
    assert.equal(error.code, 20001);
    assert.match(error.message, /business refused/);
  });
  await rejectsWith(http().fork('validation'), (error) => {
    assert.equal(error.status, 422);
    assert.equal(error.message, 'request_id: field required (HTTP 422)');
  });
  await rejectsWith(http().compact('proxy'), (error) => {
    assert.equal(error.status, 502);
    assert.match(error.message, /Bad Gateway/);
  });
  await rejectsWith(new HugAgentOSHttp({ baseUrl: server.url, token: 'wrong' }).getToolApproval(), (error) => {
    assert.equal(error.status, 401);
    assert.equal(error.message, 'Not authenticated (HTTP 401)');
  });
  await rejectsWith(new HugAgentOSHttp({ baseUrl: 'http://127.0.0.1:9', token: 'x' }).getToolApproval(), (error) => {
    assert.equal(error.status, 0);
    assert.match(error.message, /Cannot reach/);
  });
});

test('steer sends a fresh steer_id each time', async () => {
  await http().steer('run1', 'also check tests', 'followUp');
  await http().steer('run1', 'again');
  const [first, second] = server.state.steers.slice(-2);
  assert.equal(first.delivery_mode, 'followUp');
  assert.equal(second.delivery_mode, 'steer');
  assert.ok(first.steer_id && first.steer_id.length <= 64);
  assert.notEqual(first.steer_id, second.steer_id);
});

test('cloud engine: sessions, models, reconnect after a dropped stream', async () => {
  const engine = new RemoteEngine({ server: server.url, apiKey: TOKEN });
  await engine.connect();
  assert.equal(engine.info.canAccessCwd, false);
  assert.match(engine.info.label, /^cloud · 127\.0\.0\.1:/);
  assert.deepEqual((await engine.listSessions()).map((s) => s.chatId), ['cloud1']);
  assert.deepEqual(await engine.listModels(), [
    { providerId: 'p1', modelName: 'one', label: 'Model One', current: true },
    { providerId: 'p2', modelName: 'two', label: 'two', current: false },
  ]);
  const history = await engine.history('c1');
  assert.deepEqual(history.map((m) => m.message_id), ['m1', 'm2'], 'oldest first');
  assert.equal(server.state.requests.at(-1).query.order, 'desc');

  const text = [];
  for await (const event of engine.send('c1', 'drop')) if (event.type === 'content') text.push(event.delta);
  assert.equal(text.join(''), 'partial resumed');
  const resume = server.state.resumes.at(-1);
  assert.equal(resume.runId, 'run-c1');
  assert.ok(resume.from > 0, 'resumes after the last delivered offset');

  await assert.rejects(engine.compact('missing'), /no manual compaction endpoint/);
  assert.deepEqual(await engine.compact('c1'), { compacted: true, tokensBefore: 100, tokensAfter: 10 });
});

test('cloud engine maps a rejected key to setup-required (exit 3)', async () => {
  const engine = new RemoteEngine({ server: server.url, apiKey: 'nope' });
  await assert.rejects(engine.connect(), (error) => error.exitCode === 3 && /hugagent login/.test(error.message));
});

test('device engine synthesizes a turn from the operation queue', async () => {
  const engine = new DeviceEngine('device:dev1', { server: server.url, apiKey: TOKEN });
  await engine.connect();
  assert.equal((await engine.newSession('t')).chatId, 'dchat');
  const events = [];
  for await (const event of engine.send('dchat', 'hi')) events.push(event);
  assert.deepEqual(events.map((e) => e.type), ['run_started', 'content', 'end']);
  assert.equal(events[1].content, 'device answer');
  assert.equal(events[2].status, 'completed');
  const sent = [...server.state.operations.values()].find((op) => op.operation === 'chat.send');
  assert.equal(sent.target, 'device:dev1');
  assert.deepEqual(sent.args, { chat_id: 'dchat', message: 'hi' });
  await assert.rejects(engine.cancel('drun'), /run.cancel failed on device:dev1/);
  await assert.rejects(engine.compact('dchat'), (error) => error instanceof UnsupportedError);
  await assert.rejects(new DeviceEngine('device:ghost', { server: server.url, apiKey: TOKEN }).connect(), /No paired device ghost/);
});

test('cloud URLs need https except loopback or HUGAGENT_ALLOW_HTTP=1', () => {
  assert.equal(validateServerUrl('https://agent.example.com/api/', {}), 'https://agent.example.com/api');
  assert.equal(validateServerUrl('http://localhost:8000', {}), 'http://localhost:8000');
  assert.throws(() => validateServerUrl('http://agent.example.com', {}), /Refusing plain HTTP/);
  assert.equal(validateServerUrl('http://10.0.0.2', { HUGAGENT_ALLOW_HTTP: '1' }), 'http://10.0.0.2');
  assert.throws(() => validateServerUrl('https://user:pw@agent.example.com', {}), /Invalid server URL/);
});
