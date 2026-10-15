import {createHash} from 'node:crypto';
import {existsSync, readFileSync, readdirSync, statSync} from 'node:fs';
import {join} from 'node:path';

export const packages = ['workbench-protocol', 'workbench-sdk'];
export function fail(code, detail) { throw new Error(code + ': ' + detail); }
export function read(root, path) {
  if (!existsSync(join(root, path))) fail('workbench_missing_file', path);
  return readFileSync(join(root, path));
}
export function json(root, path) {
  try { return JSON.parse(read(root, path)); }
  catch (error) {
    if (error.message.startsWith('workbench_')) throw error;
    fail('workbench_invalid_json', path);
  }
}
export function sha256(bytes) { return createHash('sha256').update(bytes).digest('hex'); }
function sameMap(a = {}, b = {}) {
  const sorted = value => JSON.stringify(Object.entries(value).sort());
  return sorted(a) === sorted(b);
}
export function validateLock(root, directory) {
  const pkg = json(root, directory + '/package.json');
  const lock = json(root, directory + '/package-lock.json');
  const entry = lock.packages?.[''];
  if (lock.lockfileVersion !== 3 || !entry || pkg.name !== entry.name ||
      pkg.version !== entry.version ||
      !['dependencies', 'devDependencies', 'optionalDependencies']
        .every(key => sameMap(pkg[key], entry[key]))) {
    fail('workbench_lock_drift', directory + '/package-lock.json; regenerate with npm --package-lock-only');
  }
  return pkg;
}
export function validateSources(root) {
  const result = {};
  for (const name of packages) {
    const directory = 'packages/' + name;
    const pkg = validateLock(root, directory);
    if (!pkg.private || pkg.license !== 'UNLICENSED')
      fail('workbench_distribution_policy', directory + ' must remain private/UNLICENSED');
    read(root, directory + '/LICENSE');
    read(root, directory + '/scripts/build.mjs');
    read(root, directory + '/src/index.ts');
    result[name] = pkg;
  }
  validateLock(root, 'src/frontend');
  const host = json(root, 'packages/workbench-protocol/schemas/host.json');
  if (host.protocolVersion !== result['workbench-protocol'].version || host.uiVersion !== 3)
    fail('workbench_version_mismatch', 'host schema and protocol package must declare protocol 1.x / UI v3');
  const sdk = result['workbench-sdk'];
  if (sdk.devDependencies?.['@hugagent/workbench-protocol'] !== 'file:../workbench-protocol')
    fail('workbench_dependency_mismatch', 'SDK must build against the adjacent protocol package');
  const frontend = json(root, 'src/frontend/package.json');
  if (frontend.dependencies?.['@hugagent/workbench-protocol'] !== 'file:../../packages/workbench-protocol')
    fail('workbench_dependency_mismatch', 'frontend protocol package must be repository-local');
  for (const [directory, key] of [
    ['src/frontend', '../../packages/workbench-protocol'],
    ['packages/workbench-sdk', '../workbench-protocol'],
  ]) {
    const linked = json(root, directory + '/package-lock.json').packages?.[key];
    if (linked?.version !== result['workbench-protocol'].version)
      fail('workbench_lock_drift', directory + ' linked protocol version');
  }
  return {host, definitions: result};
}
export function inputHash(root, name) {
  const directory = 'packages/' + name;
  const paths = ['package.json', 'package-lock.json', 'LICENSE', 'tsconfig.json'];
  function walk(path) {
    for (const entry of readdirSync(join(root, directory, path)).sort()) {
      const child = path + '/' + entry;
      if (statSync(join(root, directory, child)).isDirectory()) walk(child);
      else paths.push(child);
    }
  }
  walk('scripts');
  walk('src');
  if (name === 'workbench-protocol') walk('schemas');
  return sha256(paths.sort().map(path => path + '\0' + sha256(read(root, directory + '/' + path))).join('\n'));
}
export function artifactRecords(root, name) {
  const directory = 'packages/' + name;
  const paths = ['LICENSE', 'package-lock.json'];
  for (const path of ['dist/index.js', 'dist/index.d.ts', ...(name === 'workbench-protocol'
    ? ['dist/legacy.js', 'dist/legacy.d.ts'] : [])]) read(root, directory + '/' + path);
  function walk(path) {
    for (const entry of readdirSync(join(root, directory, path)).sort()) {
      const child = path + '/' + entry;
      if (statSync(join(root, directory, child)).isDirectory()) walk(child);
      else if (child !== 'dist/build-manifest.json') paths.push(child);
    }
  }
  walk('dist');
  if (name === 'workbench-protocol') walk('schemas');
  return Object.fromEntries(paths.sort().map(path => [path, sha256(read(root, directory + '/' + path))]));
}
export function assertBrowserBundle(root, name) {
  const code = read(root, 'packages/' + name + '/dist/index.js').toString();
  if (/\b(?:from\s*|import\s*\()\s*["'](?:https?:|file:|node:)/.test(code) ||
      /(?:\/home\/|[A-Z]:\\\\Users\\\\)/.test(code)) {
    fail('workbench_nonportable_artifact', name + '/dist/index.js');
  }
}
