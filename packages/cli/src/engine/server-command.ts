/**
 * Which command runs the Python server (`hugagent-server`), shared by the local engine spawn
 * and the `hugagent serve|onboard|doctor` forwarding (DESIGN.md §8.1):
 *   1. HUGAGENT_ENGINE_CMD, a JSON array such as ["python3", "/repo/src/backend/cli.py"];
 *   2. `hugagent-server` on PATH;
 *   3. [${HUGAGENT_PYTHON:-python3}, "$HUGAGENT_REPO/src/backend/cli.py"] when HUGAGENT_REPO is set.
 */
import { accessSync, constants, existsSync, statSync } from 'node:fs';
import { delimiter, join } from 'node:path';
import { CliError, EXIT } from './errors.js';

export function resolveServerCommand(env: NodeJS.ProcessEnv = process.env): string[] {
  const configured = env.HUGAGENT_ENGINE_CMD?.trim();
  if (configured) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(configured);
    } catch {
      parsed = null;
    }
    if (!Array.isArray(parsed) || !parsed.length || !parsed.every((part) => typeof part === 'string' && part)) {
      throw new CliError('HUGAGENT_ENGINE_CMD must be a JSON array of strings, e.g. ["hugagent-server"]', EXIT.usage);
    }
    return parsed as string[];
  }
  const onPath = findExecutable('hugagent-server', env);
  if (onPath) return [onPath];
  const repo = env.HUGAGENT_REPO?.trim();
  if (repo) {
    const script = join(repo, 'src', 'backend', 'cli.py');
    if (!existsSync(script)) throw new CliError(`HUGAGENT_REPO is set but ${script} does not exist`, EXIT.usage);
    return [env.HUGAGENT_PYTHON?.trim() || 'python3', script];
  }
  throw new CliError(
    'Cannot find the HugAgentOS server: install it (pip install hugagent, which provides hugagent-server), ' +
    'or set HUGAGENT_REPO to a HugAgentOS checkout, or HUGAGENT_ENGINE_CMD to a JSON command array',
  );
}

export function findExecutable(name: string, env: NodeJS.ProcessEnv = process.env): string | null {
  const extensions = process.platform === 'win32' ? (env.PATHEXT || '.EXE;.CMD;.BAT').split(';') : [''];
  for (const dir of (env.PATH || '').split(delimiter)) {
    if (!dir) continue;
    for (const extension of extensions) {
      const candidate = join(dir, name + extension);
      try {
        if (!statSync(candidate).isFile()) continue;
        accessSync(candidate, constants.X_OK);
        return candidate;
      } catch {
        // not here
      }
    }
  }
  return null;
}
