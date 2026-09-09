import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/conversation-navigation-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: {
    contents: `
      import React, { useRef, useState } from 'react';
      import { createRoot } from 'react-dom/client';
      import { ConversationNavigation } from './src/components/chat/ConversationNavigation';
      import './src/styles/variables.css';
      const initial = Array.from({ length: 24 }, (_, i) => [
        { role: 'user', content: 'Question ' + (i + 1), ts: i * 2 + 10 },
        { role: 'assistant', content: 'Answer ' + (i + 1) + ': this is a readable summary.', ts: i * 2 + 11 },
      ]).flat();
      function Fixture() {
        const ref = useRef(null);
        const [messages, setMessages] = useState(initial);
        const [chat, setChat] = useState('one');
        return <>
          <header>
            <button onClick={() => setMessages(m => [
              { role: 'user', content: 'Older question', ts: 1 },
              { role: 'assistant', content: 'Older answer', ts: 2 }, ...m,
            ])}>Prepend history</button>
            <button onClick={() => { setChat('two'); setMessages([{ role: 'user', content: 'Other chat', ts: 100 }]); }}>Switch chat</button>
            <button onClick={() => setMessages([])}>Empty chat</button>
            <button onClick={() => document.documentElement.dataset.theme = 'dark'}>Dark mode</button>
            <button onClick={() => document.querySelector('.jx-content').style.width = '60%'}>Side panel</button>
            <button onClick={() => setMessages(m => [...m, { role: 'assistant', content: 'Streaming answer', ts: 200 }])}>Stream</button>
          </header>
          <main className="jx-content"><div className="jx-panel"><div className="jx-chatWrap">
            <ConversationNavigation key={chat} messages={messages} chatListRef={ref} />
            <div className="jx-chatList" ref={ref}>
              {messages.map(m => <section key={m.ts} data-message-ts={m.ts} className={'jx-msg ' + m.role}>
                <p>{m.content}</p>
              </section>)}
            </div>
            <footer className="jx-chatFooter">Message composer</footer>
          </div></div></main>
        </>;
      }
      createRoot(document.getElementById('root')).render(<Fixture />);
    `,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile: resolve(output, 'fixture.js'),
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
});
const fixtureCss = `
  * { box-sizing: border-box; }
  body { margin: 0; font-family: Arial, sans-serif; background: var(--color-bg-container); color: var(--color-text); }
  header { height: 64px; }
  .jx-content { height: calc(100vh - 64px); overflow-y: auto; overflow-x: hidden; }
  .jx-panel { width: 100%; }
  .jx-chatWrap { max-width: 760px; margin: 0 auto; min-height: 100%; }
  .jx-msg { min-height: 140px; padding: 16px; }
  .jx-msg.user { text-align: right; min-height: 100px; }
  .jx-chatFooter { position: sticky; bottom: 0; height: 120px; background: var(--color-bg-container); }
`;
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('https://conversation.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/fixture.js' || path === '/fixture.css') {
      await route.fulfill({ contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css',
        body: await readFile(resolve(output, path.slice(1))) });
    } else await route.fulfill({ contentType: 'text/html', body:
      '<html><head><link rel="stylesheet" href="/fixture.css"><style>' + fixtureCss
      + '</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://conversation.test/');
  const nav = page.getByRole('navigation', { name: '当前会话导航' });
  await nav.waitFor();
  assert.equal(await nav.getByRole('button').count(), 24);

  const question = number => nav.getByRole('button', { name: '跳转到：Question ' + number, exact: true });
  await question(5).hover();
  await page.getByText('Answer 5: this is a readable summary.', { exact: true }).last().waitFor();
  await question(5).click();
  await page.waitForFunction(() => document.querySelector('[data-message-ts="18"]').getBoundingClientRect().top < 100);
  assert.equal(await question(5).getAttribute('aria-current'), 'location');
  assert.ok(Math.abs(await page.locator('[data-message-ts="18"]').evaluate(el => el.getBoundingClientRect().top) - 88) < 2);

  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await question(7).click();
  await page.waitForFunction(() => Math.abs(document.querySelector('[data-message-ts="22"]').getBoundingClientRect().top - 88) < 2);
  assert.equal(await question(7).getAttribute('aria-current'), 'location');
  await page.emulateMedia({ reducedMotion: 'reduce' });

  // Older history must not change the target identity.
  await page.getByRole('button', { name: 'Prepend history', exact: true }).click();
  assert.equal(await nav.getByRole('button').count(), 25);
  await question(5).click();
  await page.waitForFunction(() => Math.abs(document.querySelector('[data-message-ts="18"]').getBoundingClientRect().top - 88) < 2);

  await page.locator('.jx-content').evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.waitForFunction(() => document.querySelector('[aria-current="location"]')?.getAttribute('aria-label') === '跳转到：Question 24');
  await question(24).focus();
  await page.keyboard.press('Home');
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), '跳转到：Older question');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.jx-content').scrollTop < 10);

  await page.getByRole('button', { name: 'Dark mode', exact: true }).click();
  await question(3).hover();
  await page.getByText('Answer 3: this is a readable summary.', { exact: true }).last().waitFor();
  await page.screenshot({ path: resolve(output, 'desktop-dark.png'), animations: 'disabled' });
  await page.getByRole('button', { name: 'Side panel', exact: true }).click();
  await page.waitForFunction(() => {
    const nav = document.querySelector('.jx-conversationNav').getBoundingClientRect();
    const chat = document.querySelector('.jx-content').getBoundingClientRect();
    return nav.right <= chat.right && nav.right >= chat.right - 16;
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.jx-content').evaluate(el => { el.style.width = '100%'; });
  await question(3).focus();
  await page.keyboard.press('Enter');
  await page.screenshot({ path: resolve(output, 'mobile.png'), animations: 'disabled' });
  assert.ok(await nav.evaluate(el => el.getBoundingClientRect().right <= window.innerWidth));

  await page.getByRole('button', { name: 'Switch chat', exact: true }).click();
  await nav.getByRole('button', { name: '跳转到：Other chat', exact: true }).waitFor();
  assert.equal(await nav.getByRole('button').count(), 1);
  assert.equal(await nav.getByRole('button').getAttribute('aria-label'), '跳转到：Other chat');
  await nav.getByRole('button').hover();
  await page.locator('.jx-conversationNav-summary').filter({ hasText: '暂无回复' }).waitFor();
  await page.getByRole('button', { name: 'Stream', exact: true }).click();
  await nav.getByRole('button').hover();
  await page.locator('.jx-conversationNav-summary').filter({ hasText: 'Streaming answer' }).waitFor();
  assert.equal(await nav.getByRole('button').count(), 1);
  await page.getByRole('button', { name: 'Empty chat', exact: true }).click();
  await nav.waitFor({ state: 'detached' });
  assert.equal(await page.getByRole('navigation').count(), 0);
  assert.deepEqual(errors, []);
  console.log('Browser navigation checks passed: click, preview, active turn, history prepend, keyboard, long rail, side pane, mobile, dark mode, chat switch, empty chat.');
} finally {
  await browser.close();
}
