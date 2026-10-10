import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/channel-binding-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ChannelBotsPanel } from './src/components/settings/ChannelBotsPanel';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { setHybridDual } from './src/api';
import 'antd/dist/reset.css';
setHybridDual(true);
useDeploymentModeStore.setState({provisionMode:'dual',localReady:true});
createRoot(document.getElementById('root')).render(<ChannelBotsPanel/>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
});
const origin = 'http://127.0.0.1:18496';
const browser = await chromium.launch({ headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
});
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  page.setDefaultTimeout(6000);
  await page.addInitScript(() => localStorage.setItem('jx_lang','zh-CN'));
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let failRegistration = true, polls = 0, starts = 0, releasePoll;
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
      return route.fulfill({contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
        body: await readFile(resolve(output,url.pathname.slice(1)))});
    }
    if (!url.pathname.startsWith('/api/')) {
      return route.fulfill({contentType:'text/html',body:'<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
    }
    let data = {};
    if (url.pathname.endsWith('/adapters')) data = {adapters:[{channel_type:'weixin',supports_long_conn:true,bind_mode:'qr',credential_fields:[]}]};
    if (url.pathname.endsWith('/bots')) data = {bots:[]};
    if (url.pathname.endsWith('/local-binding')) {
      assert.equal(req.headers()['x-hugagent-target'],'local');
      if (failRegistration) {
        failRegistration=false;
        return route.fulfill({status:400,json:{code:20001,message:'本机授权失败，请重试',data:{}}});
      }
      data={binding_id:'grant',device_name:'PC'};
    }
    if (url.pathname.endsWith('/bind/start')) {
      assert.equal(url.searchParams.get('execution_location'),'local');
      assert.equal(url.searchParams.get('local_binding_id'),'grant');
      assert(!req.headers()['x-hugagent-target']);
      starts += 1;
      data={bind_id:'bind-'+starts,qrcode_img:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aAkoAAAAASUVORK5CYII='};
    }
    if (url.pathname.endsWith('/status')) {
      polls += 1;
      if (starts === 1) await new Promise(resolve => { releasePoll = resolve; });
      data=starts === 1 ? {status:'confirmed',channel_id:'bot'} : {status:'expired'};
    }
    return route.fulfill({json:{code:10000,message:'Success',data}});
  });
  await page.goto(origin);
  await page.getByRole('button',{name:'绑定机器人'}).click();
  await page.getByRole('radio',{name:'本机',exact:true}).check();
  await page.getByRole('combobox').first().click();
  await page.getByText('微信（扫码）',{exact:true}).click();
  await page.getByRole('button',{name:'扫码绑定'}).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('本机授权失败，请重试',{exact:true}).waitFor();
  assert.equal(await dialog.locator('.ant-spin').count(),0,'failed binding stops spinner');
  assert.equal(starts,0,'registration failure must not start QR login');
  await dialog.getByRole('button',{name:'重试'}).click();
  await dialog.getByRole('img',{name:'qrcode',exact:true}).waitFor();
  await page.waitForFunction(() => {
    const img=document.querySelector('img[alt="qrcode"]');return img?.complete && img.naturalWidth>0;
  });
  await page.waitForTimeout(4500);
  assert.equal(polls,1,'pending status request is serialized');
  await dialog.getByRole('button',{name:'Close',exact:true}).click();
  releasePoll();
  await dialog.waitFor({state:'hidden'});
  assert.equal(await dialog.isVisible(),false,'late status must not reopen closed dialog');
  assert.equal(await page.getByText('微信已绑定',{exact:true}).count(),0,'closed attempt must not report success');
  await page.getByRole('button',{name:'扫码绑定'}).click();
  await dialog.getByText('二维码已过期，请重试',{exact:true}).waitFor();
  assert.equal(await dialog.locator('.ant-spin').count(),0);
  await dialog.getByRole('button',{name:'重试'}).waitFor();
  await page.screenshot({path:resolve(output,'expired.png')});
  assert.deepEqual(errors,[]);
  console.log('PASS: rendered local binding failure, QR retry, long-poll serialization, close isolation and expiry');
} finally { await browser.close(); }
