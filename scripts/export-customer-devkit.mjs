import { cpSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { resolve, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(process.argv[2] || join(root, '.scratch/customer-devkit'));
if (output === root || !output.startsWith(join(root, '.scratch/'))) throw new Error('Export into a new .scratch subdirectory');
mkdirSync(output, { recursive: true });
const kit = mkdtempSync(join(output, 'hugagent-customer-'));
const vendor = join(kit, 'vendor'); mkdirSync(vendor);
execFileSync(process.execPath, [join(root, 'scripts/build-chat-sdk.mjs')], { cwd: root, stdio: 'inherit' });
const sdk = mkdtempSync(join(output, 'compiled-sdk-'));
const sdkPackage = JSON.parse(readFileSync(join(root, 'packages/chat-sdk/package.json')));
delete sdkPackage.devDependencies; delete sdkPackage.scripts;
writeFileSync(join(sdk, 'package.json'), JSON.stringify(sdkPackage, null, 2) + '\n');
for (const name of ['dist', 'README.md', 'CHANGELOG.md']) cpSync(join(root, 'packages/chat-sdk', name), join(sdk, name), { recursive: true });
const packed = JSON.parse(execFileSync('npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', vendor], { cwd: sdk, encoding: 'utf8' }))[0];
const example = join(root, 'examples/customer-chat');
for (const name of ['src', 'scripts', 'index.html', 'preview.html', 'debug.html', 'session.html', 'tsconfig.json', 'vite.config.mjs', '.env.example', '.gitignore']) {
 cpSync(join(example, name), join(kit, name), { recursive: true });
}
const pkg = JSON.parse(readFileSync(join(example, 'package.json')));
pkg.devDependencies['@hugagent/chat-sdk'] = './vendor/' + packed.filename;
writeFileSync(join(kit, 'package.json'), JSON.stringify(pkg, null, 2) + '\n');
cpSync(join(root, 'document/zh-CN/development/customer-devkit.md'), join(kit, 'README.md'));
execFileSync('npm', ['install', '--package-lock-only', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: kit, stdio: 'inherit' });
const manifest = {};
function walk(folder) {
 for (const entry of readdirSync(folder)) {
  const file = join(folder, entry);
  if (statSync(file).isDirectory()) walk(file);
  else manifest[relative(kit, file).replaceAll('\\', '/')] = createHash('sha256').update(readFileSync(file)).digest('hex');
 }
}
walk(kit);
writeFileSync(join(kit, 'SHA256SUMS.json'), JSON.stringify(manifest, null, 2) + '\n');
execFileSync('tar', ['-czf', join(output, 'hugagent-customer-devkit-1.0.0.tar.gz'), '-C', kit, '.']);
execFileSync('python3', ['-c', 'import shutil,sys;shutil.make_archive(sys.argv[1], "zip", sys.argv[2])',
 join(output, 'hugagent-customer-devkit-1.0.0'), kit]);
writeFileSync(join(output, 'latest.json'), JSON.stringify({ kit, archive: join(output, 'hugagent-customer-devkit-1.0.0.zip') }, null, 2));
console.log('Customer kit: ' + kit);
