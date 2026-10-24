/**
 * LocalEngine: attach to (or start) the shared headless engine on this machine and bind the
 * current directory to a backend local project (DESIGN.md §8.1, §8.2).
 */
import { spawn } from 'node:child_process';
import { closeSync, mkdirSync, openSync, readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { hugagentHome } from '../config.js';
import { HttpEngine, latestSummaries, pageSizeFor, toSummary } from './base.js';
import type { EngineInfo, SessionSummary } from './engine.js';
import { CliError, EXIT, HugAgentOSHttpError, SetupRequiredError } from './errors.js';
import { HugAgentOSHttp } from './http.js';
import { resolveServerCommand } from './server-command.js';

export interface ReadyFile {
  version: number;
  url: string;
  token: string;
  pid: number;
  started_at?: string;
  home?: string;
}

export interface LocalEngineOptions {
  cwd: string;
  env?: NodeJS.ProcessEnv;
  /** How long a fresh engine may take to write its ready file (default 120 s). */
  startTimeoutMs?: number;
  /** Progress lines while starting the engine ("Starting local engine…"). */
  onStatus?: (message: string) => void;
}

export function readyFilePath(env: NodeJS.ProcessEnv = process.env): string {
  return join(hugagentHome(env), 'engine.json');
}

export function engineLogPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(hugagentHome(env), 'logs', 'engine.log');
}

export function readReadyFile(path: string): ReadyFile | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const ready = parsed as Partial<ReadyFile>;
  if (typeof ready.url !== 'string' || typeof ready.token !== 'string' || !ready.token ||
    typeof ready.pid !== 'number' || !Number.isInteger(ready.pid) || ready.pid <= 0) return null;
  try {
    const url = new URL(ready.url);
    // The engine only ever binds loopback; anything else is not ours to send the token to.
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return null;
  } catch {
    return null;
  }
  return ready as ReadyFile;
}

export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

async function usable(ready: ReadyFile): Promise<boolean> {
  if (!pidAlive(ready.pid)) return false;
  const http = new HugAgentOSHttp({ baseUrl: ready.url, token: ready.token });
  for (let attempt = 0; attempt < 3; attempt++) {
    if (await http.health()) return true;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
}

function logTail(path: string, lines = 15): string {
  try {
    return readFileSync(path, 'utf8').trimEnd().split('\n').slice(-lines).join('\n');
  } catch {
    return '';
  }
}

/**
 * Reuse the running engine, or start one detached and wait for its ready file. Concurrent starts
 * are safe without a lock here: `hugagent-server engine` holds its own per-home lock, and a second
 * one waits for the first and exits 0 once the ready file describes it.
 */
export async function discoverEngine(options: Omit<LocalEngineOptions, 'cwd'> = {}): Promise<ReadyFile> {
  const env = options.env ?? process.env;
  const readyPath = readyFilePath(env);
  const existing = readReadyFile(readyPath);
  if (existing && await usable(existing)) return existing;

  const home = hugagentHome(env);
  mkdirSync(join(home, 'logs'), { recursive: true, mode: 0o700 });
  const timeoutMs = options.startTimeoutMs ?? 120_000;
  const logPath = engineLogPath(env);
  const command = resolveServerCommand(env);
  options.onStatus?.('Starting local engine…');
  let exit: { code: number | null; signal: NodeJS.Signals | null; error?: Error } | undefined;
  const log = openSync(logPath, 'a', 0o600);
  try {
    const child = spawn(command[0], [...command.slice(1), 'engine', '--ready-file', readyPath], {
      cwd: home,
      env,
      detached: true,
      stdio: ['ignore', log, log],
      windowsHide: true,
    });
    child.on('exit', (code, signal) => { exit ??= { code, signal }; });
    child.on('error', (error) => { exit = { code: null, signal: null, error }; });
    child.unref();
  } finally {
    closeSync(log);
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ready = readReadyFile(readyPath);
    if (ready && await usable(ready)) return ready;
    const stopped = exit as typeof exit;
    if (stopped) {
      if (stopped.code === EXIT.setup) {
        throw new SetupRequiredError('HugAgentOS is not initialized on this machine: run `hugagent onboard --no-serve` first');
      }
      const reason = stopped.error ? stopped.error.message : stopped.signal ? `signal ${stopped.signal}` : `exit code ${stopped.code}`;
      const tail = logTail(logPath);
      throw new CliError(`Local engine stopped before it was ready (${reason}); see ${logPath}${tail ? '\n' + tail : ''}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new CliError(`Local engine did not become ready within ${Math.round(timeoutMs / 1000)} s; see ${logPath}`);
}

export function tildify(path: string): string {
  const home = homedir();
  return path === home ? '~' : path.startsWith(home + '/') ? '~' + path.slice(home.length) : path;
}

export class LocalEngine extends HttpEngine {
  readonly info: EngineInfo;

  constructor(private readonly options: LocalEngineOptions) {
    super();
    this.info = { mode: 'local', label: 'local · ' + tildify(options.cwd), server: '', workspace: options.cwd, canAccessCwd: true };
  }

  protected async open(): Promise<void> {
    const ready = await discoverEngine(this.options);
    const http = new HugAgentOSHttp({ baseUrl: ready.url, token: ready.token });
    let path: string;
    try {
      path = realpathSync(this.options.cwd);
    } catch {
      throw new CliError(`Working directory does not exist: ${this.options.cwd}`);
    }
    let project;
    try {
      // Running hugagent in a folder is consent to work in it (Codex's workspace-write):
      // the server binds it and grants it to the OS sandbox, never $HOME or / wholesale.
      project = await http.resolveLocalProject(path);
    } catch (error) {
      if (error instanceof HugAgentOSHttpError && error.status === 404) {
        throw new CliError(`The local engine at ${ready.url} cannot bind folders (POST /v1/projects/local/resolve: ${error.message}); update hugagent-server`);
      }
      if (error instanceof HugAgentOSHttpError && error.status === 401) {
        throw new CliError(`The local engine rejected its own token; delete ${readyFilePath(this.options.env)} and retry`);
      }
      throw error;
    }
    this.client = http;
    this.info.server = ready.url;
    this.info.projectId = project.project_id;
  }

  async newSession(title?: string): Promise<SessionSummary> {
    await this.connect();
    return toSummary(await this.http.createChat({ title, projectId: this.info.projectId }));
  }

  async listSessions(limit = 20): Promise<SessionSummary[]> {
    await this.connect();
    const page = await this.http.listProjectChats(this.info.projectId!, 1, pageSizeFor(limit));
    return latestSummaries(page.items, limit);
  }
}
