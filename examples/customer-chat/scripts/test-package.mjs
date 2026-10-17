import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import assert from 'node:assert/strict';
const pkg = JSON.parse(readFileSync('package.json'));
const sdkFile = pkg.devDependencies['@hugagent/chat-sdk'].replace(/^file:/, '');
if (!sdkFile.endsWith('.tgz')) throw new Error('Run this check in the exported developer kit');
const component = resolve(pkg.name.replaceAll('@', '').replaceAll('/', '-') + '-' + pkg.version + '.tgz');
if (!existsSync(component)) throw new Error('Run npm pack first');
const host = mkdtempSync(join(tmpdir(), 'hugagent-customer-host-'));
const react = JSON.parse(readFileSync('node_modules/react/package.json')).version;
const vite = JSON.parse(readFileSync('node_modules/vite/package.json')).version;
writeFileSync(join(host, 'package.json'), JSON.stringify({ private: true, type: 'module', dependencies: {
 '@hugagent/chat-sdk': 'file:' + resolve(sdkFile), [pkg.name]: 'file:' + component, react, vite,
} }, null, 2));
execFileSync('npm', ['install', '--prefer-offline', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: host, stdio: 'pipe' });
const installed = JSON.parse(readFileSync(join(host, 'node_modules', pkg.name, 'package.json')));
assert.equal(installed.dependencies?.['@hugagent/chat-sdk'], undefined);
assert.ok(installed.peerDependencies['@hugagent/chat-sdk']);
writeFileSync(join(host, 'entry.js'), "export { default } from " + JSON.stringify(pkg.name) + ";");
writeFileSync(join(host, 'build.mjs'), "import{build}from'vite';await build({configFile:false,logLevel:'error',build:{lib:{entry:'entry.js',formats:['es'],fileName:'customer',cssFileName:'customer'}}});");
execFileSync(process.execPath, ['build.mjs'], { cwd: host, stdio: 'pipe' });
assert.ok(readFileSync(join(host, 'dist/customer.js'), 'utf8').includes('Customer Portal'));
assert.ok(existsSync(join(host, 'dist/customer.css')));
if (process.env.HUGAGENT_EXPECT_HELPER) {
 assert.ok(readFileSync(join(host, 'dist/customer.js'), 'utf8').includes('Customer helper included'));
 assert.ok(readFileSync(join(host, 'dist/customer.css'), 'utf8').includes('customer-extra-badge'));
}
console.log('PASS customer tarball installs and builds in a fresh host: ' + host);
