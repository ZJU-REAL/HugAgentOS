import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/api-key-clipboard-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { resolveDir: process.cwd(), loader: 'tsx', contents: `
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiKeyManager } from './src/components/common/ApiKeyManager';
import { CopyButton } from './src/components/common/CopyButton';
const key = { id: 'test-key', name: 'Fixture', key_prefix: 'fixture', enabled: true, revealable: true };
const api = {
 list: async () => [key], create: async () => key, toggle: async () => key,
 revoke: async () => {}, reveal: async () => 'test-only-revealed-key',
};
function Fixture() {
 const [open, setOpen] = useState(true);
 return <><button onClick={() => setOpen(false)}>Close</button>
   {open && <><ApiKeyManager resourceId="project-fixture" api={api} />
     <CopyButton text="test-only-created-key">Copy created key</CopyButton></>}
 </>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
` },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
  loader: { '.css': 'empty' },
});
const fixture = await readFile(resolve(output, 'fixture.js'), 'utf8');
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
});
try {
  for (const button of ['复制完整密钥', 'Copy created key']) {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.fallbackCopies = [];
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: () => new Promise((_, reject) => { window.rejectClipboard = reject; }) },
      });
      document.execCommand = () => {
        window.fallbackCopies.push(window.getSelection()?.toString());
        return true;
      };
    });
    await page.route('https://key-clipboard.test/**', route => route.fulfill({
      contentType: route.request().url().endsWith('.js') ? 'text/javascript' : 'text/html',
      body: route.request().url().endsWith('.js') ? fixture :
        '<div id="root"></div><script type="module" src="/fixture.js"></script>',
    }));
    await page.goto('https://key-clipboard.test/');
    await page.getByRole('button', { name: button, exact: button === '复制完整密钥' }).click();
    await page.waitForFunction(() => !!window.rejectClipboard);
    await page.getByRole('button', { name: 'Close', exact: true }).click();
    await page.evaluate(() => window.rejectClipboard(new Error('Delayed clipboard rejection')));
    await page.waitForTimeout(150);
    assert.deepEqual(await page.evaluate(() => window.fallbackCopies), [],
      'Closing a key view must prevent an obsolete fallback copy');
    assert.equal(await page.getByRole('textbox', { name: '完整密钥', exact: true }).count(), 0);
    assert.equal(await page.getByText('复制失败，请手动选择文本复制', { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log('API key clipboard cancellation checks passed for revealed and created keys.');
} finally {
  await browser.close();
}
