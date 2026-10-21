import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const scenarios = ['slow', 'slow-icons', 'long', 'failure', 'empty', 'switch', 'stale-action', 'cache-expiry'];
if (process.argv[2] && ![...scenarios, 'http'].includes(process.argv[2])) {
  throw new Error('Unknown file-open test scenario: ' + process.argv[2]);
}
const output = resolve('node_modules/.tmp/file-open-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import { ConfigProvider } from 'antd';
import { FileOpenControl } from './src/components/file/FileOpenControl';
import { DeliveredFiles } from './src/components/chat/DeliveredFiles';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import './src/styles/variables.css';
import './src/styles/common.css';
useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'dual' });
function Fixture() {
  const [id, setId] = useState('report');
  const [phase, setPhase] = useState();
  const standalone = new URLSearchParams(location.search).has('control');
  return <ConfigProvider theme={{token: {motion: false}}}>
    <button onClick={() => setId('second')}>Switch file</button>
    <main style={{margin: '24px 16px'}}>
      {standalone ? <section data-native-control>
        <FileOpenControl file={{file_id: id, name: '测试文档.md', origin: 'local'}} onPhase={setPhase} />
        <span role="status">{phase?.message}</span>
      </section> : <DeliveredFiles artifacts={[{file_id: id, name: '测试文档.md', origin: 'local'}]} />}
    </main>
  </ConfigProvider>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: {'import.meta.env': '{}'}, external: ['/loader.gif', '/loader-done.png'],
  loader: {'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'},
});
const apps = Array.from({length: 60}, (_, i) => ({id: 'app-' + i, name: 'Editor ' + i, icon: null}));
const browser = await chromium.launch({headless: true});
const metrics = [];
try {
  for (const scenario of (process.argv[2] ? [process.argv[2]] : scenarios)) {
    const page = await browser.newPage({viewport: {width: 654, height: 540}});
    await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const opened = [], queries = [], iconQueries = [];
    let releaseApps;
    const gate = new Promise(resolve => {releaseApps = resolve;});
    await page.route('http://file-open.test/**', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
        return route.fulfill({contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
          body: await readFile(resolve(output, url.pathname.slice(1)))});
      }
      if (scenario === 'http' && url.pathname.startsWith('/api/')) {
        const response = await route.fetch({url: process.env.FILE_OPEN_E2E_BASE + url.pathname + url.search});
        if (request.method() === 'POST' && response.ok()) opened.push(request.postDataJSON());
        return route.fulfill({response});
      }
      if (url.pathname.endsWith('/applications')) {
        if (url.searchParams.get('include_icons') === 'true') iconQueries.push(url.pathname);
        else queries.push(url.pathname);
        if (scenario === 'slow' || (['slow-icons', 'cache-expiry'].includes(scenario) && url.searchParams.get('include_icons') === 'true') || (scenario === 'switch' && url.pathname.includes('/report/'))) await gate;
        if (scenario === 'failure' && queries.length === 1) return route.fulfill({status: 503, json: {detail: 'Unavailable'}});
        return route.fulfill({json: {applications: scenario === 'empty' ? [] : apps, default_id: scenario === 'empty' ? null : 'app-0'}});
      }
      if (url.pathname.endsWith('/open')) {
        if (scenario === 'stale-action' && url.pathname.includes('/report/')) await gate;
        assert.equal(url.searchParams.get('hg_target'), 'local', 'native actions retain local routing');
        opened.push({path: url.pathname, ...request.postDataJSON()});
        return route.fulfill({json: {ok: true}});
      }
      if (url.pathname.endsWith('.svg')) return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg"/>'});
      return route.fulfill({contentType: 'text/html', body:
        '<link rel="stylesheet" href="/fixture.css"><style>body{margin:0;font:14px Arial}</style><div id="root"></div><script type="module" src="/fixture.js"></script>'});
    });
    await page.goto('http://file-open.test/' + (scenario === 'stale-action' ? '?control=1' : ''));
    const card = page.locator(scenario === 'stale-action' ? '[data-native-control]' : '[data-presented-file]');
    const main = card.locator('.hg-delivery-open');
    await main.waitFor();
    if (scenario === 'stale-action') {
      await main.click();
      await card.getByRole('status').filter({hasText:'正在打开文件…'}).waitFor();
      await page.getByRole('button', {name:'Switch file'}).click();
      await page.waitForFunction(() => document.querySelector('[data-native-control] [role="status"]').textContent === '', undefined, {timeout:2000});
      assert.equal(await card.getByRole('status').textContent(), '',
        'Changing the target clears the previous action status in the same component instance');
      const oldResponse = page.waitForResponse(r => r.url().includes('/report/open'));
      releaseApps();
      await oldResponse;
      await main.click();
      await card.getByRole('status').filter({hasText:'已打开文件'}).waitFor();
      assert.equal(opened.at(-1).path, '/api/files/second/open');
    } else if (scenario === 'cache-expiry') {
      const iconRequest = page.waitForRequest(r => r.url().includes('include_icons=true'));
      await card.getByRole('button', {name:'打开选项',exact:true}).click();
      await iconRequest;
      await page.keyboard.press('Escape');
      await page.evaluate(() => {const now = Date.now; Date.now = () => now() + 31000;});
      const refreshed = page.waitForRequest(r => r.url().includes('include_icons=false'), {timeout:2000});
      await card.getByRole('button', {name:'打开选项',exact:true}).click();
      await refreshed;
      assert.equal(queries.length, 2, 'Expired application names refresh despite pending optional icons');
    } else if (scenario === 'http') {
      await main.click();
      await card.getByRole('status').filter({hasText: '已打开文件'}).waitFor();
      await card.getByRole('button', {name: '打开所在文件夹', exact: true}).click();
      await card.getByRole('status').filter({hasText: '已显示文件'}).waitFor();
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      const menu = page.getByRole('menu');
      await page.getByRole('menuitem', {name: 'Editor 59', exact: true}).waitFor({state:'attached'});
      await menu.hover();
      await page.mouse.wheel(0, 10000);
      await page.getByRole('menuitem', {name: 'Editor 59', exact: true}).click();
      await card.getByRole('status').filter({hasText: '已打开文件'}).waitFor();
      assert.deepEqual(opened, [{action:'open'}, {action:'reveal'}, {action:'open',application:'viewer-59.desktop'}]);
      for (const [id, body, status, user] of [
        ['private', {action:'open'}, 403, 'owner'],
        ['report', {action:'open'}, 401, 'anonymous'],
        ['missing', {action:'open'}, 404, 'owner'],
        ['escape', {action:'open'}, 404, 'owner'],
        ['report', {action:'open',application:'/untrusted/program'}, 400, 'owner'],
      ]) {
        const result = await fetch(process.env.FILE_OPEN_E2E_BASE + '/api/files/' + id + '/open', {
          method:'POST', headers:{'Content-Type':'application/json','x-fixture-user':user}, body:JSON.stringify(body),
        });
        assert.equal(result.status, status, id + ' must be rejected before any OS action');
      }
      const denied = await fetch(process.env.FILE_OPEN_E2E_BASE + '/api/files/private/applications?include_icons=false');
      assert.equal(denied.status, 403);
      await page.screenshot({path:resolve(output,'http-end-to-end.png')});
    } else if (scenario === 'slow') {
      assert.equal(queries.length, 0, 'rendering does not enumerate applications');
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await page.getByRole('menuitem', {name: '正在读取关联应用…'}).waitFor();
      assert.equal(queries.length, 1, 'discovery is now pending');
      const start = performance.now();
      assert.equal(await main.isEnabled(), true, 'default open must not wait for application discovery');
      await main.click();
      await page.waitForFunction(() => document.querySelector('[role="status"]')?.textContent.includes('已打开文件'));
      const clickToSuccessMs = Math.round(performance.now() - start);
      assert.deepEqual(opened[0], {path: '/api/files/report/open', action: 'open'});
      const folder = card.getByRole('button', {name: '打开所在文件夹', exact: true});
      assert.equal(await folder.isEnabled(), true);
      await folder.click();
      assert.equal(opened.at(-1).action, 'reveal');
      assert.equal(queries.length, 1, 'default actions neither wait for nor restart discovery');
      metrics.push({scenario, clickToSuccessMs});
    } else if (scenario === 'slow-icons') {
      const iconRequest = page.waitForRequest(r => r.url().includes('include_icons=true'));
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await iconRequest;
      await page.getByRole('menuitem', {name: 'Editor 1', exact: true}).click();
      await card.getByRole('status').filter({hasText: '已打开文件'}).waitFor();
      assert.equal(opened.at(-1).application, 'app-1');
      await card.getByRole('button', {name: '打开所在文件夹', exact: true}).click();
      await card.getByRole('status').filter({hasText: '已显示文件'}).waitFor();
      await main.click();
      await card.getByRole('status').filter({hasText: '已打开文件'}).waitFor();
      assert.deepEqual(opened.at(-1), {path:'/api/files/report/open',action:'open'});
    } else if (scenario === 'long') {
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await page.getByRole('menuitem', {name: 'Editor 59', exact: true}).waitFor({state: 'attached'});
      for (const viewport of [{width:654,height:540},{width:320,height:420},{width:844,height:390}]) {
        await page.setViewportSize(viewport);
        const menu = page.getByRole('menu');
        const box = await menu.boundingBox();
        assert.ok(box.y >= 0 && box.y + box.height <= viewport.height && box.x >= 0 && box.x + box.width <= viewport.width + 1,
          'application menu stays in the viewport: ' + JSON.stringify({viewport, box}));
        assert.equal(await menu.evaluate(el => el.scrollHeight > el.clientHeight), true, 'long menu scrolls');
        await menu.hover();
        await page.mouse.wheel(0, 1200);
        await page.waitForFunction(() => document.querySelector('[role="menu"]').scrollTop > 0);
        await menu.evaluate(el => {el.scrollTop = 0;});
      }
      await page.screenshot({path: resolve(output, 'scrollable-menu.png')});
      const menu = page.getByRole('menu');
      await menu.focus();
      await page.keyboard.press('End');
      await page.waitForFunction(() => document.querySelector('[role="menu"]').scrollTop > 0);
      const last = await page.getByRole('menuitem', {name:'Editor 59',exact:true}).boundingBox();
      const bounds = await menu.boundingBox();
      assert.ok(last.y >= bounds.y && last.y + last.height <= bounds.y + bounds.height + 1,
        'keyboard navigation reveals the final application');
      await page.keyboard.press('Home');
      await page.waitForFunction(() => document.querySelector('[role="menu"]').scrollTop === 0);
      await menu.hover();
      await page.mouse.wheel(0, 10000);
      await menu.focus();
      await page.keyboard.press('End');
      await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'menuitem' && document.activeElement.textContent.includes('Editor 59'));
      await page.keyboard.press('Enter');
      await card.getByRole('status').filter({hasText:'已打开文件'}).waitFor();
      assert.equal(opened.at(-1).application, 'app-59');
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await page.getByRole('menuitem', {name: 'Editor 0（默认）', exact: true}).waitFor();
      assert.equal(queries.length, 1, 'reopening the same menu reuses its result');
      await page.keyboard.press('Escape');
      await card.getByRole('button', {name: '打开所在文件夹', exact: true}).click();
      assert.equal(opened.at(-1).action, 'reveal');
    } else if (scenario === 'failure') {
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await page.getByRole('menuitem', {name: '重新读取关联应用'}).click();
      await page.getByRole('menuitem', {name: 'Editor 1', exact: true}).waitFor();
      assert.equal(queries.length, 2);
    } else if (scenario === 'empty') {
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await page.getByRole('menuitem', {name:'正在读取关联应用…'}).waitFor({state:'hidden'});
      assert.equal(iconQueries.length, 0, 'Empty application choices do not trigger icon work');
      assert.equal(await page.locator('.ant-dropdown-menu-item-divider').count(), 0,
        'A folder-only menu does not contain a trailing divider');
      await page.getByRole('menuitem', {name: '打开所在文件夹', exact: true}).click();
      assert.equal(opened.at(-1).action, 'reveal');
    } else if (scenario === 'switch') {
      await card.getByRole('button', {name: '打开选项', exact: true}).click();
      await page.getByRole('button', {name: 'Switch file'}).click();
      releaseApps();
      await main.click();
      assert.equal(opened.at(-1).path, '/api/files/second/open', 'stale discovery cannot change the target');
    }
    releaseApps();
    assert.deepEqual(errors, []);
    await page.close();
    console.log('PASS', scenario);
  }
  console.log(JSON.stringify(metrics));
} finally {
  await browser.close();
}
