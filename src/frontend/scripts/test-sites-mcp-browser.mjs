import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/sites-mcp-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { SitesPanel } from './src/components/sites/SitesPanel';
import { ApplicationDataPanel } from './src/components/sites/ApplicationDataPanel';
import 'antd/dist/reset.css';
import './src/styles/variables.css';
import './src/styles/catalog.css';
import { useChatStore } from './src/stores/chatStore';
import { useCatalogStore } from './src/stores';
function SessionStatus(){ const chat=useChatStore(s=>s.store.chats[s.currentChatId]); const panel=useCatalogStore(s=>s.panel); return <output id="editor-session">{JSON.stringify({panel, projectId:chat?.projectId,title:chat?.title,runTarget:chat?.runTarget,id:chat?.id})}</output> }
createRoot(document.getElementById('root')).render(window.location.pathname === '/unscoped' ? <ApplicationDataPanel initialTab="mcp" /> : <><SitesPanel/><SessionStatus/></>);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'),
 bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
 external: ['/loader.gif', '/loader-done.png'],
 loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
const origin = 'http://127.0.0.1:18479';
const appId = 'a'.repeat(32);
const site = { site_id: 'site-one', title: '星河站点', slug: 'star', url: '/site/star/',
 visibility: 'private', current_version: 2, file_count: 3, total_size_bytes: 1200,
 view_count: 5, editable: true, can_manage: true, origin: 'cloud', project_id: 'project-one' };
const columns = [{ name: 'name', type: 'text' }, { name: 'region', type: 'text' },
 { name: 'limit', type: 'integer' }, { name: 'model_id', type: 'text' }, { name: 'class', type: 'text' }];
const app = { id: appId, title: '企业查询 MCP', site_id: null, mcp_enabled: true, mcp_version: 3,
 tables: { enterprises: { name: 'enterprises', columns, public_insert: false } },
 tools: [
 { name: 'query_enterprises', description: '查询企业名称', table: 'enterprises', fields: ['name'], filters: ['region'] },
 { name: 'query_regions', description: '查询地区', table: 'enterprises', fields: ['region'], filters: [] },
 ] };
const retained = { ...app, id: 'c'.repeat(32), title: '保留的应用数据库', site_id: 'deleted-site',
 mcp_enabled: false, mcp_version: 0, tools: [] };
const linkedData = { ...retained, id: 'd'.repeat(32), title: '站点关联数据库', site_id: 'site-one' };
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
try {
 const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
 page.setDefaultTimeout(5000);
 await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
 const errors = [], writes = [], requests = [];
 let failList = false, holdPublish = false, releasePublish;
 page.on('pageerror', error => errors.push(error.message));
 await page.route(origin + '/**', async route => {
  const request = route.request(), url = new URL(request.url());
  requests.push(url.pathname);
  if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
   return route.fulfill({ contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
    body: await readFile(resolve(output, url.pathname.slice(1))) });
  }
  if (url.pathname.startsWith('/api/')) {
   let data = {};
   if (url.pathname === '/api/v1/plugins/installed') data = { items: [{slug:'sites', enabled:true, install_id:'sites-test', name:'Sites'}] };
   if (url.pathname.endsWith('/editor')) data = { app_id: appId, project_id:'amcp-project', project_name:'企业 MCP 项目', chat_id:null };
   if (url.pathname === '/api/v1/sites') data = { items: [site], total: 1 };
   if (url.pathname === '/api/v1/applications') {
    if (failList) { failList = false; return route.fulfill({ status: 503, json: { detail: 'Hosting unavailable' } }); }
    data = { items: [app, retained, linkedData], available: true };
   }
   if (url.pathname.endsWith('/records')) data = { items: [], total: 0 };
   if (url.pathname.endsWith('/mcp') && request.method() !== 'GET') {
    writes.push({ method: request.method(), body: request.postDataJSON() });
    if (request.method() === 'POST') {
     if (holdPublish) await new Promise(resolve => { releasePublish = resolve; });
     app.tools = request.postDataJSON().tools; app.mcp_enabled = true; app.mcp_version++;
     data = { url: '/applications-mcp/' + appId, token: 'test-only-credential', version: app.mcp_version };
    } else { app.mcp_enabled = false; data = { revoked: true }; }
   }
   return route.fulfill({ json: { code: 0, data } });
  }
  return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"><style>body{margin:0;background:var(--color-bg-layout);color:var(--color-text);font:14px Arial}#root{padding:24px}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
 });
 await page.goto(origin);
 const siteCard = page.locator('.jx-sites-card').filter({ hasText: '星河站点' });
 const mcpCard = page.locator('.jx-sites-card').filter({ hasText: '企业查询 MCP' });
 await siteCard.waitFor();
 await mcpCard.waitFor();
 assert.equal(await page.locator('.jx-sites-list .jx-sites-card').count(), 3);
 assert.equal(await page.locator('.jx-sites-card').filter({ hasText: '站点关联数据库' }).count(), 0);
 await page.getByPlaceholder('搜索站点或 MCP 服务').fill('企业');
 assert.equal(await page.locator('.jx-sites-card').count(), 1);
 await page.getByPlaceholder('搜索站点或 MCP 服务').fill('');
 await page.screenshot({ path: resolve(output, 'cards-desktop.png'), fullPage: true, animations: 'disabled' });
 await mcpCard.getByRole('button', { name: /管理/ }).click();
 await page.getByRole('button', { name: '发布或更新 MCP', exact: true }).click();
 const editor = page.getByRole('dialog', { name: '定义 MCP 工具', exact: true });
 await editor.waitFor();
 assert.equal(await editor.getByLabel('工具名称', { exact: true }).count(), 2);
 assert.equal(await editor.getByLabel('工具名称', { exact: true }).nth(0).inputValue(), 'query_enterprises');
 assert.equal(await editor.getByLabel('工具名称', { exact: true }).nth(1).inputValue(), 'query_regions');
 await editor.getByLabel('工具说明', { exact: true }).nth(0).fill('按地区查询企业');
 await editor.getByRole('button', { name: /添加工具/ }).click();
 assert.equal(await editor.getByLabel('工具名称', { exact: true }).count(), 3);
 const defaultFields = await editor.getByLabel('返回字段', { exact: true }).nth(2).locator('..').locator('..').textContent();
 assert.ok(defaultFields.includes('name') && defaultFields.includes('region'));
 assert.ok(!defaultFields.includes('limit') && !defaultFields.includes('model_id') && !defaultFields.includes('class'));
 await editor.getByLabel('工具名称', { exact: true }).nth(2).fill('query_regions');
 await editor.getByLabel('工具说明', { exact: true }).nth(2).fill('重复工具');
 await editor.getByRole('button', { name: /保\s*存/ }).click();
 await editor.getByText('工具名称不能重复', { exact: true }).waitFor();
 assert.equal(writes.length, 0);
 await editor.getByRole('button', { name: '删除工具', exact: true }).nth(2).click();
 assert.equal(await editor.getByLabel('工具名称', { exact: true }).count(), 2);
 await editor.getByRole('button', { name: /保\s*存/ }).click();
 await page.getByRole('dialog', { name: '保存 MCP 访问凭据' }).waitFor();
 assert.equal(writes.length, 1);
 assert.equal(writes[0].method, 'POST');
 assert.deepEqual(writes[0].body.tools, [
  { name: 'query_enterprises', description: '按地区查询企业', table: 'enterprises', fields: ['name'], filters: ['region'] },
  { name: 'query_regions', description: '查询地区', table: 'enterprises', fields: ['region'], filters: [] },
 ]);
 const credential = page.getByRole('dialog', { name: '保存 MCP 访问凭据' });
 await credential.getByRole('button', { name: /确\s*认/ }).click();
 const management = page.getByRole('dialog', { name: 'MCP 服务 — 企业查询 MCP', exact: true });
 await management.getByRole('button', { name: '撤销 MCP 访问', exact: true }).click();
 await page.getByText('撤销后所有客户端将无法访问此 MCP，确定撤销？', { exact: true }).waitFor();
 assert.equal(writes.length, 1, 'revocation must wait for confirmation');
 await page.getByRole('button', { name: /取\s*消/ }).click();
 assert.equal(writes.length, 1);
 await management.getByRole('button', { name: '撤销 MCP 访问', exact: true }).click();
 await page.getByRole('button', { name: /确\s*认/ }).click();
 await mcpCard.getByText('未发布', { exact: true }).waitFor();
 assert.equal(writes[1].method, 'DELETE');
 await management.getByRole('button', { name: 'Close', exact: true }).click();
 await mcpCard.getByRole('button', { name: /打\s*开/ }).click();
 const access = page.getByRole('dialog', { name: 'MCP 服务 — 企业查询 MCP', exact: true });
 await access.getByText('该 MCP 尚未发布或已撤销，请在管理中重新发布', { exact: true }).waitFor();
 assert.equal(await access.locator('input').inputValue(), origin + '/applications-mcp/' + appId);
 assert.equal(requests.some(path => path.startsWith('/applications-mcp/')), false, 'Open must not navigate to a protocol endpoint');
 await access.getByRole('button', { name: 'Close', exact: true }).click();
 await page.setViewportSize({ width: 390, height: 844 });
 await mcpCard.getByRole('button', { name: /管理/ }).waitFor();
 assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
 const buttons = await mcpCard.getByRole('button').all();
 for (const button of buttons.slice(1)) assert.ok((await button.boundingBox()).height >= 44);
 await page.screenshot({ path: resolve(output, 'cards-mobile.png'), fullPage: true, animations: 'disabled' });
 const retainedCard = page.locator('.jx-sites-card').filter({ hasText: '保留的应用数据库' });
 await retainedCard.getByRole('button', { name: /管理/ }).click();
 const database = page.getByRole('dialog', { name: '应用数据库 — 保留的应用数据库', exact: true });
 await database.getByRole('button', { name: '定义数据表', exact: true }).waitFor();
 assert.equal(await database.getByRole('combobox', { name: '选择应用', exact: true }).count(), 0);
 await database.getByRole('button', { name: 'Close', exact: true }).click();
 await page.locator('.ant-message').waitFor({ state: 'hidden' });
 await page.screenshot({ path: resolve(output, 'cards-mobile.png'), fullPage: true, animations: 'disabled' });
 await mcpCard.getByRole('button', { name: /打\s*开/ }).click();
 const mobileAccess = page.getByRole('dialog', { name: 'MCP 服务 — 企业查询 MCP', exact: true });
 assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
 await page.screenshot({ path: resolve(output, 'connection-mobile.png'), fullPage: true, animations: 'disabled' });
 await mobileAccess.getByRole('button', { name: 'Close', exact: true }).click();
 failList = true;
 await page.reload();
 await page.getByRole('alert').waitFor();
 await page.getByRole('button', { name: /重\s*试/ }).click();
 await mcpCard.waitFor();
 await page.getByPlaceholder('搜索站点或 MCP 服务').fill('按地区');
 assert.equal(await page.locator('.jx-sites-card').count(), 1);
 await page.getByPlaceholder('搜索站点或 MCP 服务').fill('');
 await mcpCard.getByRole('button', { name: /编辑/ }).click();
 await page.waitForFunction(() => document.getElementById('editor-session').textContent.includes('amcp-project'));
 const firstSession = JSON.parse(await page.locator('#editor-session').textContent());
 assert.equal(firstSession.projectId, 'amcp-project');
 assert.equal(firstSession.runTarget, 'cloud');
 assert.equal(firstSession.title, '编辑 MCP：企业查询 MCP');
 await mcpCard.getByRole('button', { name: /编辑/ }).click();
 await page.waitForFunction(() => !document.querySelector('.jx-sites-card .ant-btn-loading'));
 assert.equal(JSON.parse(await page.locator('#editor-session').textContent()).id, firstSession.id);
 assert.equal(await page.getByRole('dialog').count(), 0, 'Edit must navigate to a project conversation');
 await page.getByPlaceholder('搜索站点或 MCP 服务').fill('不存在的资源');
 await page.getByText('没有匹配的站点或 MCP 服务', { exact: true }).waitFor();
 await page.goto(origin + '/unscoped');
 const chooser = page.getByRole('combobox', { name: '选择应用', exact: true });
 await chooser.waitFor();
 await page.getByRole('button', { name: '发布或更新 MCP', exact: true }).click();
 const delayedEditor = page.getByRole('dialog', { name: '定义 MCP 工具', exact: true });
 await delayedEditor.waitFor();
 holdPublish = true;
 await delayedEditor.getByRole('button', { name: /保\s*存/ }).click();
 await page.waitForFunction(() => document.querySelector('[aria-label="选择应用"]').disabled);
 assert.ok(await chooser.isDisabled(), 'publishing must lock the application selection');
 assert.ok(await delayedEditor.getByRole('button', { name: /取\s*消/ }).isDisabled());
 assert.ok(releasePublish);
 releasePublish(); holdPublish = false;
 await page.getByRole('dialog', { name: '保存 MCP 访问凭据' }).waitFor();
 assert.deepEqual(errors, []);
 console.log('sites/MCP browser checks passed: shared cards/search, preserved tools, add/delete validation, publish/revoke, connection details, retained databases, retry and mobile layout');
} finally { await browser.close(); }
