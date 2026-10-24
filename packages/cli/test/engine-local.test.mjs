import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { LocalEngine, discoverEngine, readReadyFile } from '../dist/engine/local.js';
import { resolveServerCommand } from '../dist/engine/server-command.js';
import { startFakeServer, TOKEN } from './engine-fake-server.mjs';

const FAKE_ENGINE = fileURLToPath(new URL('./engine-fake-engine.mjs', import.meta.url));
const fakeCmd = JSON.stringify([process.execPath, FAKE_ENGINE]);
const spawned = [];

let server;
before(async () => { server = await startFakeServer(); });
after(async () => {
  for (const pid of spawned) try { process.kill(pid, 'SIGTERM'); } catch { /* gone */ }
  await server.close();
});

const home = () => mkdtempSync(join(tmpdir(), 'hugagent-cli-test-'));

test('attaches to the engine named in a live ready file and binds cwd', async () => {
  const dir = home();
  writeFileSync(join(dir, 'engine.json'), JSON.stringify({ version: 1, url: server.url, token: TOKEN, pid: process.pid }));
  const cwd = mkdtempSync(join(tmpdir(), 'hugagent-cwd-'));
  const engine = new LocalEngine({ cwd, env: { ...process.env, HUGAGENT_HOME: dir, HUGAGENT_ENGINE_CMD: '["false"]' } });
  await engine.connect();
  await engine.connect(); // idempotent
  assert.equal(engine.info.projectId, 'proj1');
  assert.equal(engine.info.server, server.url);
  assert.equal(engine.info.workspace, cwd);
  assert.equal(engine.info.canAccessCwd, true);
  const resolves = server.state.requests.filter((r) => r.path === '/v1/projects/local/resolve');
  assert.equal(resolves.length, 1);
  assert.equal(resolves[0].body.path, realpathSync(cwd));
  assert.equal(resolves[0].body.grant, true, 'the server grants the folder to the OS sandbox in the same call');

  const created = await engine.newSession('x');
  assert.equal(created.projectId, 'proj1');
  assert.equal(server.state.requests.at(-1).body.project_id, 'proj1');
  assert.deepEqual((await engine.listSessions()).map((s) => s.chatId), ['new1', 'old1'], 'project chats, newest first');
  await engine.close();
});

test('ignores a ready file whose pid is dead or whose url is not loopback', () => {
  const dir = home();
  const path = join(dir, 'engine.json');
  writeFileSync(path, JSON.stringify({ version: 1, url: 'http://evil.example.com', token: 't', pid: 1 }));
  assert.equal(readReadyFile(path), null);
});

test('spawns HUGAGENT_ENGINE_CMD detached and waits for its ready file', async () => {
  const dir = home();
  const argsFile = join(dir, 'args.json');
  writeFileSync(join(dir, 'engine.json'), JSON.stringify({ version: 1, url: server.url, token: TOKEN, pid: 999999 }));
  const env = { ...process.env, HUGAGENT_HOME: dir, HUGAGENT_ENGINE_CMD: fakeCmd, FAKE_ENGINE_ARGS_FILE: argsFile };
  const statuses = [];
  const ready = await discoverEngine({ env, onStatus: (s) => statuses.push(s) });
  spawned.push(ready.pid);
  assert.notEqual(ready.pid, 999999);
  assert.notEqual(ready.url, server.url);
  assert.deepEqual(JSON.parse(readFileSync(argsFile, 'utf8')), ['engine', '--ready-file', join(dir, 'engine.json')]);
  assert.match(readFileSync(join(dir, 'logs', 'engine.log'), 'utf8'), /engine ready on/);
  assert.deepEqual(statuses, ['Starting local engine…']);
  // The next client reuses it instead of starting another.
  const again = await discoverEngine({ env: { ...env, HUGAGENT_ENGINE_CMD: '["false"]' } });
  assert.equal(again.pid, ready.pid);
});

test('an engine that exits 3 asks for onboarding (exit code 3)', async () => {
  const dir = home();
  const env = { ...process.env, HUGAGENT_HOME: dir, HUGAGENT_ENGINE_CMD: fakeCmd, FAKE_ENGINE_EXIT: '3' };
  await assert.rejects(discoverEngine({ env }), (error) => error.exitCode === 3 && /onboard --no-serve/.test(error.message));
});

test('an engine that dies early reports its log', async () => {
  const dir = home();
  const env = { ...process.env, HUGAGENT_HOME: dir, HUGAGENT_ENGINE_CMD: fakeCmd, FAKE_ENGINE_EXIT: '1' };
  await assert.rejects(discoverEngine({ env }), (error) => error.exitCode === 1 && /exit code 1/.test(error.message) && /not_initialized/.test(error.message));
});

test('server command resolution order', () => {
  assert.deepEqual(resolveServerCommand({ HUGAGENT_ENGINE_CMD: '["a","b"]', PATH: '' }), ['a', 'b']);
  assert.throws(() => resolveServerCommand({ HUGAGENT_ENGINE_CMD: 'hugagent-server' }), /JSON array/);
  const repo = mkdtempSync(join(tmpdir(), 'hugagent-repo-'));
  assert.throws(() => resolveServerCommand({ HUGAGENT_REPO: repo, PATH: '' }), /does not exist/);
  const bin = mkdtempSync(join(tmpdir(), 'hugagent-bin-'));
  writeFileSync(join(bin, 'hugagent-server'), '#!/bin/sh\n', { mode: 0o755 });
  assert.deepEqual(resolveServerCommand({ PATH: bin, HUGAGENT_REPO: repo }), [join(bin, 'hugagent-server')]);
  assert.throws(() => resolveServerCommand({ PATH: '' }), /Cannot find the HugAgentOS server/);
});
