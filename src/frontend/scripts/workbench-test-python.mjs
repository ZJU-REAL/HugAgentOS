import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';

/** Test builders use the current checkout's tools, including a linked worktree. */
export function workbenchTestPython(root) {
  if (process.env.WORKBENCH_PYTHON) return process.env.WORKBENCH_PYTHON;
  const common = spawnSync('git', ['rev-parse', '--git-common-dir'],
    {cwd: root, encoding: 'utf8', shell: false});
  const roots = [root];
  if (common.status === 0) roots.push(dirname(resolve(root, common.stdout.trim())));
  for (const candidate of roots) {
    for (const path of ['.venv/bin/python', '.venv/Scripts/python.exe']) {
      const executable = join(candidate, path);
      if (existsSync(executable)) return executable;
    }
  }
  return 'python3';
}
