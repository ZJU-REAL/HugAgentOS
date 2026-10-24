// Canvas viewers in a real Vite dev server (pdf.js worker, ?worker and
// import.meta.glob assets need Vite): PDF render / virtualization / text-layer
// selection / zoom + fit-on-resize / find / bookmarks outline, code view
// highlighting / line numbers / wrap / find, markdown outline / find.
// PLAYWRIGHT_CHROMIUM may point at a cached chromium-headless-shell binary.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import { chromium } from 'playwright';

const dir = resolve('node_modules/.tmp/canvas-viewers');
await mkdir(dir, { recursive: true });
await writeFile(resolve(dir, 'index.html'), `<!doctype html>
<html><head><meta charset="utf-8"><style>*{box-sizing:border-box}html,body,#root{margin:0;height:100%}body{font:15px Arial;color:var(--color-text);background:var(--color-bg-container)}</style></head>
<body><div id="root"></div><script type="module" src="./fixture.tsx"></script></body></html>
`);
await writeFile(resolve(dir, 'fixture.tsx'), `import { createRoot } from 'react-dom/client';
import { RightSidebarPanel } from '../../../src/components/canvas/RightSidebarPanel';
import { useCanvasStore } from '../../../src/stores/canvasStore';
import { useCanvasZoomStore } from '../../../src/components/canvas/canvasZoom';
import '../../../src/styles/index';
import 'highlight.js/styles/github.css';

Object.assign(window, { canvasStore: useCanvasStore, zoomStore: useCanvasZoomStore });
function Fixture() {
  const open = useCanvasStore((s) => s.isOpen);
  return <main className="jx-appMainLayout" style={{ display: 'flex', height: '100%' }}>
    <div style={{ flex: 1 }}>chat</div>
    {open && <div className="jx-canvasPanelSlot" style={{ display: 'flex', flex: 'none', height: '100%' }}><RightSidebarPanel /></div>}
  </main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
`);
const server = await createServer({ server: { port: 0 }, logLevel: 'error' });
await server.listen();

// Minimal multi-page PDF with Helvetica text and a 2-level outline.
function makePdf(pages = 30) {
  const objs = [];
  const add = (body) => { objs.push(body); return objs.length; };
  const catalog = add(null), pagesId = add(null), font = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  const pageIds = [];
  for (let i = 1; i <= pages; i++) {
    const lines = [`Chapter ${Math.ceil(i / 10)} - Page ${i}`, 'The quick brown fox jumps over the lazy dog.', `Needle marker on page ${i}.`, 'Selectable text layer line for quoting.'];
    const stream = 'BT /F1 18 Tf 72 720 Td ' + lines.map((l, k) => `${k ? '0 -28 Td ' : ''}(${l}) Tj`).join(' ') + ' ET';
    const content = add(`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`);
    pageIds.push(add(`<< /Type /Page /Parent ${pagesId} 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 ${font} 0 R >> >> /Contents ${content} 0 R >>`));
  }
  objs[pagesId - 1] = `<< /Type /Pages /Kids [${pageIds.map((id) => `${id} 0 R`).join(' ')}] /Count ${pages} >>`;
  // Outline: Chapter 1 (p1) > Section 1.2 (p5); Chapter 2 (p11); Chapter 3 (p21)
  const outlines = add(null);
  const ch = [add(null), add(null), add(null)];
  const sec = add(null);
  const dest = (p, top = 792) => `[${pageIds[p - 1]} 0 R /XYZ 0 ${top} null]`;
  objs[outlines - 1] = `<< /Type /Outlines /First ${ch[0]} 0 R /Last ${ch[2]} 0 R /Count 4 >>`;
  objs[ch[0] - 1] = `<< /Title (Chapter 1) /Parent ${outlines} 0 R /Next ${ch[1]} 0 R /First ${sec} 0 R /Last ${sec} 0 R /Count 1 /Dest ${dest(1)} >>`;
  objs[sec - 1] = `<< /Title (Section 1.2) /Parent ${ch[0]} 0 R /Dest ${dest(5, 700)} >>`;
  objs[ch[1] - 1] = `<< /Title (Chapter 2) /Parent ${outlines} 0 R /Prev ${ch[0]} 0 R /Next ${ch[2]} 0 R /Dest ${dest(11)} >>`;
  objs[ch[2] - 1] = `<< /Title (Chapter 3) /Parent ${outlines} 0 R /Prev ${ch[1]} 0 R /Dest ${dest(21)} >>`;
  objs[catalog - 1] = `<< /Type /Catalog /Pages ${pagesId} 0 R /Outlines ${outlines} 0 R >>`;
  let out = '%PDF-1.4\n';
  const offsets = [];
  objs.forEach((body, i) => { offsets.push(out.length); out += `${i + 1} 0 obj\n${body}\nendobj\n`; });
  const xref = out.length;
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n` + offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n \n`).join('');
  out += `trailer\n<< /Size ${objs.length + 1} /Root ${catalog} 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}

const pdf = makePdf(30);
const code = Array.from({ length: 60 }, (_, i) => `export function fn${i}(value: number): number { return value * ${i}; } // needle ${i}`).join('\n');
const markdown = '# Title\n\nIntro needle\n\n## Part A\n\ntext\n\n### Detail A1\n\nmore needle\n\n## Part B\n\nend';

const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.setDefaultTimeout(15000);
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
await page.route(/\/(files|v1)\//, (route) => {
  const url = new URL(route.request().url()); url.pathname = url.pathname.replace(/^\/api/, '');
  if (url.pathname === '/files/doc') return route.fulfill({ contentType: 'application/pdf', body: pdf });
  if (url.pathname === '/files/code') return route.fulfill({ contentType: 'text/plain', body: code });
  if (url.pathname === '/files/notes') return route.fulfill({ contentType: 'text/markdown', body: markdown });
  const text = url.pathname.match(/^\/v1\/file\/([^/]+)\/text$/);
  if (text) return route.fulfill({ json: { code: 0, data: { file_id: text[1], name: text[1], content: text[1] === 'code' ? code : markdown, revision: 'r1', size: 1, can_edit: false } } });
  return route.fulfill({ json: { code: 0, data: null } });
});
await page.goto(`${server.resolvedUrls.local[0]}node_modules/.tmp/canvas-viewers/index.html`);
const open = (file_id, name, mime_type) => page.evaluate((a) => window.canvasStore.getState().openCanvas({ ...a, url: '/files/' + a.file_id }), { file_id, name, mime_type });

try {
  // ── PDF: render, virtualization, text layer selection ──
  await open('doc', 'fixture.pdf', 'application/pdf');

  await page.locator('.jx-pdfPage canvas').first().waitFor();
  const pageCount = await page.locator('.jx-pdfPage').count();
  assert.equal(pageCount, 30);
  const rendered = await page.locator('.jx-pdfPage canvas').count();
  assert.ok(rendered < 30, `virtualized: only ${rendered} canvases rendered`);
  await page.locator('.jx-pdfPage[data-page="1"] .textLayer span').first().waitFor();
  assert.equal(await page.locator('.jx-pdfViewer-pageIndicator').innerText(), '1 / 30');
  // fit width: page width == scroller content width minus padding
  const fit = await page.evaluate(() => {
    const s = document.querySelector('.jx-pdfViewer-scroll');
    const p = document.querySelector('.jx-pdfPage');
    return { avail: s.clientWidth - 32, page: p.getBoundingClientRect().width };
  });
  assert.ok(Math.abs(fit.avail - fit.page) <= 1, `fit width ${JSON.stringify(fit)}`);
  // select text in the text layer with the mouse
  const span = page.locator('.jx-pdfPage[data-page="1"] .textLayer span', { hasText: 'quick brown fox' });
  const box = await span.boundingBox();
  await page.mouse.move(box.x + 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 2, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();
  const selected = await page.evaluate(() => String(window.getSelection()));
  assert.match(selected, /quick brown fox/);
  assert.ok(await page.evaluate(() => !!window.getSelection().anchorNode.parentElement.closest('.jx-canvas-body')), 'selection lives inside .jx-canvas-body');
  await page.screenshot({ path: resolve(dir, 'pdf-fit.png') });

  // zoom numeric -> page width grows; back to fit
  const tabId = await page.evaluate(() => window.canvasStore.getState().activeTabId);
  await page.evaluate((id) => window.zoomStore.getState().setZoom(id, 1.5), tabId);
  await page.waitForFunction(() => Math.abs(document.querySelector('.jx-pdfPage').getBoundingClientRect().width - 612 * 1.5 * 96 / 72) < 2);
  await page.evaluate((id) => window.zoomStore.getState().setZoom(id, 'fit'), tabId);
  // fit recomputes on resize
  await page.evaluate(() => window.canvasStore.getState().setPanelWidth(560));
  await page.waitForFunction(() => {
    const s = document.querySelector('.jx-pdfViewer-scroll');
    return Math.abs(document.querySelector('.jx-pdfPage').getBoundingClientRect().width - (s.clientWidth - 32)) <= 1;
  });

  // ── Find in pdf (Ctrl+F while hovering the pane) ──
  await page.mouse.move(box.x + 10, box.y + 40);
  await page.keyboard.press('Control+f');
  const input = page.getByRole('textbox', { name: '查找内容' });
  await input.waitFor();
  assert.ok(await input.evaluate((el) => el === document.activeElement), 'find input focused');
  await input.fill('needle marker');
  await page.locator('.jx-canvasFind-count', { hasText: '1/30' }).waitFor();
  await page.keyboard.press('Enter'); // -> 2/30
  await page.locator('.jx-canvasFind-count', { hasText: '2/30' }).waitFor();
  await page.keyboard.press('Shift+Enter');
  await page.keyboard.press('Shift+Enter'); // wraps to 30/30 -> page 30 must render
  await page.locator('.jx-canvasFind-count', { hasText: '30/30' }).waitFor();
  await page.waitForFunction(() => CSS.highlights.get('jx-canvas-find-primary-current')?.size === 1);
  const currentText = await page.evaluate(() => [...CSS.highlights.get('jx-canvas-find-primary-current')][0].toString());
  assert.equal(currentText.toLowerCase(), 'needle marker');
  assert.equal(await page.locator('.jx-pdfViewer-pageIndicator').innerText(), '30 / 30');
  await page.screenshot({ path: resolve(dir, 'pdf-find.png') });
  await page.keyboard.press('Escape');
  await input.waitFor({ state: 'detached' });
  assert.equal(await page.evaluate(() => CSS.highlights.has('jx-canvas-find-primary')), false);

  // ── Outline (pdf bookmarks) ──
  await page.getByRole('button', { name: '目录', exact: true }).click();
  const tree = page.getByRole('tree');
  await tree.getByRole('button', { name: 'Chapter 2' }).waitFor();
  assert.ok(await tree.getByRole('button', { name: 'Section 1.2' }).isVisible(), 'first level expanded');
  await tree.getByRole('button', { name: 'Chapter 2' }).click();
  await page.locator('.jx-pdfViewer-pageIndicator', { hasText: '11 / 30' }).waitFor();
  await tree.getByRole('button', { name: 'Section 1.2' }).click();
  await page.locator('.jx-pdfViewer-pageIndicator', { hasText: '5 / 30' }).waitFor();
  await page.screenshot({ path: resolve(dir, 'pdf-outline.png') });
  await page.getByRole('button', { name: '关闭目录' }).click();

  // ── Code view ──
  await open('code', 'sample.ts', 'text/plain');
  await page.locator('.jx-codeView-line').first().waitFor();
  await page.waitForFunction(() => document.querySelectorAll('.jx-codeView-line').length === 60);
  assert.ok(await page.locator('.jx-codeView .hljs-keyword').count() > 0, 'syntax highlighted');
  assert.match(await page.locator('.jx-codeView-line').first().evaluate((el) => getComputedStyle(el, '::before').content), /counter/);
  const nowrap = await page.locator('.jx-codeView-line').first().evaluate((el) => getComputedStyle(el).whiteSpace);
  await page.getByRole('button', { name: '自动换行' }).click();
  const wrapped = await page.locator('.jx-codeView-line').first().evaluate((el) => getComputedStyle(el).whiteSpace);
  assert.deepEqual([nowrap, wrapped], ['pre', 'pre-wrap']);
  await page.mouse.move(900, 400);
  await page.keyboard.press('Control+f');
  await page.getByRole('textbox', { name: '查找内容' }).fill('needle 5');
  await page.locator('.jx-canvasFind-count', { hasText: /^1\/11$/ }).waitFor(); // needle 5, 50..59
  await page.screenshot({ path: resolve(dir, 'code.png') });
  await page.keyboard.press('Escape');
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await page.screenshot({ path: resolve(dir, 'code-dark.png') });
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  // ── Markdown outline + find ──
  await open('notes', 'notes.md', 'text/markdown');
  await page.locator('.jx-canvas-markdown h2').first().waitFor();
  await page.getByRole('button', { name: '目录', exact: true }).click();
  await page.getByRole('tree').getByRole('button', { name: 'Part B' }).waitFor();
  // Three levels start expanded; a level can still be collapsed and reopened.
  await page.getByRole('tree').getByRole('button', { name: 'Detail A1' }).waitFor();
  await page.getByRole('tree').getByRole('button', { name: '收起' }).nth(1).click();
  assert.ok(await page.getByRole('tree').getByRole('button', { name: 'Detail A1' }).isHidden(), 'collapsed level hides its entries');
  await page.getByRole('tree').getByRole('button', { name: '展开' }).first().click();
  await page.getByRole('tree').getByRole('button', { name: 'Detail A1' }).waitFor();
  // Clicking an entry selects it even when nothing has to scroll.
  await page.getByRole('tree').getByRole('button', { name: 'Part B' }).click();
  assert.equal(await page.locator('.jx-canvasOutline-row.is-active').innerText(), 'Part B');
  await page.getByRole('button', { name: '在文件中查找' }).click();
  await page.getByRole('textbox', { name: '查找内容' }).fill('needle');
  await page.locator('.jx-canvasFind-count', { hasText: '1/2' }).waitFor();
  await page.screenshot({ path: resolve(dir, 'markdown.png') });

  assert.deepEqual(errors.filter((e) => !/favicon|404|Failed to load resource/.test(e)), []);
  console.log('canvas viewers: pdf render/virtualize/select/zoom/fit-resize/find/outline, code highlight/lines/wrap/find, markdown outline/find passed');
} finally {
  await browser.close();
  await server.close();
}
