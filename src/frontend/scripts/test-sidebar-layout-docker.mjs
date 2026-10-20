// Real Docker deployment acceptance test. Requires a disposable account credential file.
import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const base = process.env.LAYOUT_E2E_URL || 'http://127.0.0.1:3000';
const root = process.env.LAYOUT_E2E_ROOT || '/tmp/hugagent-sidebar-layout-e2e';
const account = JSON.parse(await readFile(resolve(root, 'auth.json'), 'utf8'));
const results = [];
await mkdir(root, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
const contexts = [];
const errors = [];
// Keep real browser navigations within the deployment's per-IP burst limit.
const settle = () => new Promise(resolve => setTimeout(resolve, 3000));
async function login(context, path = '/') {
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/login?redirect=' + encodeURIComponent(path));
  await page.locator('#loginForm input[name=username]').fill(account.username);
  await page.locator('#loginForm input[name=password]').fill(account.password);
  await page.locator('#loginForm button[type=submit]').click();
  await page.locator('.jx-moduleShell, .jx-classicShell').waitFor({ timeout: 30000 });
  await settle();
  return page;
}
async function preferences(page) {
  const response = await page.request.get(base + '/api/v1/users/' + account.user_id + '/preferences');
  assert.equal(response.status(), 200);
  return (await response.json()).data;
}
async function sessionSettings(page) {
  await settle();
  await page.goto(base + '/settings/session');
  await page.getByRole('radio', { name: /现代模式/ }).waitFor();
}
const apply = page => page.getByRole('button', { name: /^应\s*用$/ });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  contexts.push(context);
  const page = await login(context, '/settings/session');
  // Normalize a prior interrupted run on this disposable account.
  if ((await preferences(page)).sidebar_layout === 'classic') {
    await page.request.put(base + '/api/v1/users/' + account.user_id + '/preferences', { data: { sidebar_layout: 'modern' } });
    await page.reload();
    await page.locator('.jx-moduleShell').waitFor();
    await page.waitForLoadState('networkidle');
  }
  const initialDefault = await preferences(page);
  assert.equal(initialDefault.sidebar_layout, 'modern', 'new account defaults to modern from the server');
  results.push('database default: modern');
  const setup = await page.request.put(base + '/api/v1/users/' + account.user_id + '/preferences', {
    data: { theme: 'dark', language: 'en', default_model: 'layout-acceptance-model' },
  });
  assert.equal(setup.status(), 200);
  const initial = await preferences(page);
  await sessionSettings(page);
  let navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
  await page.getByRole('radio', { name: /经典模式/ }).check();
  assert.equal(navigations, 0, 'selection does not reload');
  assert.equal((await preferences(page)).sidebar_layout, 'modern', 'selection does not save');
  await apply(page).click();
  await page.getByRole('dialog').getByRole('button', { name: /取\s*消/ }).click();
  assert.equal(navigations, 0, 'cancel does not reload');
  assert.equal((await preferences(page)).sidebar_layout, 'modern', 'cancel does not save');
  results.push('draft selection and cancellation preserve layout');
  await apply(page).click();
  await settle();
  await page.getByRole('dialog').getByRole('button', { name: '应用并刷新' }).click();
  await page.locator('.jx-classicShell').waitFor();
  assert.ok(navigations > 0, 'confirmation reloads the page');
  await settle();
  const saved = await preferences(page);
  assert.equal(saved.sidebar_layout, 'classic');
  const { sidebar_layout: _before, ...otherBefore } = initial;
  const { sidebar_layout: _after, ...otherAfter } = saved;
  assert.deepEqual(otherAfter, otherBefore, 'layout save preserves other preferences');
  await page.screenshot({ path: resolve(root, 'classic-settings.png') });
  results.push('confirm saves classic to database, reloads and preserves other preferences');

  await settle();
  const fresh = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  contexts.push(fresh);
  const second = await login(fresh, '/settings/session');
  await second.locator('.jx-classicShell').waitFor();
  await second.getByRole('radio', { name: /经典模式/ }).waitFor();
  assert.equal(await second.getByRole('radio', { name: /经典模式/ }).isChecked(), true);
  assert.ok(!(await fresh.storageState()).origins.flatMap(origin => origin.localStorage).some(item => /sidebar[_-]layout/i.test(item.name)));
  results.push('new browser context with empty storage restores database choice; layout is not in browser storage');
  await second.locator('.jx-classicNav').getByRole('button', { name: '定时任务', exact: true }).click();
  await second.locator('.jx-taskSidebar').waitFor();
  await second.locator('.jx-classicNav').getByRole('button', { name: '我的空间', exact: true }).click();
  await second.locator('.jx-spaceNavigation').waitFor();
  for (const name of ['个人文件夹', '公共知识库', '私有知识库', '会话收藏', '分享记录', '消息通知']) {
    await second.locator('.jx-spaceNavigation').getByRole('button', { name: new RegExp(name) }).waitFor();
  }
  await second.locator('.jx-classicNav').getByRole('button', { name: '智能体', exact: true }).click();
  await second.waitForURL('**/ability-center/agents');
  await second.getByRole('button', { name: '更多', exact: true }).click();
  await second.getByRole('menuitem', { name: '项目', exact: true }).click();
  await second.waitForURL('**/projects');
  results.push('classic task, space, capability and project navigation');

  await second.setViewportSize({ width: 390, height: 844 });
  await second.getByRole('button', { name: '打开侧边栏', exact: true }).first().click();
  await second.locator('.jx-classicInner').waitFor();
  await second.screenshot({ path: resolve(root, 'classic-mobile.png') });
  await second.getByRole('button', { name: '关闭侧边栏', exact: true }).click({ position: { x: 350, y: 400 } });
  assert.ok(await second.locator('.jx-classicInner').count() === 0);
  results.push('mobile drawer opens and closes');

  const denied = await second.request.put(base + '/api/v1/users/not-the-current-user/preferences', { data: { sidebar_layout: 'classic' } });
  assert.equal(denied.status(), 403);
  const invalid = await second.request.put(base + '/api/v1/users/' + account.user_id + '/preferences', { data: { sidebar_layout: 'invalid' } });
  assert.equal(invalid.status(), 422);
  results.push('cross-user write denied; invalid layout rejected');

  await second.setViewportSize({ width: 1440, height: 1000 });
  await sessionSettings(second);
  await fresh.setOffline(true);
  await second.getByRole('radio', { name: /现代模式/ }).check();
  await apply(second).click();
  await settle();
  await second.getByRole('dialog').getByRole('button', { name: '应用并刷新' }).click();
  await second.getByText('无法保存排布，请检查网络后重试。', { exact: true }).waitFor();
  assert.ok(await second.locator('.jx-classicShell').count() > 0);
  await fresh.setOffline(false);
  assert.equal((await preferences(second)).sidebar_layout, 'classic');
  await second.getByRole('dialog').getByRole('button', { name: /取\s*消/ }).click();
  results.push('network save failure preserves database and current layout');

  await second.getByRole('radio', { name: /现代模式/ }).check();
  await apply(second).click();
  await settle();
  await second.getByRole('dialog').getByRole('button', { name: '应用并刷新' }).click();
  await second.locator('.jx-moduleShell').waitFor();
  await settle();
  await second.reload();
  await second.locator('.jx-moduleShell').waitFor();
  await settle();
  assert.equal((await preferences(second)).sidebar_layout, 'modern');
  await second.screenshot({ path: resolve(root, 'modern-settings.png') });
  results.push('modern restoration survives reload');
  await second.locator('.jx-moduleRail').getByRole('button', { name: '对话', exact: true }).click();
  await second.locator('.jx-newChatBtn').click();
  await second.waitForURL(base + '/');
  results.push('conversation navigation returns home');
  assert.deepEqual(errors, []);
  await writeFile(resolve(root, 'results.json'), JSON.stringify({ passed: results, browserErrors: errors }, null, 2));
  console.log(JSON.stringify({ passed: results, browserErrors: errors }, null, 2));
} catch (error) {
  console.log(JSON.stringify({ passedBeforeFailure: results }));
  const last = contexts.at(-1)?.pages().at(-1);
  if (last) {
    await last.screenshot({ path: resolve(root, 'failure.png') });
    console.log((await last.locator('.jx-spaceNavigation').innerText({ timeout: 1000 }).catch(() => '')).slice(0, 1000));
  }
  throw error;
} finally {
  for (const context of contexts) await context.close();
  await browser.close();
}
