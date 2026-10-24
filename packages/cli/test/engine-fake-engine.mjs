// Stands in for `hugagent-server` in the spawn tests: `engine --ready-file PATH` serves the fake
// API and writes the §8.1 ready file; FAKE_ENGINE_EXIT=3 mimics an uninitialized install.
import { writeFileSync, renameSync } from 'node:fs';
import { startFakeServer, TOKEN } from './engine-fake-server.mjs';

const args = process.argv.slice(2);
if (process.env.FAKE_ENGINE_ARGS_FILE) writeFileSync(process.env.FAKE_ENGINE_ARGS_FILE, JSON.stringify(args));
if (process.env.FAKE_ENGINE_EXIT) {
  process.stderr.write('{"error":"not_initialized"}\n');
  process.exit(Number(process.env.FAKE_ENGINE_EXIT));
}
if (args[0] !== 'engine') {
  process.stdout.write('forwarded:' + args.join(' ') + '\n');
  process.exit(args[0] === 'doctor' ? 4 : 0);
}
const readyFile = args[args.indexOf('--ready-file') + 1];
const server = await startFakeServer();
const temp = readyFile + '.tmp';
writeFileSync(temp, JSON.stringify({
  version: 1, url: server.url, token: TOKEN, pid: process.pid, started_at: new Date().toISOString(), home: process.env.HUGAGENT_HOME,
}), { mode: 0o600 });
renameSync(temp, readyFile);
process.stdout.write('engine ready on ' + server.url + '\n');
setTimeout(() => process.exit(0), 60_000).unref();
process.on('SIGTERM', () => process.exit(0));
