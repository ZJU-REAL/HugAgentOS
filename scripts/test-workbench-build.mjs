import assert from 'node:assert/strict';
import {cpSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import test from 'node:test';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const scratch = join(repo, '.scratch', 'workbench-22', 'build-fixture');
mkdirSync(scratch, {recursive: true});
function fixture(name) {
  const root = join(scratch, name);
  rmSync(root, {recursive: true, force: true});
  mkdirSync(join(root, 'scripts'), {recursive: true});
  cpSync(join(repo, 'scripts'), join(root, 'scripts'), {recursive: true,
    filter: path => !path.includes('node_modules') && !path.includes('/deploy/')});
  cpSync(join(repo, 'packages'), join(root, 'packages'), {recursive: true,
    filter: path => !['node_modules', 'dist'].includes(path.split('/').at(-1))});
  for (const path of ['src/frontend/package.json', 'src/frontend/package-lock.json',
    'src/backend/features/workbench/schemas']) {
    mkdirSync(dirname(join(root, path)), {recursive: true});
    cpSync(join(repo, path), join(root, path), {recursive: true});
  }
  symlinkSync(join(repo, 'src/frontend/node_modules'), join(root, 'src/frontend/node_modules'), 'dir');
  return root;
}
function run(root, ...args) {
  return spawnSync(process.execPath, [join(root, 'scripts/build-workbench.mjs'), ...args],
    {cwd: root, encoding: 'utf8'});
}
test('build refuses a missing license before compiling any shared package', () => {
  const root = fixture('missing-license');
  rmSync(join(root, 'packages/workbench-sdk/LICENSE'), {force: true});
  const result = run(root, '--check');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /workbench_missing_file.*workbench-sdk\/LICENSE/);
});

test('source checks reject lock drift and incompatible host version', () => {
  for (const [name, path, change, expected] of [
    ['lock-drift', 'packages/workbench-sdk/package-lock.json', value => {
      value.packages[''].version = '0.0.0';
    }, /workbench_lock_drift/],
    ['version-drift', 'packages/workbench-protocol/schemas/host.json', value => {
      value.protocolVersion = '2.0.0';
    }, /workbench_version_mismatch/],
  ]) {
    const root = fixture(name);
    const file = join(root, path);
    const value = JSON.parse(readFileSync(file, 'utf8'));
    change(value);
    writeFileSync(file, JSON.stringify(value));
    const result = run(root, '--check');
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, expected);
  }
});
test('relocated source builds and emits offline version diagnostics; stale inputs fail closed', () => {
  const root = fixture('portable source');
  const build = run(root, '--build');
  assert.equal(build.status, 0, build.stderr);
  const output = join(root, 'web');
  mkdirSync(output);
  writeFileSync(join(output, 'index.html'), '<html><script src="/assets/main.js"></script></html>');
  const result = run(root, '--dist', output);
  assert.equal(result.status, 0, result.stderr);
  const diagnostic = JSON.parse(readFileSync(join(output, 'workbench-build.json'), 'utf8'));
  assert.equal(diagnostic.protocolVersion, '1.0.0');
  assert.equal(diagnostic.sdkVersion, '1.0.0');
  assert.deepEqual(diagnostic.pluginUiVersions, [1, 2, 3]);
  assert.equal(run(root, '--verify-dist', output).status, 0);
  // The archive seam uses actual compiled outputs, not a mocked package builder.
  rmSync(join(root, 'src/frontend/node_modules'), {force: true});
  mkdirSync(join(root, 'src/frontend/dist'), {recursive: true});
  cpSync(output, join(root, 'src/frontend/dist'), {recursive: true});
  for (const file of ['desktop-bundle.json', 'pyproject.toml', 'requirements-mem0.txt'])
    writeFileSync(join(root, file), '{}');
  const archive = spawnSync('python3', [join(repo, 'desktop/scripts/create-ce-archive.py'),
    '--source', root, '--output', join(scratch, 'portable.zip')], {encoding: 'utf8'});
  assert.equal(archive.status, 0, archive.stderr);

  const source = join(root, 'packages/workbench-sdk/src/index.ts');
  writeFileSync(source, readFileSync(source, 'utf8') + '\n// next source revision\n');
  const stale = run(root, '--dist', output);
  assert.notEqual(stale.status, 0);
  assert.match(stale.stderr, /workbench_stale_artifact/);
});
test('distribution refuses missing compiled artifacts', () => {
  const root = fixture('missing-artifact');
  const result = run(root, '--dist', join(root, 'web'));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /workbench_missing_file.*build-manifest/);
});

test('desktop archive refuses a payload missing shared package diagnostics', () => {
  const root = join(scratch, 'archive-missing');
  rmSync(root, {recursive: true, force: true});
  mkdirSync(join(root, 'src/frontend/dist'), {recursive: true});
  for (const file of ['desktop-bundle.json', 'pyproject.toml', 'requirements-mem0.txt', 'src/frontend/dist/index.html']) {
    writeFileSync(join(root, file), '{}');
  }
  const result = spawnSync('python3', [join(repo, 'desktop/scripts/create-ce-archive.py'),
    '--source', root, '--output', join(scratch, 'archive-missing.zip')], {encoding: 'utf8'});
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /workbench|workbench-build/);
});
