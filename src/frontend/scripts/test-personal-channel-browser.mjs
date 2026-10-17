import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/personal-channel-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import { ChannelEntry } from './src/components/personal-agent/ChannelEntry';
import './src/styles/variables.css';
import './src/components/personal-agent/personal-agent.css';
createRoot(document.getElementById('root')).render(<ConfigProvider theme={{token:{motion:false}}}><ChannelEntry agentName="Hugo" /></ConfigProvider>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
});
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 1000 } });
  page.setDefaultTimeout(8000);
  const bots = [], routed = new Set(), errors = [];
  let qrAttempt = 0, testFailure = false, routeFailure = false, creates = 0;
  page.on('pageerror', error => errors.push(error.message));
  const origin = 'http://personal-channel.test';
  await page.route(origin + '/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({
      contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css',
      body: await readFile(resolve(output, path.slice(1))),
    });
    if (!path.startsWith('/api/')) return route.fulfill({ contentType: 'text/html',
      body: '<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>' });
    let data = {};
    if (path.endsWith('/adapters')) data = { adapters: ['dingtalk','lark','weixin','wecom'].map(channel_type => ({
      channel_type, supports_long_conn: channel_type !== 'wecom', bind_mode: channel_type === 'weixin' ? 'qr' : 'credentials',
      credential_fields: channel_type === 'weixin' ? [] : ['app_id','app_secret'],
    })) };
    else if (path.endsWith('/bots') && request.method() === 'POST') {
      const body = request.postDataJSON();
      assert.equal(body.channel_type, 'dingtalk');
      creates++;
      data = { channel_id: 'ding', channel_type: body.channel_type, display_name: 'Ding bot', enabled: true, status: 'connected' };
      bots.push(data);
    } else if (path.endsWith('/bots')) data = { bots };
    else if (path.endsWith('/test')) {
      if (testFailure) return route.fulfill({ status: 400, json: { code: 20001, message: '凭据已过期', data: {} } });
      data = { ok: true };
    } else if (path.endsWith('/channels')) data = bots.map(bot => ({ ...bot, personal_agent: routed.has(bot.channel_id) }));
    else if (path.includes('/personal-agent/channels/')) {
      if (routeFailure) { routeFailure = false; return route.fulfill({ status: 503, json: { code: 20001, message: 'Service unavailable' } }); }
      const id = path.split('/').at(-1);
      if (request.postDataJSON().enabled) routed.add(id); else routed.delete(id);
      data = bots.map(bot => ({ ...bot, personal_agent: routed.has(bot.channel_id) }));
    } else if (path.endsWith('/bind/start')) { qrAttempt++; data = { bind_id: 'qr-' + qrAttempt, qrcode_img: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aAkoAAAAASUVORK5CYII=' }; }
    else if (path.endsWith('/status')) {
      if (qrAttempt === 1) data = { status: 'expired' };
      else {
        if (!bots.some(bot => bot.channel_id === 'wx')) bots.push({ channel_id: 'wx', channel_type: 'weixin', display_name: 'WeChat bot', enabled: true, status: 'connected' });
        data = { status: 'confirmed', channel_id: 'wx' };
      }
    }
    return route.fulfill({ json: { code: 10000, message: 'Success', data } });
  });
  await page.goto(origin);
  await page.getByRole('button', { name: '接入频道' }).click();
  const dialog = page.getByRole('dialog');
  assert.equal(await dialog.getByText('管理全部机器人').count(), 0);
  assert.equal(await dialog.getByText(/开启后，这个机器人/).count(), 0);
  const ding = dialog.locator('[data-channel="dingtalk"]');
  await ding.getByRole('button', { name: '绑定机器人' }).click();
  await ding.getByLabel('App ID / CorpID').fill('app');
  await ding.getByLabel('App Secret / Secret').fill('secret-fixture');
  assert.equal(await dialog.getByText('我的机器人', { exact: true }).count(), 0);
  routeFailure = true;
  await ding.getByRole('button', { name: /绑\s*定/, exact: true }).click();
  await ding.getByRole('alert').waitFor();
  await ding.getByRole('button', { name: /重\s*试/, exact: true }).first().click();
  await ding.getByText('绑定成功', { exact: true }).waitFor();
  assert.equal(creates, 1, 'routing retry reuses created bot');
  assert(routed.has('ding'));
  assert.equal(await ding.locator('input').count(), 0, 'success collapses form');

  testFailure = true;
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: '接入频道' }).click();
  await ding.getByText('凭据已过期', { exact: true }).waitFor();
  assert.equal(await ding.getByText('绑定成功').count(), 0, 'failed health check cannot show success');
  testFailure = false;
  await ding.getByRole('button', { name: /重\s*试/, exact: true }).click();
  await ding.getByText('绑定成功', { exact: true }).waitFor();

  const wx = dialog.locator('[data-channel="weixin"]');
  await wx.getByRole('button', { name: '绑定机器人' }).click();
  await wx.getByRole('button', { name: '扫码绑定' }).click();
  await wx.getByText('二维码已过期，请重试').waitFor();
  await page.screenshot({ path: resolve(output, 'expired.png') });
  await wx.getByRole('button', { name: /重\s*试/, exact: true }).click();
  await wx.getByText('绑定成功', { exact: true }).waitFor();
  assert(routed.has('wx'));
  bots.find(bot => bot.channel_id === 'wx').status = 'error';
  bots.find(bot => bot.channel_id === 'wx').last_error = '微信登录已失效';
  await wx.getByText('微信登录已失效').waitFor();
  assert.equal(await wx.getByText('绑定成功').count(), 0);
  await page.screenshot({ path: resolve(output, 'connection-error.png') });
  assert.deepEqual(errors, []);
  console.log('PASS: inline credential/QR binding, automatic Hugo routing, routing retry without duplicate creation, expired QR retry, live connection errors and removed footer');
} finally { await browser.close(); }
