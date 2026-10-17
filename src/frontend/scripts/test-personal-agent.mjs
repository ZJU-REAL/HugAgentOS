import assert from 'node:assert/strict';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.PERSONAL_E2E_FRONTEND || 'http://127.0.0.1:13010';
const output = resolve('../../.scratch/pa-e2e/frontend');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
const errors = [];
const context = await browser.newContext({ viewport: { width: 1550, height: 1050 }, extraHTTPHeaders: { Authorization: 'Bearer pa-e2e-owner' } });
try {
  const session = JSON.parse(await readFile(resolve("../../.scratch/pa-e2e/session.json"), "utf-8"));
  await context.addCookies([{ name: "jx_session", value: session.token, url: origin, httpOnly: true, sameSite: "Lax" }]);
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin, { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: 'Personal Agent', exact: true }).waitFor({ timeout: 45000 });
  await page.getByRole('button', { name: 'Personal Agent', exact: true }).click();
  await page.locator('.pa-panel').waitFor();
  await page.locator('.pa-panel[aria-busy="true"]').waitFor({ state: 'hidden' });
  await page.getByRole('textbox', { name: '发送给 Personal Agent' }).waitFor();
  await page.screenshot({ path: resolve(output, '01-personal-agent-desktop.png'), fullPage: true });
  assert.equal(await page.locator('.pa-monitor').isVisible(), true);
  assert.equal(await page.locator('.pa-panel .jx-thinking').count(), 0);
  assert.equal(await page.locator('.pa-panel .jx-toolCall').count(), 0);
  const before = await context.request.get(origin + '/api/v1/personal-agent/work-items');
  assert.equal(before.status(), 200);
  const initial = (await before.json()).data.length;
  async function chooseNew(){
    await page.getByRole('combobox',{name:'消息处理方式'}).click();
    await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option').filter({hasText:'作为新任务'}).click();
  }
  const nonce = Date.now();
  const goals = [
    `前端端到端测试 ${nonce}-A：在云端任务目录创建 ui-A.txt，内容为 UI_A_VERIFIED，并读回确认。最后只回复文件路径和结果。`,
    `前端端到端测试 ${nonce}-B：在云端执行 sleep 5，然后计算 17*19。最后回复计算结果。`,
    `前端端到端测试 ${nonce}-C：列出你的专属工作目录中的文件名。只回复列表。`,
  ];
  for (const goal of goals) {
    await chooseNew();
    await page.getByRole('textbox', { name: '发送给 Personal Agent' }).fill(goal);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.pa-composer textarea').value === '');
  }
  const attachmentGoal = `前端附件测试 ${nonce}-D：读取本任务输入附件 ui-input-${nonce}.txt，确认内容为 PA_INPUT_${nonce}。把该文件导出为回复的下载附件，最后回复验证内容和下载链接。`;
  goals.push(attachmentGoal);
  await page.locator('input[type="file"][aria-label="添加附件"]').setInputFiles({ name: `ui-input-${nonce}.txt`, mimeType: "text/plain", buffer: Buffer.from(`PA_INPUT_${nonce}`) });
  await page.getByText(`ui-input-${nonce}.txt`, { exact: true }).first().waitFor();
  await chooseNew();
  await page.getByRole('textbox', { name: '发送给 Personal Agent' }).fill(attachmentGoal);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.pa-composer textarea').value === '');
  for(let attempt=0;attempt<60;attempt++){
    const admitted=await context.request.get(origin+'/api/v1/personal-agent/work-items');
    if((await admitted.json()).data.length>=initial+goals.length)break;
    assert.ok(attempt<59,'Background admission timed out');await new Promise(resolve=>setTimeout(resolve,500));
  }
  await page.screenshot({ path: resolve(output, '02-concurrent-tasks.png'), fullPage: true });
  await page.close(); // A closed browser page must not own the task's lifetime.
  let tasks = [];
  for (let i = 0; i < 180; i++) {
    const response = await context.request.get(origin + '/api/v1/personal-agent/work-items');
    tasks = (await response.json()).data.filter(task => goals.includes(task.goal));
    if (tasks.length === goals.length && tasks.every(task => ['completed', 'failed', 'waiting'].includes(task.status))) break;
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  assert.ok(tasks.every(task => task.status === 'completed'), JSON.stringify(tasks.map(task => ({ goal: task.goal, status: task.status, reason: task.waiting_reason }))));
  const reopened = await context.newPage();
  await reopened.goto(origin + '/personal-agent');
  await reopened.locator('.pa-message-assistant').first().waitFor({ timeout: 45000 });
  const attachmentTask = tasks.find(task => task.goal === attachmentGoal);
  assert.ok(attachmentTask.result.includes(`PA_INPUT_${nonce}`), attachmentTask.result);
  const downloadUrl = attachmentTask.result.match(/\/(?:api\/)?files\/[A-Za-z0-9_-]+/)?.[0];
  assert.ok(downloadUrl, "Final reply must include an artifact download link");
  const download = await context.request.get(origin + downloadUrl);
  assert.equal(download.status(), 200);
  assert.equal((await download.body()).toString(), `PA_INPUT_${nonce}`);
  await reopened.screenshot({ path: resolve(output, '03-final-replies.png'), fullPage: true });
  await reopened.setViewportSize({ width: 720, height: 950 });
  await reopened.getByRole('button', { name: '监控', exact: true }).click();
  assert.equal(await reopened.locator('.pa-monitor').isVisible(), true);
  await reopened.screenshot({ path: resolve(output, '04-narrow-monitor.png'), fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(resolve(output, 'result.json'), JSON.stringify({ passed: true, tasks: tasks.map(task => ({ id: task.id, status: task.status })), checks: ['sidebar below new chat', 'final-only conversation', 'desktop monitor', 'four consecutive messages', 'tasks continue after page close', 'reopen final replies', 'narrow monitor', 'explicit attachment upload, model read, export and real download'], browserErrors: errors }, null, 2));
  console.log('PASS: real frontend and backend; four model tasks continue after page close.');
} catch (error) {
  const pages = context.pages();
  if (pages.length) { await pages[0].screenshot({ path: resolve(output, 'failure.png'), fullPage: true }); console.log((await pages[0].locator('body').innerText()).slice(-8000)); }
  throw error;
} finally { await browser.close(); }
