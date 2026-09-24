/** Real browser → grant API → real Rust transport/UOS controller, no external protocol.
 * Upstream SSO is the fixture boundary; no production accounts, DB or services are used.
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
import { DeviceLogin } from '../../../desktop-uos/src/device-login.mjs';
import { createHttpClient } from '../../../desktop-uos/src/http-client.mjs';
const root = resolve('../..');
const children = [];
function child(cmd, args, options = {}) {
  const p = spawn(cmd, args, { cwd: root, env: { ...process.env, PYTHONPATH: 'src/backend' }, ...options });
  children.push(p);
  p.errors = '';
  p.stderr.on('data', chunk => { p.errors += chunk; });
  p.output = '';
  p.on('exit', code => { if (code) console.error('Child failed', cmd, code, p.errors); });
  p.stdout.on('data', chunk => { p.output += chunk; });
  return p;
}
async function until(fn, message, ms = 30000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    for (const p of children) if (p.exitCode !== null && p.exitCode !== 0) throw new Error('E2E child failed: ' + p.errors);
    const result = await fn();
    if (result) return result;
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error(message);
}
const server = child('.venv/bin/python', ['src/backend/tests/desktop_login_browser_server.py']);
let browser;
try {
  const base = await until(() => server.output.match(/FIXTURE_URL (\S+)/)?.[1], 'fixture startup: ' + server.errors);
  await until(async () => { try { return (await fetch(base)).ok; } catch { return false; } }, 'fixture listening');
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
  const context = await browser.newContext();
  await context.request.post(base + '/__test__/session');
  const page = await context.newPage();
  page.on('response', async response => {
    if (response.url().endsWith('/approve') || response.url().endsWith('/deny')) {
      console.log('Browser decision', response.status(), await response.text());
    }
  });
  let protocols = 0;
  page.on('request', request => { if (request.url().startsWith('hugagent:')) protocols++; });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  const rust = child('cargo', ['test', '--lib', '--offline', 'device_login::tests::browser_e2e', '--', '--ignored', '--nocapture'], {
    cwd: resolve(root, 'desktop/src-tauri'),
    env: { ...process.env, DESKTOP_LOGIN_E2E_BASE: base, TAURI_CONFIG: '{"bundle":{"resources":[]}}' },
  });
  const url = await until(() => rust.output.match(/DEVICE_LOGIN_URL (\S+)/)?.[1],
    'Rust E2E transport startup', 180000);
  await page.goto(url);
  await page.getByRole('button', { name: '确认登录桌面端', exact: true }).waitFor();
  await until(() => page.getByRole('button', { name: '确认登录桌面端', exact: true }).isEnabled(), 'approval ready');
  assert.match(await page.locator('#desktop-approval-account').innerText(), /端到端测试账号/);
  await page.waitForTimeout(2000); // regression: the former 1.5s auto-close must never run
  assert.equal(page.isClosed(), false);
  assert.ok(!rust.output.includes('DEVICE_LOGIN_COMPLETE'), 'no login before explicit approval');
  await page.getByRole('button', { name: '确认登录桌面端', exact: true }).click();
  await until(() => rust.output.includes('DEVICE_LOGIN_COMPLETE'), 'desktop must receive approval');
  await until(async () => (await page.locator('#desktop-approval-status').innerText()).includes('已登录成功'), 'browser completion receipt');
  assert.equal(protocols, 0, 'authentication must not invoke an external protocol');
  assert.ok(!url.includes('secret'));
  console.log('PASS: built browser UI → real API → Rust desktop transport → authenticated request → receipt; no deep-link');

  const http = createHttpClient();
  let opened, accepted = false;
  const uos = new DeviceLogin({
    http, serverBase: base, cookieName: (await context.cookies()).find(c => c.httpOnly).name,
    currentEpoch: () => 1, openBrowser: url => { opened = url; },
    accept: async (token, _epoch, valid) => {
      assert.ok(valid());
      const response = await http.fetch(base + '/api/v1/auth/session/check', { headers: { cookie: uos.cookieName + '=' + token } });
      assert.equal(response.status, 200); accepted = true; return true;
    },
  });
  await uos.start();
  await page.goto(opened);
  await until(() => page.getByRole('button', { name: '确认登录桌面端', exact: true }).isEnabled(), 'UOS confirmation ready');
  // Offline confirmation is recoverable and must not silently authorize.
  await context.setOffline(true);
  await page.getByRole('button', { name: '确认登录桌面端', exact: true }).click();
  await page.getByRole('button', { name: '重试', exact: true }).waitFor();
  assert.equal(accepted, false);
  await context.setOffline(false);
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await until(() => page.getByRole('button', { name: '确认登录桌面端', exact: true }).isEnabled(), 'recover approval');
  await page.getByRole('button', { name: '确认登录桌面端', exact: true }).click();
  await until(() => accepted && uos.snapshot().status === 'completed', 'UOS controller acceptance');
  await until(async () => (await page.locator('#desktop-approval-status').innerText()).includes('已登录成功'), 'UOS receipt');

  await uos.start();
  await page.goto(opened);
  await until(() => page.getByRole('button', { name: '拒绝', exact: true }).isEnabled(), 'denial ready');
  await page.getByRole('button', { name: '拒绝', exact: true }).click();
  await until(() => uos.snapshot().status === 'error', 'desktop learns denial');

  await uos.start();
  await page.goto(opened);
  await until(() => page.getByRole('button', { name: '拒绝', exact: true }).isEnabled(), 'cancellation ready');
  uos.cancel();
  await until(async () => (await page.locator('#desktop-approval-status').innerText()).includes('已取消'), 'browser learns desktop cancellation');
  assert.equal(protocols, 0);
  assert.deepEqual(errors, []);
  http.destroy();
  console.log('PASS: UOS production controller; offline recovery, refusal, cancellation and zero browser errors');
  await page.screenshot({ path: '/tmp/desktop-device-login-e2e.png', fullPage: true });
} catch (error) {
  console.error('E2E children diagnostics', children.map(p => ({exit: p.exitCode, stderr: p.errors.slice(-5000), stdout: p.output.slice(-3000)})));
  throw error;
} finally {
  await browser?.close();
  for (const p of children) if (p.exitCode === null) p.kill('SIGTERM');
}
