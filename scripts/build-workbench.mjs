import {spawnSync} from 'node:child_process';
import {copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {artifactRecords, assertBrowserBundle, fail, inputHash, json, packages,
  read, sha256, validateSources} from './workbench-build-contract.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
function run(script, ...arguments_) {
  const result = spawnSync(process.execPath, [join(root, script), ...arguments_],
    {cwd: root, stdio: 'inherit', shell: false});
  if (result.error || result.status !== 0) fail('workbench_build_failed', script);
}
async function main() {
  if (args.length && !['--check', '--build', '--dist', '--verify-dist'].includes(args[0]))
    fail('workbench_invalid_argument', 'use --check, --build, --dist <directory> or --verify-dist <directory>');
  const source = validateSources(root);
  if (args[0] === '--check') {
    console.log('[workbench] source, lock, license and version checks passed');
    return;
  }
  if (!args.length || args[0] === '--build') {
    // Shared artifacts are always built before the frontend, with its locked tools.
    for (const name of packages) {
      rmSync(join(root, 'packages', name, 'dist'), {recursive: true, force: true});
      run('packages/' + name + '/scripts/build.mjs', '--host-tools');
    }
    run('scripts/sync-workbench-schema.mjs', '--check');
    const sdk = await import(pathToFileURL(join(root, 'packages/workbench-sdk/dist/index.js')));
    if (sdk.SDK_VERSION !== source.definitions['workbench-sdk'].version ||
        sdk.PROTOCOL_VERSION !== source.host.protocolVersion)
      fail('workbench_version_mismatch', 'built SDK constants differ from package/schema versions');
    for (const name of packages) {
      assertBrowserBundle(root, name);
      writeFileSync(join(root, 'packages', name, 'dist/build-manifest.json'),
        JSON.stringify({inputHash: inputHash(root, name), artifacts: artifactRecords(root, name)}, null, 2) + '\n');
    }
    console.log('[workbench] built protocol ' + source.host.protocolVersion + ' → SDK ' + sdk.SDK_VERSION);
    return;
  }
  const records = {};
  for (const name of packages) {
    const manifest = json(root, 'packages/' + name + '/dist/build-manifest.json');
    const artifacts = artifactRecords(root, name);
    if (manifest.inputHash !== inputHash(root, name) || JSON.stringify(manifest.artifacts) !== JSON.stringify(artifacts))
      fail('workbench_stale_artifact', name + '; run the frontend prebuild again');
    assertBrowserBundle(root, name);
    records[name] = {version: source.definitions[name].version, inputHash: manifest.inputHash, artifacts};
  }
  if (!args[1]) fail('workbench_invalid_argument', args[0] + ' requires a directory');
  const output = resolve(process.cwd(), args[1]);
  const html = readFileSync(join(output, 'index.html'), 'utf8');
  if (/<(?:script|link)\b[^>]*(?:src|href)=["'](?:https?:)?\/\//i.test(html))
    fail('workbench_external_asset', 'entry point must use bundled script/styles');
  const diagnostic = {
    schemaVersion: 1,
    protocolVersion: source.host.protocolVersion,
    pluginUiVersions: [1, 2, 3],
    sdkVersion: source.definitions['workbench-sdk'].version,
    capabilities: source.host.capabilities,
    packages: records,
    frontendLockSha256: sha256(read(root, 'src/frontend/package-lock.json')),
  };
  if (args[0] === '--verify-dist') {
    if (JSON.stringify(json(output, 'workbench-build.json')) !== JSON.stringify(diagnostic))
      fail('workbench_distribution_drift', 'workbench-build.json does not match the current source/locks');
    for (const name of packages) {
      if (sha256(read(output, 'licenses/' + name + '.txt')) !== records[name].artifacts.LICENSE)
        fail('workbench_distribution_drift', name + ' license');
    }
  } else {
    mkdirSync(join(output, 'licenses'), {recursive: true});
    for (const name of packages)
      copyFileSync(join(root, 'packages', name, 'LICENSE'), join(output, 'licenses', name + '.txt'));
    writeFileSync(join(output, 'workbench-build.json'), JSON.stringify(diagnostic, null, 2) + '\n');
  }
  console.log('[workbench] ' + args[0] + ': protocol ' + diagnostic.protocolVersion +
    ', SDK ' + diagnostic.sdkVersion + ', plugin UI v1/v2/v3');
}
main().catch(error => { console.error('[workbench] ' + error.message); process.exitCode = 1; });
