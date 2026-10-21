import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

// Run against the deployed local frontend; no request interception or API fixtures.
const origin = process.env.CONFIG_E2E_BASE || 'http://localhost:3000';
const token = process.env.CONFIG_E2E_TOKEN || execFileSync('docker', [
  'exec', 'hugagent-backend', 'printenv', 'CONFIG_TOKEN',
], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
assert.ok(token, 'A configuration credential is required');
const output = resolve('node_modules/.tmp/config-app-live');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const errors = [], failures = [], responses = [];
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => {
  const path = new URL(response.url()).pathname;
  if (path.startsWith('/api/')) {
    responses.push({ path, status: response.status() });
    if (response.status() >= 400) failures.push({ path, status: response.status() });
  }
});
await page.addInitScript(value => {
  localStorage.setItem('jx_config_token', value);
  localStorage.setItem('jx_config_active_key', 'app-config');
  localStorage.setItem('jx_lang', 'zh-CN');
}, token);
try {
  const legacyPath = '/api/v1/workbench/admin/legacy-config';
  const loaded = page.waitForResponse(r => new URL(r.url()).pathname === legacyPath);
  await page.goto(origin + '/config');
  const response = await loaded;
  assert.equal(response.status(), 200, 'Real legacy configuration is authorized');
  const original = (await response.json()).data.app_config.apps;
  assert.ok(Array.isArray(original));
  const cards = page.locator('.ant-card');
  const add = page.getByRole('button', { name: /新\s*增\s*应\s*用/, exact: true });
  await add.waitFor();
  assert.equal(await cards.count(), original.length || 1);
  assert.equal(await page.getByRole('region', { name: '普通外部应用', exact: true }).count(), 0);
  await add.click();
  const modal = page.getByRole('dialog');
  await modal.waitFor();
  await modal.getByPlaceholder('例如：my_app').fill('e2e_unsaved_application');
  await modal.getByPlaceholder('卡片标题').fill('E2E 临时未保存应用');
  await modal.getByRole('button', { name: /取\s*消/ }).click();
  await modal.waitFor({ state: 'hidden' });
  assert.equal(await cards.count(), original.length || 1, 'Cancel does not add an application');
  if (original.length) {
    const input = cards.first().locator('input').first();
    const value = await input.inputValue();
    await input.fill('E2E 未保存编辑');
    assert.equal(await input.inputValue(), 'E2E 未保存编辑');
    const reloaded = page.waitForResponse(r => new URL(r.url()).pathname === legacyPath);
    await page.getByRole('button', { name: /重\s*新\s*加\s*载/, exact: true }).click();
    await reloaded;
    await page.waitForFunction(expected => document.querySelector('.ant-card input')?.value === expected, value);
  }
  for (const label of ['普通外部应用', '工作台布局策略', '工作台应用配置', '应用诊断']) {
    const header = page.locator('.ant-collapse-header').filter({ hasText: label });
    await header.click();
    const item = header.locator('..');
    await item.locator('.ant-collapse-body').waitFor();
    await page.waitForLoadState('networkidle');
    assert.equal(await item.locator('[role="alert"]').count(), 0, label + ' loads without an alert');
    await header.click();
  }
  await page.reload();
  await add.waitFor();
  assert.equal(await page.locator('.ant-collapse-item-active').count(), 0, 'Refresh returns to the original view');
  await page.waitForFunction(() => [...document.querySelectorAll('.admin-fade-in')].every(el => Number(getComputedStyle(el).opacity) >= 0.99));
  await page.screenshot({ path: resolve(output, 'desktop.png'), fullPage: true });
  await page.locator('.application-config-extensions').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'extensions.png'), fullPage: true });
  await page.setViewportSize({ width: 900, height: 1000 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const current = await context.request.get(origin + legacyPath, { headers: { Authorization: 'Bearer ' + token } });
  assert.equal(current.status(), 200);
  assert.deepEqual((await current.json()).data.app_config.apps, original, 'Live application configuration remains unchanged');
  assert.deepEqual(errors, [], 'No browser runtime errors');
  assert.deepEqual(failures, [], 'No failed API requests');
  await writeFile(resolve(output, 'result.json'), JSON.stringify({ origin, applicationCount: original.length,
    checks: ['real configuration load', 'original cards first', 'add dialog cancel', 'unsaved edit and reload',
      'all four extension panels', 'refresh persistence', 'responsive width', 'unchanged saved data', 'no runtime or API errors'],
    responses }, null, 2));
  console.log('Live config E2E passed: real frontend/backend, ' + original.length + ' applications, 4 extension panels; saved data unchanged');
} finally { await context.close(); await browser.close(); }
