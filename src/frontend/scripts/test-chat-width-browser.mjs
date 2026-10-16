import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/chat-width-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: {
    contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Layout } from 'antd';
import { SlidePanel } from './src/components/common/SlidePanel';
import { ConversationNavigation } from './src/components/chat/ConversationNavigation';
import { usePaneOcclusion } from './src/hooks/usePaneOcclusion';
import './src/styles/index';

const messages = [{ uid: 'question', role: 'user', content: 'User question', ts: 1 }];
function Fixture() {
  const [done, setDone] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  const [full, setFull] = React.useState(false);
  const [inner, setInner] = React.useState(false);
  const { paneRef, hidden } = usePaneOcclusion(open, full);
  const listRef = React.useRef(null);
  return (
    <Layout className="jx-appShell" style={{ height: '100vh' }}>
      <Layout.Sider width={260} className="jx-sider ant-layout-sider-collapsed">Sidebar</Layout.Sider>
      <Layout className={"jx-appMainLayout"+(full?" is-canvasFullscreen":"")} style={{ overflow: 'hidden' }}>
        <div ref={paneRef} inert={hidden} aria-hidden={hidden || undefined} className={"jx-primaryPane is-chatSurface"+(open?" is-canvasOpen":"")}>
          <div className="jx-chatTopbar">Chat</div>
          <div className="jx-mainRow">
            <Layout.Content className="jx-content jx-content--chatSurface jx-content--messageSurface">
              <div className="jx-panel" data-panel="chat">
                <div className="jx-chatWrap">
                  <ConversationNavigation hidden={hidden} messages={messages} chatListRef={listRef} />
                  <div className="jx-chatViewportFrame">
                    <div className="jx-chatViewport">
                      <div className="jx-chatList" ref={listRef}>
                        <p data-message-uid="question">User question</p>
                        <p>{done ? 'Answer complete' : 'Streaming answer'}</p>
                        <button onClick={() => setDone(true)}>Finish</button>
                        <button className="jx-rightSidebarToggle" onClick={() => setOpen(!open)}>Toggle canvas</button>
                        <button onClick={() => setInner(!inner)}>Toggle inner panel</button>
                      </div>
                    </div>
                  </div>
                  <div className="jx-chatFooter">Composer</div>
                </div>
              </div>
            </Layout.Content>
            <SlidePanel show={inner && !open} panelKey="tool"><div className="fixture-inner" style={{width:400}}>Tool result panel</div></SlidePanel>
            <SlidePanel show={false} panelKey="prompt" />
          </div>
        </div>
        <SlidePanel show={open} panelKey="canvas" className={"jx-canvasPanelSlot"+(full?" is-fullscreen":"")}>
          <div className="jx-rightSidebar"><button onClick={()=>setFull(!full)}>Fullscreen</button><button onClick={()=>{setFull(false);setOpen(false);}}>Close canvas</button></div>
        </SlidePanel>
      </Layout>
    </Layout>
  );
}
createRoot(document.getElementById('root')).render(<Fixture />);
`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile: resolve(output, 'fixture.js'),
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.svg': 'dataurl', '.png': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
});
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
});
try {
  const page = await browser.newPage({ viewport: { width: 1846, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://layout.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/fixture.js' || path === '/fixture.css') {
      return route.fulfill({
        body: await readFile(resolve(output, path.slice(1))),
        contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css',
      });
    }
    return route.fulfill({
      contentType: 'text/html',
      body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>',
    });
  });
  await page.goto('http://layout.test');
  await page.getByRole('button', { name: 'Finish', exact: true }).waitFor();
  if (process.env.CHAT_LAYOUT_BASE_URL) {
    const base = process.env.CHAT_LAYOUT_BASE_URL;
    const response = await page.request.get(base);
    assert.equal(response.status(), 200);
    const html = await response.text();
    const styles = [...html.matchAll(/href="([^"]+[.]css)"/g)].map(match => new URL(match[1], base).href);
    assert.ok(styles.length > 0, 'deployed app must expose its stylesheets');
    // Exercise the CSS actually served by Docker, without the fixture's source CSS.
    await page.locator('link[href="/fixture.css"]').evaluate(element => element.remove());
    for (const url of styles) {
      const stylesheet = await page.request.get(url);
      assert.equal(stylesheet.status(), 200);
      await page.addStyleTag({ content: await stylesheet.text() });
    }
  }

  const readingBounds = () => page.evaluate(() =>
    ['.jx-chatList', '.jx-chatFooter'].map(selector => {
      const rect = document.querySelector(selector).getBoundingClientRect();
      return { x: rect.x, width: rect.width };
    }));
  // Compare with the old outer width cap: moving the scrollbar must not move
  // or resize the reading column or composer.
  const legacy = await page.addStyleTag({
    content: '.jx-content--messageSurface > .jx-panel { max-width:1400px }',
  });
  const before = await readingBounds();
  await legacy.evaluate(element => element.remove());
  assert.deepEqual(await readingBounds(), before);
  await page.getByRole('button', { name: 'Finish', exact: true }).click();
  assert.deepEqual(await readingBounds(), before);

  async function check(label) {
    await page.waitForFunction(() => {
      const nav = document.querySelector('.jx-conversationNav');
      const viewport = document.querySelector('.jx-chatViewport').getBoundingClientRect();
      return nav && Math.abs(nav.getBoundingClientRect().right - (viewport.right - 8)) < 2;
    });
    const bounds = await page.evaluate(() => {
      const pane = document.querySelector('.jx-content').getBoundingClientRect();
      const viewport = document.querySelector('.jx-chatViewport').getBoundingClientRect();
      const list = document.querySelector('.jx-chatList').getBoundingClientRect();
      const footer = document.querySelector('.jx-chatFooter').getBoundingClientRect();
      return {
        gap: pane.right - viewport.right,
        center: (list.left + list.right - pane.left - pane.right) / 2,
        aligned: Math.abs(list.left - footer.left),
        overflow: document.documentElement.scrollWidth - window.innerWidth,
      };
    });
    assert.ok(Math.abs(bounds.gap) <= 1, label + JSON.stringify(bounds));
    assert.ok(Math.abs(bounds.center) <= 2, label + JSON.stringify(bounds));
    assert.ok(bounds.aligned <= 2, label + JSON.stringify(bounds));
    assert.ok(bounds.overflow <= 0, label + JSON.stringify(bounds));
  }
  for (const width of [1440, 1846, 2769, 1100, 1024, 960, 760, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.waitForFunction(() => {
      const sidebar = document.querySelector('.jx-sider');
      const expected = window.innerWidth <= 960 ? 0 : parseFloat(sidebar.style.width);
      return Math.abs(sidebar.getBoundingClientRect().width - expected) < 0.1;
    });
    await check('viewport ' + width);
    const baseline = await page.addStyleTag({
      content: '.jx-content--messageSurface > .jx-panel{max-width:1400px}.jx-chatViewportFrame{width:auto;align-self:stretch}.jx-chatViewport{padding-inline:0;scrollbar-gutter:auto}',
    });
    const oldReading = await readingBounds();
    await baseline.evaluate(element => element.remove());
    assert.deepEqual(await readingBounds(), oldReading, 'reading dimensions changed at ' + width);
  }
  await page.setViewportSize({ width: 1846, height: 1000 });
  await page.waitForFunction(() => {
    const sidebar = document.querySelector('.jx-sider');
    return Math.abs(sidebar.getBoundingClientRect().width - parseFloat(sidebar.style.width)) < 1;
  });
  const paneWidth = await page.locator('.jx-primaryPane').evaluate(element => element.getBoundingClientRect().width);
  await page.getByRole('button', { name: 'Toggle canvas' }).click();
  await page.locator('.jx-canvasPanelSlot').waitFor();
  await check('split canvas');
  assert.ok(await page.locator('.jx-primaryPane').evaluate(element => element.getBoundingClientRect().width) < paneWidth);
  await page.getByRole('button', { name: 'Toggle canvas' }).click();
  await page.locator('.jx-canvasPanelSlot').waitFor({ state: 'detached' });
  assert.ok(Math.abs(await page.locator('.jx-primaryPane').evaluate(element => element.getBoundingClientRect().width) - paneWidth) < 1);
  await check('collapsed canvas');
  const restoredReading = await readingBounds();
  await page.getByRole('button', {name:'Toggle inner panel'}).click();
  await page.locator('.fixture-inner').waitFor();
  await check('internal tool/prompt/project panel');
  await page.getByRole('button', {name:'Toggle inner panel'}).click();
  await page.locator('.fixture-inner').waitFor({state:'detached'});
  await check('closed internal panel');
  assert.deepEqual(await readingBounds(), restoredReading);

  await page.getByRole('button', {name:'Toggle canvas'}).click();
  await page.getByRole('button', {name:'Fullscreen', exact:true}).click();
  await page.locator('.jx-primaryPane[inert]').waitFor({state:'attached'});
  await page.locator('.jx-conversationNav').waitFor({state:'detached'});
  for (let step=0; step<4; step++) {
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.querySelector('.jx-primaryPane').contains(document.activeElement)),false);
  }
  await page.getByRole('button', {name:'Fullscreen', exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('.jx-primaryPane').inert);
  await check('exit fullscreen');

  for (const width of [1100,960,900,760,390]) {
    await page.setViewportSize({width,height:1000});
    await page.locator('.jx-primaryPane[inert]').waitFor({state:'attached'});
    await page.locator('.jx-conversationNav').waitFor({state:'detached'});
    await page.getByRole('button',{name:'Fullscreen',exact:true}).focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.querySelector('.jx-primaryPane').contains(document.activeElement)),false);
  }
  await page.getByRole('button',{name:'Close canvas',exact:true}).click();
  await page.locator('.jx-canvasPanelSlot').waitFor({state:'detached'});
  await page.waitForFunction(()=>!document.querySelector('.jx-primaryPane').inert);
  assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('jx-rightSidebarToggle')),true,'collapse restores focus to chat toggle');
  await page.setViewportSize({width:1846,height:1000});
  await page.waitForFunction(()=>Math.abs(document.querySelector('.jx-sider').getBoundingClientRect().width-260)<0.1);
  await check('closed overlay and returned to desktop');
  // Chromium may round a settled flex position by 1/64 CSS pixel after resizing.
  const returnedReading = await readingBounds();
  returnedReading.forEach((bounds, index) => {
    assert.ok(Math.abs(bounds.x - restoredReading[index].x) < 0.1, 'reading column x restored');
    assert.ok(Math.abs(bounds.width - restoredReading[index].width) < 0.1, 'reading column width restored');
  });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await check('dark and reduced motion');
  // Non-chat pages retain their existing page width cap.
  assert.equal(await page.locator('.jx-panel').evaluate(element => {
    element.parentElement.classList.remove('jx-content--messageSurface');
    const maxWidth = getComputedStyle(element).maxWidth;
    element.parentElement.classList.add('jx-content--messageSurface');
    return maxWidth;
  }), '1400px');
  await page.locator('.jx-chatList').evaluate(element => { element.style.minHeight = '2000px'; });
  const edge = await page.evaluate(() => {
    const viewport = document.querySelector('.jx-chatViewport');
    const rect = viewport.getBoundingClientRect();
    return { scrolls: viewport.scrollHeight > viewport.clientHeight,
      hit: document.elementFromPoint(rect.right - 2, rect.top + rect.height / 2)?.className };
  });
  assert.ok(edge.scrolls);
  assert.ok(String(edge.hit).includes('jx-chatViewport'), 'scrollbar must be visible at the pane edge: ' + JSON.stringify(edge));
  assert.deepEqual(errors, []);
  await page.screenshot({ path: resolve(output, 'after.png') });
  console.log('PASS chat width: pane-edge scrollbar, unclipped long history, unchanged reading geometry, 8 widths, internal panels, fullscreen/overlay focus isolation and recovery, navigation, themes and non-chat layout');
} finally {
  await browser.close();
}
