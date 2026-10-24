/** The EngineFactory: builds and connects the engine for a mode. */
import { resolveCloudCredentials } from '../config.js';
import type { Engine, EngineFactory, Mode } from './engine.js';
import { LocalEngine } from './local.js';
import { DeviceEngine, RemoteEngine } from './remote.js';

export interface FactoryDefaults {
  /** --server: the cloud server for cloud and device modes. */
  server?: string;
  env?: NodeJS.ProcessEnv;
  /** Progress text while a local engine starts (only shown by callers that want it). */
  onStatus?: (message: string) => void;
}

export function buildEngine(mode: Mode, options: { cwd: string }, defaults: FactoryDefaults = {}): Engine {
  const env = defaults.env ?? process.env;
  if (mode === 'local') return new LocalEngine({ cwd: options.cwd, env, onStatus: defaults.onStatus });
  const credentials = resolveCloudCredentials(defaults.server, env);
  return mode === 'cloud' ? new RemoteEngine(credentials) : new DeviceEngine(mode, credentials);
}

/** Engines come back connected (connect() is idempotent, so calling it again is harmless). */
export function engineFactory(defaults: FactoryDefaults = {}): EngineFactory {
  return async (mode, options) => {
    const engine = buildEngine(mode, options, defaults);
    await engine.connect();
    return engine;
  };
}

export { LocalEngine } from './local.js';
export { DeviceEngine, RemoteEngine } from './remote.js';
export * from './errors.js';
