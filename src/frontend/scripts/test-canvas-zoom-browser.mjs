// Canvas content zoom: docx fit / numeric scale / wide-table scrolling, image
// zoom + pan, markdown CSS zoom, keyboard + ctrl-wheel scoping, panel-width
// persistence. Real docx-preview rendering of a generated A4 document.
import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import JSZip from 'jszip';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/canvas-zoom');
await mkdir(output, { recursive: true });

async function fixtureDocx() {
  const zip = new JSZip();
  zip.file('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>');
  zip.file('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>');
  const p = (text, size = 22) => `<w:p><w:r><w:rPr><w:sz w:val="${size}"/></w:rPr><w:t xml:space="preserve">${text}</w:t></w:r></w:p>`;
  const cell = (text) => `<w:tc><w:tcPr><w:tcW w:w="2500" w:type="dxa"/></w:tcPr>${p(text)}</w:tc>`;
  const cols = Array.from({ length: 8 }, (_, i) => `Column ${i + 1}`);
  const table = `<w:tbl><w:tblPr><w:tblW w:w="20000" w:type="dxa"/><w:tblLayout w:type="fixed"/><w:tblBorders><w:top w:val="single" w:sz="4"/><w:bottom w:val="single" w:sz="4"/><w:insideV w:val="single" w:sz="4"/></w:tblBorders></w:tblPr><w:tblGrid>${cols.map(() => '<w:gridCol w:w="2500"/>').join('')}</w:tblGrid><w:tr>${cols.map(cell).join('')}</w:tr><w:tr>${cols.map((c) => cell('value of ' + c)).join('')}</w:tr></w:tbl>`;
  const body = [
    p('Quarterly Operations Review', 48),
    p('Summary', 32),
    ...Array.from({ length: 6 }, (_, i) => p(`Paragraph ${i + 1}. The quick brown fox jumps over the lazy dog while the operations team reviews throughput, latency and cost across every region this quarter.`)),
    table,
    ...Array.from({ length: 30 }, (_, i) => p(`Follow-up line ${i + 1}.`)),
  ].join('');
  zip.file('word/document.xml', `<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708"/></w:sectPr></w:body></w:document>`);
  return zip.generateAsync({ type: 'nodebuffer' });
}

const files = {
  doc: { body: await fixtureDocx(), type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
  manual: { body: await readFile(resolve('../../internal design docs(V3.0).docx')), type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
  img: { body: '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000"><rect width="1600" height="1000" fill="#4a90d9"/><circle cx="800" cy="500" r="300" fill="#f5a623"/></svg>', type: 'image/svg+xml' },
  md: { body: '# Heading\n\nSome **markdown** body text.\n', type: 'text/markdown' },
  bin: { body: 'x', type: 'application/octet-stream' },
};

await build({
  stdin: {
    contents: `
import {createRoot} from 'react-dom/client';
import {RightSidebarPanel} from './src/components/canvas/RightSidebarPanel';
import {useCanvasStore} from './src/stores/canvasStore';
import {useCanvasZoomStore} from './src/components/canvas/canvasZoom';
import './src/styles/variables.css';import './src/styles/motion.css';import './src/styles/common.css';import './src/styles/canvas.css';
window.canvas=useCanvasStore;window.zoom=useCanvasZoomStore;
const names={doc:'review.docx',manual:'manual.docx',img:'chart.svg',md:'notes.md',bin:'blob.bin'};
window.openFile=(id)=>useCanvasStore.getState().openCanvas({file_id:id,name:names[id],url:'/api/files/'+id,origin:'cloud'});
createRoot(document.getElementById('root')).render(<main><RightSidebarPanel/></main>);
`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  external: ['/loader.gif', '/loader-done.png'], loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  outfile: resolve(output, 'fixture.js'),
});

const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://127.0.0.1').pathname;
  if (path === '/fixture.js' || path === '/fixture.css') {
    response.writeHead(200, { 'Content-Type': path.endsWith('.js') ? 'text/javascript' : 'text/css' });
    return response.end(await readFile(resolve(output, path.slice(1))));
  }
  const file = files[path.replace(/^\/api\/files\//, '').split('/')[0]];
  if (file && !path.endsWith('/location')) {
    response.writeHead(200, { 'Content-Type': file.type });
    return response.end(file.body);
  }
  if (path.endsWith('/location')) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    return response.end(JSON.stringify({ kind: 'personal', root_name: null, relative_path: 'docs' }));
  }
  if (path.startsWith('/api')) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    return response.end(JSON.stringify({ code: 0, data: [] }));
  }
  response.writeHead(200, { 'Content-Type': 'text/html' });
  response.end('<!doctype html><html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;background:var(--color-bg-container);color:var(--color-text);font:13px Arial}main{height:100vh;width:100%;display:flex}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}/`;

const browser = await chromium.launch({ headless: true });
let page;
try {
  page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(origin);

  const body = page.locator('.jx-canvas-body');
  const zoomValue = page.locator('.jx-canvasZoom-value');
  const state = () => page.evaluate(() => {
    const viewport = document.querySelector('.jx-canvas-body');
    const vb = viewport.getBoundingClientRect();
    const pageBox = document.querySelector('section.docx').getBoundingClientRect();
    const table = document.querySelector('section.docx table')?.getBoundingClientRect();
    return {
      vb: { x: vb.x, w: viewport.clientWidth }, page: { x: pageBox.x, w: pageBox.width },
      tableRight: table ? table.right - vb.x + viewport.scrollLeft : 0,
      scrollWidth: viewport.scrollWidth, scrollLeft: viewport.scrollLeft,
      zoom: document.querySelector('.docx-wrapper').style.zoom,
    };
  });
  const percent = async () => Number((await zoomValue.textContent()).replace('%', ''));

  // ── Docx, wide pane: fit centres the page and fills the width (capped at 150%).
  await page.evaluate(() => window.openFile('doc'));
  await page.locator('section.docx').first().waitFor();
  await page.waitForFunction(() => document.querySelector('.docx-wrapper')?.style.zoom);
  let s = await state();
  const fitWide = await percent();
  assert.ok(fitWide > 100 && fitWide <= 150, `wide fit ${fitWide}`);
  const leftGap = s.page.x - s.vb.x, rightGap = s.vb.x + s.vb.w - (s.page.x + s.page.w);
  assert.ok(Math.abs(leftGap - rightGap) < 2, `page centred (${leftGap} vs ${rightGap})`);
  assert.equal(await page.getByRole('button', { name: '适应宽度' }).getAttribute('aria-pressed'), 'true');
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'docx-wide-light.png') });

  // ── Narrow pane: page shrinks to fit, nothing clipped.
  await page.setViewportSize({ width: 480, height: 900 });
  await page.waitForFunction((prev) => Number(document.querySelector('.jx-canvasZoom-value').textContent.replace('%', '')) < prev, fitWide);
  s = await state();
  const fitNarrow = await percent();
  assert.ok(fitNarrow < 70, `narrow fit ${fitNarrow}`);
  assert.ok(s.page.x >= s.vb.x - 1 && s.page.x + s.page.w <= s.vb.x + s.vb.w + 1, 'page inside narrow pane');
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'docx-narrow-light.png') });

  // ── Wide table is never clipped: its right edge is inside the scrollable area.
  assert.ok(s.scrollWidth + 1 >= s.tableRight, `table reachable (${s.scrollWidth} vs ${s.tableRight})`);

  // ── Buttons: + steps above fit, page overflows → horizontal scroll from the page's left edge.
  await page.getByRole('button', { name: '放大' }).click();
  await page.getByRole('button', { name: '放大' }).click();
  assert.ok(await percent() > fitNarrow);
  s = await state();
  assert.ok(s.scrollWidth > s.vb.w, 'horizontal scroll at > fit');
  assert.ok(s.page.x >= s.vb.x - 1, 'left edge of the page reachable at scrollLeft 0');
  assert.equal(await page.getByRole('button', { name: '适应宽度' }).getAttribute('aria-pressed'), 'false');
  await zoomValue.click();
  assert.equal(await percent(), 100);
  s = await state();
  assert.equal(s.zoom, '1');
  assert.ok(Math.abs(s.page.w - 794) < 3, `100% = A4 width (${s.page.w})`);

  // ── Keyboard + wheel scoped to the hovered / focused pane.
  await body.hover();
  await page.keyboard.press('Control+Equal');
  assert.equal(await percent(), 110);
  await page.keyboard.press('Control+Minus');
  assert.equal(await percent(), 100);
  await page.mouse.wheel(0, -200).catch(() => {});
  await page.keyboard.down('Control');
  await page.mouse.wheel(0, -100);
  await page.keyboard.up('Control');
  await page.waitForFunction(() => Number(document.querySelector('.jx-canvasZoom-value').textContent.replace('%', '')) > 100);
  await page.keyboard.press('Control+0');
  assert.equal(await page.getByRole('button', { name: '适应宽度' }).getAttribute('aria-pressed'), 'true');
  // Pointer and focus outside the pane: Ctrl+= must stay the browser's.
  await page.evaluate(() => {
    const outside = document.createElement('input');
    outside.id = 'outside';
    Object.assign(outside.style, { position: 'fixed', left: '0', top: '0', zIndex: '99' });
    document.body.append(outside);
  });
  await page.mouse.move(1, 1);
  await page.dispatchEvent('.jx-canvas', 'pointerleave');
  await page.focus('#outside');
  const prevented = await page.evaluate(() => {
    const event = new KeyboardEvent('keydown', { key: '=', ctrlKey: true, bubbles: true, cancelable: true });
    document.getElementById('outside').dispatchEvent(event);
    return event.defaultPrevented;
  });
  assert.equal(prevented, false, 'Ctrl+= outside the pane is not hijacked');
  assert.equal(await page.getByRole('button', { name: '适应宽度' }).getAttribute('aria-pressed'), 'true');

  // ── Dark theme: page stays white paper on a dark desk.
  await page.setViewportSize({ width: 900, height: 900 });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; window.openFile('manual'); });
  await page.waitForFunction(() => document.querySelectorAll('section.docx').length > 1 && document.querySelector('.docx-wrapper')?.style.zoom);
  const colors = await page.evaluate(() => ({
    paper: getComputedStyle(document.querySelector('section.docx')).backgroundColor,
    desk: getComputedStyle(document.querySelector('.jx-canvas-docx')).backgroundColor,
  }));
  assert.equal(colors.paper, 'rgb(255, 255, 255)');
  assert.notEqual(colors.desk, 'rgb(255, 255, 255)');
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'docx-manual-dark.png') });
  await page.setViewportSize({ width: 480, height: 900 });
  await page.waitForTimeout(300);
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'docx-manual-dark-narrow.png') });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light'; });
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'docx-manual-light-narrow.png') });

  // ── Image: fit reports its scale; double-click → 100%, drag pans, double-click → fit.
  await page.setViewportSize({ width: 900, height: 900 });
  await page.evaluate(() => window.openFile('img'));
  const img = page.locator('.jx-canvas-image-img');
  await page.waitForFunction(() => document.querySelector('.jx-canvas-image-img')?.complete);
  await page.waitForFunction(() => Number(document.querySelector('.jx-canvasZoom-value')?.textContent.replace('%', '')) < 100);
  const fitImg = await percent();
  assert.ok(Math.abs((await img.boundingBox()).width - 1600 * fitImg / 100) < 20);
  await img.dblclick();
  assert.equal(await percent(), 100);
  assert.equal(Math.round((await img.boundingBox()).width), 1600);
  const viewport = page.locator('.jx-canvas-image');
  const before = await viewport.evaluate((el) => [el.scrollLeft, el.scrollTop]);
  assert.ok(before[0] > 0, 'zoomed image opens centred');
  const vbox = await viewport.boundingBox();
  await page.mouse.move(vbox.x + vbox.width / 2, vbox.y + vbox.height / 2);
  await page.mouse.down();
  await page.mouse.move(vbox.x + vbox.width / 2 + 150, vbox.y + vbox.height / 2 + 80, { steps: 5 });
  await page.mouse.up();
  const after = await viewport.evaluate((el) => [el.scrollLeft, el.scrollTop]);
  assert.ok(Math.abs(before[0] - after[0] - 150) < 3 && Math.abs(before[1] - after[1] - 80) < 3, `drag pans (${before} → ${after})`);
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'image-100.png') });
  await img.dblclick();
  assert.equal(await page.getByRole('button', { name: '适应宽度' }).getAttribute('aria-pressed'), 'true');

  // ── Markdown: CSS-var zoom.
  await page.evaluate(() => window.openFile('md'));
  await page.locator('.jx-canvas-markdown').waitFor();
  await page.getByRole('button', { name: '放大' }).click();
  assert.equal(await page.locator('.jx-canvas-markdown').evaluate((el) => getComputedStyle(el).zoom), '1.1');

  // ── Unsupported category: no control.
  await page.evaluate(() => window.openFile('bin'));
  await page.locator('.jx-canvas-unknown').waitFor();
  assert.equal(await page.locator('.jx-canvasZoom').count(), 0);

  // ── Panel width survives a reload, clamped to the window.
  await page.evaluate(() => window.canvas.getState().setPanelWidth(5000));
  await page.waitForTimeout(300);
  await page.reload();
  await page.waitForFunction(() => window.canvas);
  assert.equal(await page.evaluate(() => window.canvas.getState().panelWidth), 900 - 420);
  await page.evaluate(() => window.canvas.getState().setPanelWidth(450));
  await page.waitForTimeout(300);
  await page.reload();
  await page.waitForFunction(() => window.canvas);
  assert.equal(await page.evaluate(() => window.canvas.getState().panelWidth), 450);

  assert.deepEqual(errors, []);
  console.log('Canvas zoom: docx fit (wide/narrow, centred, wide table), steps/100%, keyboard + ctrl-wheel scoping, dark paper, image zoom/pan/dblclick, markdown zoom, hidden for unsupported, panel width persistence passed');
} catch (error) {
  await page?.screenshot({ path: resolve(output, 'failure.png') });
  throw error;
} finally {
  await browser.close();
  await new Promise((done) => server.close(done));
}
