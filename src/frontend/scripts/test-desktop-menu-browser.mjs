import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const out = resolve('node_modules/.tmp/desktop-menu-browser');
await mkdir(out, { recursive: true });
const update = await readFile('../../desktop/src-tauri/src/update.rs', 'utf8');
const constant = (source, name, hashes = '##') => {
  const start = source.indexOf('const ' + name + ': &str = r' + hashes + '"');
  assert.ok(start >= 0, name);
  const valueStart = source.indexOf('"', start) + 1;
  return source.slice(valueStart, source.indexOf('"' + hashes + ';', valueStart));
};
const chrome = '../../desktop/shared/chrome/';
const menu = await readFile(chrome + 'menu.html', 'utf8');
const js = await readFile(chrome + 'menu.js', 'utf8');
const css = await readFile(chrome + 'menu.css', 'utf8');
const progress = constant(update, 'PROGRESS_HTML', '#');
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { DesktopUpdateEntry } from './src/desktop/DesktopUpdateEntry';
import { useDesktopUpdateStatus } from './src/desktop/useDesktopUpdateStatus';
import './src/styles/variables.css';
import './src/styles/sidebar.css';
function Fixture() {
 const status = useDesktopUpdateStatus();
 window.fixtureRenders=(window.fixtureRenders||0)+1;
 return <DesktopUpdateEntry status={status} className="jx-helpBtn"><button>帮助</button></DesktopUpdateEntry>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`, resolveDir: process.cwd(), loader: 'tsx' },
 outfile: resolve(out, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
 define: { 'import.meta.env': '{}' } });
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
 const page = await browser.newPage();
 const errors = [], actions = [], requests = [];
 await page.addInitScript(() => {
   window.eventStreams=[];
   window.EventSource=class {
     constructor(url) { this.url=url; window.eventStreams.push(this); }
     close() { this.closed=true; }
   };
 });
 const pushStatus = async (status) => page.evaluate((update) => {
   window.eventStreams.forEach(stream => stream.onmessage?.({data:JSON.stringify({update})}));
 }, status);
 page.on('pageerror', e => errors.push(e.message));
 await page.route('http://desktop.test/**', async route => {
  const url = new URL(route.request().url());
  requests.push(url.pathname);
  if (url.pathname === '/__desktop/update/status') return route.fulfill({json:{available_version:null,busy:false}});
  if (url.pathname === '/__desktop/menu') { actions.push(url.searchParams.get('action')); return route.abort(); }
  if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') return route.fulfill({
   contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
   body: await readFile(resolve(out, url.pathname.slice(1))),
  });
  return route.fulfill({ contentType:'text/html', body:
   '<html lang="zh-CN"><head><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"></head><body>' +
   '<script>window.__HG_DESKTOP__={provision_mode:"dual"};localStorage.setItem("jx_lang","zh-CN");</script>' +
   '<style>' + css + '</style><header id="hugagent-titlebar">' + menu + '</header><script>' + js + '</script>' +
   '<div id="root" style="margin:100px"></div><script type="module" src="/fixture.js"></script></body></html>',
  });
 });
 await page.goto('http://desktop.test/');
 await page.locator('#root').getByRole('button', {name:'帮助', exact:true}).waitFor();
 await page.locator('[data-menu="file"] > button').click();
 const file = page.locator('#hugagent-file-menu');
 assert.deepEqual(await file.getByRole('menuitem').allTextContents(), ['新建窗口Ctrl+Shift+N', '新建对话Ctrl+N', '打开文件夹…', '退出']);
 await file.getByRole('menuitem', {name:'打开文件夹…'}).click();
 await page.waitForTimeout(100);
 assert.deepEqual(actions, ['open_folder']);
 await page.goto('http://desktop.test/');
 await page.locator('#root').getByRole('button', {name:'帮助', exact:true}).waitFor();
 await pushStatus({available_version:'2.0.0', busy:false});
 await page.getByRole('button', {name:'下载更新 · 2.0.0'}).waitFor();
 await page.getByRole('button', {name:'下载更新 · 2.0.0'}).click();
 await page.waitForTimeout(100);
 assert.deepEqual(actions, ['open_folder','check_update']);
 await page.goto('http://desktop.test/');
 await page.locator('#root').getByRole('button', {name:'帮助', exact:true}).waitFor();
 await pushStatus({available_version:'2.0.0', busy:true});
 await page.waitForFunction(() => document.querySelector('[aria-label="下载更新 · 2.0.0"]')?.disabled);
 await pushStatus({available_version:null, busy:false});
 await page.locator('#root').getByRole('button', {name:'帮助', exact:true}).waitFor();
 const renders = await page.evaluate(() => window.fixtureRenders);
 await pushStatus({available_version:null, busy:false});
 await page.waitForTimeout(50);
 assert.equal(await page.evaluate(() => window.fixtureRenders), renders, 'unchanged status must not render');
 assert.equal(await page.evaluate(() => window.eventStreams.length), 1, 'reuse one desktop event stream');
 assert.ok(!requests.includes('/__desktop/update/status'), 'no local polling');
 await page.setContent(progress);
 await page.evaluate(() => window.__set(25, 10, 40));
 assert.equal(await page.locator('#p').textContent(), '25%');
 assert.equal(await page.locator('#s').textContent(), '10.0 / 40.0 MB');
 await page.evaluate(() => window.__set(-1, 10, 0));
 assert.match(await page.locator('#f').getAttribute('class'), /indet/);
 await page.evaluate(() => window.__done());
 assert.equal(await page.locator('#t').textContent(), '下载完成，正在安装…');
 assert.deepEqual(errors, []);
 console.log('desktop menu/update browser checks passed');
} finally { await browser.close(); }
