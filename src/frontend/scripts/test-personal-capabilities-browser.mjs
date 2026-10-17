import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/personal-capabilities');
await mkdir(output, { recursive: true });
await build({ entryPoints: ['scripts/personal-capabilities-fixture.tsx'], outfile: resolve(output, 'fixture.js'),
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 800, height: 950 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const origin = 'http://pa-capabilities.test';
const plugin = { id: 'sample', name: '示例插件', skill_ids: ['plugin-skill'], mcp_ids: ['plugin-tool'] };
let config = { concurrency: 3, space_enabled: true, email_enabled: true,
  skill_ids: ['document'], mcp_ids: ['search'], plugins: [plugin],
  capability_selection: { skill_ids: ['document', 'unavailable-skill'], mcp_ids: ['search'], plugin_ids: ['sample'] } };
const agent = () => ({ id: 'pa-test', chat_id: 'pa-chat', status: 'ready', computer_status: 'ready', generation: 1, config });
const imported = { skill_ids: [], mcp_ids: [], plugins: [] };
let failImport = true;
let failLoad = true, failSave = false, saves = 0;
await page.route(origin + '/**', async route => {
  const path = new URL(route.request().url()).pathname;
  const json = data => route.fulfill({ json: { code: 0, data } });
  if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({
    contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css',
    body: await readFile(resolve(output, path.slice(1))) });
  if (path.endsWith('/personal-agent/capabilities')) {
    if (failLoad) return route.fulfill({ status: 503, json: { detail: 'unavailable' } });
    return json({ skill_ids: ['document', 'translate', ...imported.skill_ids], mcp_ids: ['search', 'fetch', ...imported.mcp_ids], plugins: [plugin, ...imported.plugins], plugin_component_ids: { skill_ids: ['plugin-skill'], mcp_ids: ['plugin-tool'] } });
  }
  if (path.endsWith('/marketplace/skills')) return json({ items: [{ slug: 'market-skill', display_name: '市场技能', summary: '用于测试导入', category: '办公', tags: [], required_secrets: [] }], categories: ['办公'] });
  if (path.endsWith('/marketplace/install')) {
    if (failImport) return route.fulfill({ status: 503, json: { detail: 'Import failed' } });
    imported.skill_ids.push('market-skill');
    failLoad = true;
    return json({ id: 'market-skill', action: 'installed' });
  }
  if (path.endsWith('/mcp-market/items')) return json({ items: [{ slug: 'market-tool', display_name: '市场工具', description: '测试连接器', tags: [], category: '办公', status: 'active', listing_notice: {}, auth_schema: [], auth_config: { methods: [] } }], categories: ['办公'] });
  if (path.endsWith('/mcp-market/install')) {
    imported.mcp_ids.push('market-tool');
    return json({ server_id: 'market-tool', action: 'installed' });
  }
  if (path.endsWith('/plugins')) return json({ items: [
    { slug: 'browser-automation', name: '不应显示的浏览器', skills_count: 1 },
    { slug: 'market-plugin', name: '市场插件', skills_count: 1, required_secrets: [] },
  ] });
  if (path.endsWith('/plugins/market-plugin/install')) {
    imported.plugins.push({ id: 'market-plugin@owner', name: '市场插件', skill_ids: ['plugin-skill'], mcp_ids: ['plugin-tool'] });
    return json({ install_id: 'market-plugin@owner' });
  }
  if (path.endsWith('/catalog')) return json({ skills: [{ id: 'document', name: '文档技能' }, { id: 'translate', name: '翻译技能' }],
    mcp: [{ id: 'search', name: '搜索工具' }, { id: 'fetch', name: '网页读取' }] });
  if (path.endsWith('/settings')) {
    saves++;
    if (failSave) return route.fulfill({ status: 409, json: { detail: 'Selected capability is unavailable or not allowed' } });
    const body = route.request().postDataJSON();
    config = { ...config, capability_selection: body, skill_ids: body.skill_ids.filter(id => ['document', 'translate', ...imported.skill_ids].includes(id)), mcp_ids: body.mcp_ids, plugins: body.plugin_ids.map(id => [plugin, ...imported.plugins].find(p => p.id === id)) };
    return json(agent());
  }
  if (path.endsWith('/personal-agent')) return json(agent());
  if (path.startsWith('/api')) return json({});
  return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
});
try {
  await page.goto(origin);
  await page.getByRole('button', { name: /重\s*试/ }).waitFor();
  failLoad = false;
  await page.getByRole('button', { name: /重\s*试/ }).click();
  const control = name => page.getByRole('combobox', { name: '绑定 ' + name });
  await control('工具').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.querySelector('[aria-label="绑定 工具"]').disabled);
  async function clear(name) {
    // Ant Design's input ancestor may be the selector; find the outer control.
    const outer = page.locator('.ant-select').filter({ has: control(name) });
    await outer.hover();
    await outer.locator('.ant-select-clear').click();
  }
  assert.equal(await page.getByTitle('plugin-skill （暂不可用）', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '从工具市场导入' }).waitFor();
  await page.getByRole('button', { name: '从技能市场导入' }).waitFor();
  await page.getByRole('button', { name: '从插件市场导入' }).waitFor();
  await control('工具').click();
  await page.getByTitle('网页读取', { exact: true }).click();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '保存能力绑定' }).click();
  await page.getByText('能力绑定已保存，下次执行任务时生效。', { exact: true }).waitFor();
  assert.deepEqual(config.capability_selection.skill_ids, ['document', 'unavailable-skill']);
  assert.deepEqual(config.skill_ids, ['document']);
  await clear('工具'); await clear('技能'); await clear('插件');
  await page.getByRole('button', { name: '保存能力绑定' }).click();
  await page.getByText('能力绑定已保存，下次执行任务时生效。', { exact: true }).waitFor();
  assert.deepEqual(config.skill_ids, []); assert.deepEqual(config.mcp_ids, []); assert.deepEqual(config.plugins, []);
  await page.getByRole('button', { name: '切换设置' }).click();
  await page.getByRole('button', { name: '切换设置' }).click();
  await control('工具').click();
  await page.getByTitle('网页读取', { exact: true }).click();
  await page.keyboard.press('Escape');
  await control('技能').click();
  await page.getByTitle('翻译技能', { exact: true }).click();
  await page.keyboard.press('Escape');
  await control('插件').click();
  await page.getByTitle('示例插件', { exact: true }).click();
  await page.keyboard.press('Escape');
  failSave = true;
  await page.getByRole('button', { name: '保存能力绑定' }).click();
  await page.getByText('Selected capability is unavailable or not allowed', { exact: true }).waitFor();
  assert.deepEqual(config.mcp_ids, []);
  failSave = false;
  await page.getByRole('button', { name: '保存能力绑定' }).click();
  await page.waitForFunction(() => document.querySelectorAll('.ant-select-selection-item').length === 3);
  assert.deepEqual(config.mcp_ids, ['fetch']); assert.deepEqual(config.skill_ids, ['translate']); assert.equal(config.plugins[0].id, 'sample');
  await page.getByRole('button', { name: '切换设置' }).click();
  await page.getByRole('button', { name: '切换设置' }).click();
  await page.getByTitle('翻译技能', { exact: true }).waitFor();
  await page.getByRole('button', { name: '从技能市场导入' }).click();
  const skillDialog = page.getByRole('dialog');
  await skillDialog.getByRole('button', { name: /^安\s*装$/ }).click();
  await page.getByText('Import failed', { exact: true }).waitFor();
  failImport = false;
  await skillDialog.getByRole('button', { name: /^安\s*装$/ }).click();
  await page.getByText('「市场技能」已安装', { exact: true }).waitFor();
  await skillDialog.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: /重\s*试/ }).waitFor();
  failLoad = false;
  await page.getByRole('button', { name: /重\s*试/ }).click();
  await page.getByTitle('market-skill', { exact: true }).waitFor();
  await page.getByTitle('翻译技能', { exact: true }).waitFor();
  await page.getByRole('button', { name: '从工具市场导入' }).click();
  await page.getByRole('dialog').getByRole('button', { name: /^安\s*装$/ }).click();
  await page.getByRole('dialog').filter({ hasText: '安装「市场工具」' }).getByRole('button', { name: /^安\s*装$/ }).click();
  await page.getByText('「市场工具」已安装到连接器', { exact: true }).waitFor();
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByTitle('market-tool', { exact: true }).waitFor();
  await page.getByRole('button', { name: '从插件市场导入' }).click();
  await page.getByText('市场插件', { exact: true }).waitFor();
  assert.equal(await page.getByText('不应显示的浏览器', { exact: true }).count(), 0);
  await page.getByRole('dialog').getByRole('button', { name: '安装并绑定' }).click();
  await page.getByText('「市场插件」已安装', { exact: true }).waitFor();
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByTitle('市场插件', { exact: true }).waitFor();
  assert.equal(await page.getByTitle('plugin-skill （暂不可用）', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '保存能力绑定' }).click();
  await page.waitForFunction(() => document.querySelector('button.ant-btn-primary')?.disabled);
  assert.deepEqual(config.capability_selection.skill_ids, ['translate', 'market-skill']);
  assert.deepEqual(config.capability_selection.mcp_ids, ['fetch', 'market-tool']);
  assert.deepEqual(config.capability_selection.plugin_ids, ['sample', 'market-plugin@owner']);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.screenshot({ path: resolve(output, 'settings-mobile.png'), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
  assert.equal(saves, 5);
  console.log('PASS: load retry, clear bindings, save, reopen, add bindings, save failure and retry, mobile layout, three marketplace imports, install failure and refresh retry.');
} catch (error) { console.log('ERRORS', errors, await page.locator('body').innerText()); throw error; } finally { await browser.close(); }
