import assert from 'node:assert/strict';
import {chromium} from 'playwright';

/** Runs against a real authenticated host. It never replaces HTTP responses. */
const base = process.env.WB18_BASE_URL;
const key = process.env.WB18_APP_KEY;
const storageState = process.env.WB18_STORAGE_STATE;
if (!base || !key || !storageState) throw new Error('Set WB18_BASE_URL, WB18_APP_KEY, WB18_STORAGE_STATE for an isolated test account');
const browser = await chromium.launch({headless: true});
try {
  const context = await browser.newContext({storageState});
  const page = await context.newPage();
  await page.goto(base);
  const directory = await page.request.get(base + '/api/v1/workbench/catalog');
  assert.equal(directory.status(), 200);
  const items = (await directory.json()).data.items;
  assert(items.length >= 100, 'The isolated catalog must contain at least 100 applications');
  const item = items.find(value => value.applicationKey === key);
  assert(item?.availability.available, 'The test application must be visible and enabled');
  assert.equal(await page.locator('iframe').count(), 0, 'Unopened applications must not mount frames');
  const projection = await page.request.get(base + '/api/v1/workbench/me/activity');
  assert.equal(projection.status(), 200);
  assert.equal(await page.locator('iframe').count(), 0, 'Badge projection must not mount frames');

  // Install the generated real package before running this batch.
  await page.goto(base + '/apps/' + encodeURIComponent(key));
  await page.getByRole('region', {name: /应用活动权限|Application activity permissions/}).waitFor();
  await page.getByRole('checkbox', {name: /角标|Badge/}).check();
  await page.getByRole('checkbox', {name: /通知|Notifications/}).check();
  await page.getByRole('button', {name: /保存应用活动授权|Save application activity grants/}).click();
  await page.frameLocator('iframe').getByRole('button', {name: 'Publish activity'}).click();
  await page.getByRole('button', {name: /忽略通知|Dismiss notification/}).waitFor();
  await page.getByRole('button', {name: /忽略通知|Dismiss notification/}).first().click();
  await page.getByRole('button', {name: /更多应用|More applications/}).click();
  const open = page.locator('[data-application-open]').filter({hasText: typeof item.declaration.title === 'string'
    ? item.declaration.title : Object.values(item.declaration.title)[0]}).first();
  await open.waitFor();
  assert.match((await open.getAttribute('aria-label')) || (await open.textContent()) || '', /7/);
  assert.equal(await page.locator('iframe').count(), 1);
  console.log('Real host activity browser checks passed; quota/stop/native matrix remain separate.');
} finally {
  await browser.close();
}
