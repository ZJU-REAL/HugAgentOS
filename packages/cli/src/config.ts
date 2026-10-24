/**
 * CLI settings and cloud credentials in $HUGAGENT_HOME/cli.json (default ~/.hugagent/cli.json, mode 0600).
 * `hugagent login` writes `{server, apiKey}`; HUGAGENT_SERVER / HUGAGENT_API_KEY override them.
 */
import { chmodSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import type { Mode } from './engine/engine.js';
import { CliError, EXIT } from './engine/errors.js';

export interface CliConfig {
  server?: string;
  apiKey?: string;
  /** Mode used when --mode is not given. */
  defaultMode?: string;
  [key: string]: unknown;
}

export function hugagentHome(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.HUGAGENT_HOME?.trim();
  return configured ? resolve(configured) : join(homedir(), '.hugagent');
}

export function configPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(hugagentHome(env), 'cli.json');
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): CliConfig {
  const path = configPath(env);
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {};
    throw error;
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
    return parsed as CliConfig;
  } catch {
    throw new CliError(`${path} is not valid JSON; fix or delete it`, EXIT.usage);
  }
}

/** Atomic write, owner-only: the file holds an API key. */
export function saveConfig(config: CliConfig, env: NodeJS.ProcessEnv = process.env): void {
  const home = hugagentHome(env);
  mkdirSync(home, { recursive: true, mode: 0o700 });
  const path = configPath(env);
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, JSON.stringify(config, null, 2) + '\n', { mode: 0o600 });
  chmodSync(temp, 0o600);
  renameSync(temp, path);
}

const DEVICE_ID = /^[A-Za-z0-9_-]{1,100}$/;

export function parseMode(value: string): Mode | null {
  if (value === 'local' || value === 'cloud') return value;
  if (value.startsWith('device:') && DEVICE_ID.test(value.slice(7))) return value as Mode;
  return null;
}

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

export function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK.has(hostname) || hostname.endsWith('.localhost');
}

/** Normalized server URL; https is required except for loopback or HUGAGENT_ALLOW_HTTP=1. */
export function validateServerUrl(value: string, env: NodeJS.ProcessEnv = process.env): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new CliError(`Invalid server URL: ${value}`, EXIT.usage);
  }
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !url.hostname || url.username || url.password || url.search || url.hash) {
    throw new CliError(`Invalid server URL: ${value} (expected https://host[/path])`, EXIT.usage);
  }
  if (url.protocol === 'http:' && !isLoopbackHost(url.hostname) && env.HUGAGENT_ALLOW_HTTP !== '1') {
    throw new CliError(`Refusing plain HTTP to ${url.host}: use https, or set HUGAGENT_ALLOW_HTTP=1 for a trusted network`, EXIT.usage);
  }
  return url.toString().replace(/\/+$/, '');
}

export interface CloudCredentials { server: string; apiKey: string }

/**
 * Server: --server > HUGAGENT_SERVER > cli.json. API key: HUGAGENT_API_KEY > cli.json, and the saved
 * key is only used for the server it was saved for, so --server never leaks it to another host.
 */
export function resolveCloudCredentials(serverFlag: string | undefined, env: NodeJS.ProcessEnv = process.env): CloudCredentials {
  const config = loadConfig(env);
  const raw = serverFlag || env.HUGAGENT_SERVER?.trim() || config.server;
  if (!raw) throw new CliError('No cloud server configured: run `hugagent login --server https://…` or pass --server', EXIT.setup);
  const server = validateServerUrl(raw, env);
  const saved = config.server && config.apiKey && safeNormalize(config.server) === server ? config.apiKey : undefined;
  const apiKey = env.HUGAGENT_API_KEY?.trim() || saved;
  if (!apiKey) throw new CliError(`Not logged in to ${server}: run \`hugagent login --server ${server}\``, EXIT.setup);
  if (/[\r\n]/.test(apiKey)) throw new CliError('API key must be a single line', EXIT.usage);
  return { server, apiKey };
}

function safeNormalize(server: string): string | undefined {
  try {
    return validateServerUrl(server, { ...process.env, HUGAGENT_ALLOW_HTTP: '1' });
  } catch {
    return undefined;
  }
}
