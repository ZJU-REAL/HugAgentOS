import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/project-file-preview-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import ProjectRightRail from './src/components/projects/ProjectRightRail';
import { useProjectStore } from './src/stores/projectStore';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/projects.css';
import './src/styles/project-rail.css';
import './src/styles/myspace.css';
import './src/styles/mobile.css';
const files = Array.from({ length: 100 }, (_, index) => ({
  id: 'file-' + index, artifact_id: 'file-' + index,
  name: 'data/ontology/file-' + index + (index === 81 ? '.md' : '.jsonl'),
  folder_path: 'data/ontology', size_bytes: 100,
  mime_type: index === 81 ? 'text/markdown' : 'application/octet-stream',
}));
files.unshift({ id: 'run', artifact_id: 'run', name: 'runs/run.txt', folder_path: 'runs', size_bytes: 10 });
useProjectStore.setState({
  currentProjectId: 'preview-project',
  currentProject: { project_id: 'preview-project', name: 'Preview project', kind: 'personal',
    permission: 'admin', linked_folder_id: 'root', instructions: '' },
  projectFiles: files, capacityUsed: 10010, capacityLimit: 200 * 1024 * 1024,
  refreshInstructions: async () => {},
});
createRoot(document.getElementById('root')).render(
  <main style={{width: 420, margin: '24px auto'}}><ProjectRightRail /></main>
);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
});
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
});
const errors = [];
const fileRequests = [];
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  await page.route('https://project-preview.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
      return route.fulfill({ contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
        body: await readFile(resolve(output, url.pathname.slice(1))) });
    }
    if (url.pathname.startsWith('/api/files/')) {
      const id = url.pathname.split('/').pop();
      fileRequests.push({ id, inline: url.searchParams.get('inline') });
      return route.fulfill({ contentType: id === 'file-81' ? 'text/markdown' : 'application/x-ndjson',
        body: id === 'file-81' ? '# Markdown preview works\n\nExpected report content.'
          : '{"record":"first","file":"' + id + '"}\n{"record":"second"}\n' });
    }
    if (url.pathname === '/api/v1/automations') return route.fulfill({ json: { code: 0, data: [] } });
    if (url.pathname.startsWith('/api/')) {
      return route.fulfill({ json: { code: 0, data: { count: 0, items: [] } } });
    }
    return route.fulfill({ contentType: 'text/html', body:
      '<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial;background:var(--color-bg-container);color:var(--color-text)}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://project-preview.test/');
  const tree = page.getByRole('tree');
  const holder = tree.locator('.ant-tree-list-holder');
  const data = page.locator('.jx-projectRail-groupName[title="data"]');
  const ontology = page.locator('.jx-projectRail-groupName[title="ontology"]');
  await data.click();
  await ontology.click();
  await page.waitForTimeout(400);
  const beforeRows = await page.locator('.jx-projectRail-fileItem').count();
  assert.ok(beforeRows < 30, 'The 100-file inventory stays virtualized');

  async function scrollToFiles() {
    // Real wheel input, followed by a click from outside the tree, reproduces the
    // focus-to-first-row bug instead of invoking the preview callback directly.
    await page.getByText('项目文件', { exact: true }).click();
    await holder.hover();
    await page.mouse.wheel(0, -10000);
    await page.waitForFunction(() => document.querySelector('.ant-tree-list-holder').scrollTop === 0);
    await page.mouse.wheel(0, 3000);
    await page.waitForTimeout(300);
    await page.getByTitle('data/ontology/file-80.jsonl', { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('.ant-tree-list-holder').scrollTop > 2000);
  }
  async function assertPreview(index, action = 'filename') {
    await scrollToFiles();
    const scrollBefore = await holder.evaluate(el => el.scrollTop);
    const name = index === 81 ? 'file-81.md' : 'file-' + index + '.jsonl';
    const button = page.getByTitle('data/ontology/' + name, { exact: true });
    if (action === 'eye') {
      const row = page.locator('.jx-projectRail-fileItem').filter({ has: button });
      await row.hover();
      await row.getByRole('button', { name: '预览', exact: true }).click();
    } else if (action === 'Enter' || action === 'Space') {
      await button.focus();
      assert.equal(await holder.evaluate(el => el.scrollTop), scrollBefore,
        'Focusing a file must not scroll to the first tree row');
      await button.press(action);
    } else {
      await button.click();
    }
    const dialog = page.getByRole('dialog', { name, exact: true });
    await dialog.waitFor({ timeout: 2500 });
    if (index === 81) {
      await dialog.getByRole('heading', { name: 'Markdown preview works' }).waitFor();
    } else {
      await dialog.getByText(/"record":"second"/).waitFor();
    }
    assert.ok(fileRequests.some(request => request.id === 'file-' + index && request.inline === 'true'),
      'Preview reads the intended file through its authenticated inline URL');
    assert.equal(await holder.evaluate(el => el.scrollTop), scrollBefore,
      'Opening preview preserves file-list position');
    if (action === 'filename') await page.screenshot({ path: resolve(output, 'jsonl-preview.png') });
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(await holder.evaluate(el => el.scrollTop), scrollBefore,
      'Closing preview preserves file-list position');
  }

  await assertPreview(80);
  await assertPreview(80, 'eye');
  await assertPreview(80, 'Enter');
  await assertPreview(80, 'Space');
  await assertPreview(81);
  // Delete controls share the same event boundary; entering the confirmation
  // must preserve list position. Cancel without mutating any file.
  await scrollToFiles();
  const deleteScroll = await holder.evaluate(el => el.scrollTop);
  const row = page.locator('.jx-projectRail-fileItem')
    .filter({ has: page.getByTitle('data/ontology/file-80.jsonl', { exact: true }) });
  await row.hover();
  await row.getByRole('button', { name: '删除', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '删除文件？' });
  await confirmation.waitFor();
  assert.equal(await holder.evaluate(el => el.scrollTop), deleteScroll);
  await confirmation.getByRole('button', { name: /取.*消/ }).click();
  await confirmation.waitFor({ state: 'hidden' });

  // Tree-level navigation is still available when the tree itself has focus.
  await tree.focus();
  await tree.press('Home');
  assert.equal(await page.locator('.jx-projectRail-groupName[title="runs"]')
    .locator('xpath=ancestor::*[@role="treeitem"][1]').getAttribute('aria-expanded'), 'false',
    'File keyboard input must not expand the first folder');
  await tree.press('Enter');
  await page.locator('.jx-projectRail-groupName[title="runs"]').locator('xpath=ancestor::*[@role="treeitem"][1]')
    .waitFor();
  assert.equal(await page.locator('.jx-projectRail-groupName[title="runs"]')
    .locator('xpath=ancestor::*[@role="treeitem"][1]').getAttribute('aria-expanded'), 'true');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ checks: ['scrolled JSONL filename click', 'eye click', 'Enter', 'Space',
    'Markdown content', 'close/reopen scroll preservation', 'delete/cancel', 'tree keyboard navigation',
    'virtualization', 'no browser errors'], fileRequests: fileRequests.length, mountedRows: beforeRows }));
} catch (error) {
  const page = browser.contexts()[0]?.pages()[0];
  if (page) await page.screenshot({ path: resolve(output, 'failure.png') });
  throw error;
} finally {
  await browser.close();
}
