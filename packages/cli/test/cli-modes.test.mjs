import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { LocalEngine } from '../dist/engine/local.js';
import { runRpc } from '../dist/modes/rpc.js';
import { startFakeServer, TOKEN } from './engine-fake-server.mjs';

const MAIN = fileURLToPath(new URL('../dist/main.js', import.meta.url));
const FAKE_ENGINE = fileURLToPath(new URL('./engine-fake-engine.mjs', import.meta.url));

let server;
let home;
before(async () => {
  server = await startFakeServer();
  home = mkdtempSync(join(tmpdir(), 'hugagent-cli-home-'));
  writeFileSync(join(home, 'engine.json'), JSON.stringify({ version: 1, url: server.url, token: TOKEN, pid: process.pid }));
});
after(() => server.close());

function cli(args, { input, env = {}, onStdout } = {}) {
  const child = spawn(process.execPath, [MAIN, ...args], {
    cwd: home,
    env: { ...process.env, HUGAGENT_HOME: home, HUGAGENT_ENGINE_CMD: '["false"]', HUGAGENT_SERVER: '', HUGAGENT_API_KEY: '', ...env },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; onStdout?.(stdout, child); });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  if (input !== undefined) child.stdin.end(input);
  const done = new Promise((resolve) => child.on('close', (code) => resolve({ code, stdout, stderr })));
  return Object.assign(done, { child });
}

const lines = (text) => text.trim().split('\n').map((line) => JSON.parse(line));

test('--help and --version', async () => {
  const help = await cli(['--help']);
  assert.equal(help.code, 0);
  assert.match(help.stdout, /--output text\|json\|rpc/);
  const version = await cli(['--version']);
  assert.equal(version.stdout.trim(), JSON.parse(readFileSync(new URL('../package.json', import.meta.url))).version);
});

test('text mode: answer on stdout, tool lines on stderr', async () => {
  const result = await cli(['-p', 'hello there']);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(result.stdout, 'Hello, world\n');
  assert.match(result.stderr, /→ list_dir \./);
  const send = server.state.requests.findLast((r) => r.path === '/v1/agents/responses');
  assert.equal(send.body.message, 'hello there');
  assert.equal(send.body.stream, true);
});

test('prompt from stdin, --model and -r', async () => {
  const result = await cli(['-p', '--model', 'prov:deep:seek', '-r', 'chat42abc'], { input: 'from stdin\n' });
  assert.equal(result.code, 0, result.stderr);
  const send = server.state.requests.findLast((r) => r.path === '/v1/agents/responses');
  assert.deepEqual([send.body.chat_id, send.body.message, send.body.model_provider_id, send.body.model_name],
    ['chat42abc', 'from stdin\n', 'prov', 'deep:seek']);
});

test('json mode: every event as a line, then cli_result', async () => {
  const result = await cli(['-p', 'hi', '--output', 'json', '-c']);
  assert.equal(result.code, 0, result.stderr);
  const events = lines(result.stdout);
  assert.deepEqual(events.map((e) => e.type), ['run_started', 'thinking', 'content', 'tool_call', 'tool_result', 'content', 'end', 'cli_result']);
  assert.equal(typeof events[0].event_offset, 'number', 'events are passed through unchanged');
  assert.deepEqual(events.at(-1), { type: 'cli_result', status: 'completed', chat_id: 'new1', run_id: 'run-new1' });
});

test('non-interactive approvals: deny by default, allow with --auto-approve, skip questions', async () => {
  const denied = await cli(['-p', 'approve', '--output', 'json']);
  assert.equal(denied.code, 0, denied.stderr);
  assert.equal(server.state.confirms.at(-1).decision, 'deny');
  assert.match(denied.stderr, /denied a\.txt \(pass --auto-approve to allow\)/);
  const allowed = await cli(['-p', 'approve', '--auto-approve']);
  assert.equal(server.state.confirms.at(-1).decision, 'allow');
  assert.match(allowed.stdout, /decision=allow/);
  const asked = await cli(['-p', 'question']);
  assert.equal(asked.code, 0);
  assert.deepEqual(server.state.answers.at(-1).answers, [
    { id: 'a', selected: [], skipped: true }, { id: 'b', selected: [], skipped: true },
  ]);
});

test('exit codes: failed run 1, usage 2, setup 3', async () => {
  const failed = await cli(['-p', 'fail', '--output', 'json']);
  assert.equal(failed.code, 1);
  assert.deepEqual(lines(failed.stdout).at(-1), { type: 'cli_result', status: 'failed', chat_id: lines(failed.stdout).at(-1).chat_id, run_id: lines(failed.stdout).at(-1).run_id, error: 'model exploded' });
  const failedText = await cli(['-p', 'fail']);
  assert.equal(failedText.code, 1);
  assert.equal(failedText.stderr.match(/model exploded/g).length, 1, 'error printed once');

  for (const args of [['--mode', 'mars', '-p', 'x'], ['--output', 'rpc', '-p', 'x'], ['--model', 'nocolon', '-p', 'x'], ['-c', '-r', 'abc123', '-p', 'x'], ['--frobnicate']]) {
    const usage = await cli(args);
    assert.equal(usage.code, 2, args.join(' ') + ': ' + usage.stderr);
  }

  const noLogin = await cli(['--mode', 'cloud', '-p', 'x']);
  assert.equal(noLogin.code, 3);
  assert.match(noLogin.stderr, /hugagent login/);

  const freshHome = mkdtempSync(join(tmpdir(), 'hugagent-cli-fresh-'));
  const uninitialized = await cli(['-p', 'x'], {
    env: { HUGAGENT_HOME: freshHome, HUGAGENT_ENGINE_CMD: JSON.stringify([process.execPath, FAKE_ENGINE]), FAKE_ENGINE_EXIT: '3' },
  });
  assert.equal(uninitialized.code, 3);
  assert.match(uninitialized.stderr, /onboard/);
});

test('Ctrl+C cancels the run and exits 130', async () => {
  let signalled = false;
  const run = cli(['-p', 'hang', '--output', 'json'], {
    onStdout: (stdout, child) => {
      if (!signalled && stdout.includes('run_started')) {
        signalled = true;
        child.kill('SIGINT');
      }
    },
  });
  const result = await run;
  assert.equal(result.code, 130, result.stderr);
  assert.equal(server.state.cancels.at(-1), 'run-' + lines(result.stdout).at(-1).chat_id);
  assert.equal(lines(result.stdout).at(-1).status, 'cancelled');
});

test('login saves {server, apiKey} with mode 0600 and logout forgets the key', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'hugagent-cli-login-'));
  const bad = await cli(['login', '--server', server.url], { input: 'wrong-key\n', env: { HUGAGENT_HOME: dir } });
  assert.equal(bad.code, 1);
  assert.match(bad.stderr, /rejected this API key/);
  const good = await cli(['login', '--server', server.url], { input: TOKEN + '\n', env: { HUGAGENT_HOME: dir } });
  assert.equal(good.code, 0, good.stderr);
  const path = join(dir, 'cli.json');
  assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), { server: server.url, apiKey: TOKEN });
  assert.equal(statSync(path).mode & 0o777, 0o600);
  const cloud = await cli(['--mode', 'cloud', '-p', 'hi'], { env: { HUGAGENT_HOME: dir } });
  assert.equal(cloud.code, 0, cloud.stderr);
  assert.equal(cloud.stdout, 'Hello, world\n');
  // A saved key never goes to a different --server.
  const other = await cli(['--mode', 'cloud', '--server', 'http://127.0.0.1:9', '-p', 'hi'], { env: { HUGAGENT_HOME: dir } });
  assert.equal(other.code, 3);
  assert.equal((await cli(['logout'], { env: { HUGAGENT_HOME: dir } })).code, 0);
  assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), { server: server.url });
});

test('serve|onboard|doctor are forwarded to hugagent-server with its exit code', async () => {
  const env = { HUGAGENT_ENGINE_CMD: JSON.stringify([process.execPath, FAKE_ENGINE]) };
  const doctor = await cli(['doctor', '--verbose'], { env });
  assert.equal(doctor.code, 4);
  assert.equal(doctor.stdout, 'forwarded:doctor --verbose\n');
  const missing = await cli(['onboard'], { env: { HUGAGENT_ENGINE_CMD: '', PATH: '/nonexistent' } });
  assert.equal(missing.code, 1);
  assert.match(missing.stderr, /Cannot find the HugAgentOS server/);
});

test('rpc: JSON-lines round trip mirroring Engine', async () => {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  let output = '';
  stdout.on('data', (chunk) => { output += chunk; });
  const engine = new LocalEngine({ cwd: home, env: { ...process.env, HUGAGENT_HOME: home } });
  const done = runRpc({ engine, stdin, stdout });
  for (const command of [
    { id: 1, method: 'connect' },
    { id: 2, method: 'newSession', params: { title: 'rpc' } },
    { id: 3, method: 'send', params: { chatId: 'rpcchat', message: 'hi' } },
    { id: 4, method: 'getPermissionMode' },
    { id: 5, method: 'nope' },
    { id: 6, method: 'history' },
  ]) stdin.write(JSON.stringify(command) + '\n');
  stdin.write('not json\n');
  stdin.end();
  assert.equal(await done, 0);
  const messages = lines(output);
  const reply = (id) => messages.find((m) => m.id === id && !m.event);
  assert.equal(reply(1).result.projectId, 'proj1');
  assert.equal(reply(2).result.projectId, 'proj1');
  assert.deepEqual(reply(3).result, { status: 'completed', chatId: 'rpcchat', runId: 'run-rpcchat' });
  assert.equal(reply(4).result, 'ask');
  assert.match(reply(5).error.message, /Unknown method nope/);
  assert.equal(reply(6).error.code, 'invalid_params');
  assert.deepEqual(messages.find((m) => m.id === null).error.code, 'parse_error');
  const events = messages.filter((m) => m.event);
  assert.ok(events.every((m) => m.request === 3));
  assert.deepEqual(events.map((m) => m.event.type), ['run_started', 'thinking', 'content', 'tool_call', 'tool_result', 'content', 'end']);
  assert.ok(messages.indexOf(reply(3)) > messages.indexOf(events.at(-1)), 'reply follows its events');
});
