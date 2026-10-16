import {spawnSync} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {json, packages, sha256, validateSources} from './workbench-build-contract.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
if (process.argv.length !== 3) throw new Error('usage: node scripts/pack-workbench.mjs <output-directory>');
const output = resolve(process.cwd(), process.argv[2]);
mkdirSync(output, {recursive: true});
const built = spawnSync(process.execPath, [join(root, 'scripts/build-workbench.mjs'), '--build'],
  {cwd: root, stdio: 'inherit', shell: false});
if (built.error) throw built.error;
if (built.status !== 0) process.exit(built.status ?? 1);
const source = validateSources(root);
const npmCli = [process.env.npm_execpath,
  join(dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js'),
  resolve(dirname(process.execPath), '../lib/node_modules/npm/bin/npm-cli.js')]
  .find(path => path && existsSync(path));
if (!npmCli) throw new Error('workbench_missing_tool: npm-cli.js must accompany Node');

const records = [];
for (const name of packages) {
  const result = spawnSync(process.execPath,
    [npmCli, 'pack', '--ignore-scripts', '--json', '--pack-destination', output],
    {cwd: join(root, 'packages', name), encoding: 'utf8', shell: false});
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error('workbench_pack_failed: ' + name + '\n' + result.stderr);
  const metadata = JSON.parse(result.stdout)[0];
  for (const path of ['LICENSE', 'dist/index.js', 'dist/index.d.ts']) {
    if (!metadata.files.some(file => file.path === path))
      throw new Error('workbench_missing_pack_artifact: ' + name + '/' + path);
  }
  records.push({name: metadata.name, version: metadata.version, file: metadata.filename,
    sha256: sha256(readFileSync(join(output, metadata.filename))),
    build: json(root, 'packages/' + name + '/dist/build-manifest.json')});
}
writeFileSync(join(output, 'workbench-artifacts.json'), JSON.stringify({
  schemaVersion: 1, protocolVersion: source.host.protocolVersion, pluginUiVersions: [1, 2, 3],
  packages: records,
}, null, 2) + '\n');
console.log('[workbench] two private offline packages and workbench-artifacts.json ready');
