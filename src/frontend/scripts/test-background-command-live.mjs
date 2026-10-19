import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { chromium } from 'playwright';

const [identityPath, scenario = 'cancel', action = 'cancel'] = process.argv.slice(2);
assert.ok(identityPath, 'Pass the protected live-test identity file');
const state = JSON.parse(await readFile(resolve(identityPath), 'utf8'));
const info = state.chats[scenario];
assert.ok(info);
const output = dirname(resolve(identityPath));
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([{ name: state.cookie_name, value: state.token,
    url: 'http://127.0.0.1:3000', httpOnly: true, sameSite: 'Lax' }]);
  await context.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', failure => errors.push(failure.message));
  await page.goto('http://127.0.0.1:3000/c/' + info.chat_id);
  const region = page.getByRole('region', { name: '后台命令' });
  try {
    await page.locator('.jx-msg.assistant').first().waitFor({ timeout: 45000 });
    assert.equal(await region.count(), 0);
    await page.screenshot({ path: resolve(output, scenario + '-before.png'), fullPage: true });
    await writeFile(resolve(output, scenario + '-browser-ready.json'), JSON.stringify({ chat_id: info.chat_id }));
    if (action === 'cancel') {
      const result = await context.request.get('http://127.0.0.1:3000/api/v1/commands?chat_id=' + info.chat_id);
      const task = (await result.json()).data[0];
      const cancelled = await context.request.post('http://127.0.0.1:3000/api/v1/commands/' + task.task_id + '/cancel');
      assert.ok(cancelled.ok());
    } else {
      const deadline = Date.now() + 600000;
      let status = '';
      while (Date.now() < deadline) {
        const result = await context.request.get('http://127.0.0.1:3000/api/v1/commands?chat_id=' + info.chat_id);
        status = result.ok() ? (await result.json()).data[0].status : 'unavailable';
        if (status === 'completed') break;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      assert.equal(status, 'completed');
      await page.waitForFunction(marker =>
        [...document.querySelectorAll('.jx-msg.assistant')].some(message => message.textContent?.includes(marker)),
      'FINISH_' + info.marker);
    }
    await page.screenshot({ path: resolve(output, scenario + '-after.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'passed', scenario, action, chat_id: info.chat_id }));
  } catch (failure) {
    await page.screenshot({ path: resolve(output, scenario + '-error.png'), fullPage: true });
    await writeFile(resolve(output, scenario + '-dom.txt'), await page.locator('body').innerText());
    throw failure;
  }
} finally {
  await browser.close();
}
