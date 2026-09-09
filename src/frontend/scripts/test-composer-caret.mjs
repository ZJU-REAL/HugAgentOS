import assert from 'node:assert/strict';
import { build } from 'esbuild';

// Run from src/frontend with Node 20+ and Playwright installed.
// PLAYWRIGHT_MODULE may point to an existing Playwright index.mjs.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const result = await build({
  stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { InputArea } from './src/components/chat/InputArea';
    import 'antd/dist/reset.css';
    import './src/index.css';
    import './src/styles';
    const project = location.search.includes('project');
    createRoot(document.getElementById('root')).render(
      <InputArea inputRef={React.createRef()} fileInputRef={React.createRef()}
        send={() => { window.sent = true; }} handleFileSelect={() => {}} removeFile={() => {}}
        projectComposer={project} forceSendMode={project} />
    );
  `, resolveDir: process.cwd(), loader: 'tsx' },
  jsx: 'automatic', bundle: true, write: false, outdir: 'node_modules/.tmp/composer-browser', format: 'esm', external: ['/loader.gif', '/loader-done.png'],
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}', 'process.env.NODE_ENV': '"production"' },
  loader: { '.woff2': 'dataurl', '.woff': 'dataurl', '.ttf': 'dataurl', '.svg': 'dataurl', '.png': 'dataurl' },
});
const js = result.outputFiles.find(f => f.path.endsWith('.js')).text;
const css = result.outputFiles.find(f => f.path.endsWith('.css'))?.text || '';
const browser = await chromium.launch({ headless: true });
try {
  for (const [name, width, wrapper] of [
    ['home', 1280, 'jx-emptyPage--main"><div class="jx-homeInput'],
    ['chat', 1280, 'jx-chatMain'],
    ['project', 1280, 'jx-projectDetail-inputWrap'],
    ['home-mobile', 390, 'jx-emptyPage--main"><div class="jx-homeInput'],
    ['project-mobile', 390, 'jx-projectDetail-inputWrap'],
  ]) {
    const page = await browser.newPage({ viewport: { width, height: 800 } });
    page.on('pageerror', error => console.error(error.message));
    await page.route('**/*', route => {
      if (route.request().resourceType() === 'document') return route.fulfill({
        contentType: 'text/html',
        body: '<html><head><style>' + css + '</style></head><body><div class="' + wrapper +
          '" style="max-width:840px;margin:80px auto"><div id="root"></div></div></div></body></html>',
      });
      return route.fulfill({ contentType: 'application/json', body: '{"code":0,"data":[]}' });
    });
    await page.goto('http://composer.test/?' + name);
    await page.addScriptTag({ content: js, type: 'module' });
    const editor = page.locator('.jx-composerEditor');
    await editor.waitFor();
    await editor.click();
    for (let line = 0; line < 14; line++) {
      await page.keyboard.insertText('Line ' + line + ' long input text');
      await page.keyboard.press('Shift+Enter');
    }
    await page.waitForTimeout(80);
    const assertCaretVisible = async (step) => {
    await page.waitForTimeout(50);
    const visible = await page.evaluate(() => {
      const editor = document.querySelector('.jx-composerEditor');
      const range = getSelection().getRangeAt(0).cloneRange();
      const marker = document.createElement('span');
      marker.textContent = '\u200b';
      range.insertNode(marker);
      const caret = marker.getBoundingClientRect();
      marker.remove();
      const box = editor.getBoundingClientRect();
      const toolbar = document.querySelector('.jx-composerBar').getBoundingClientRect();
      return { top: caret.top, bottom: caret.bottom, editorTop: box.top, toolbarTop: toolbar.top, scrollTop: editor.scrollTop };
    });
    assert.ok(visible.bottom > 0 && visible.bottom <= visible.toolbarTop && visible.top >= visible.editorTop,
      name + ' ' + step + ': caret must be visible above toolbar: ' + JSON.stringify(visible));
    };
    await assertCaretVisible('newline');
    await page.keyboard.insertText('Last line after newline');
    await assertCaretVisible('typing');
    await page.keyboard.press('Control+Home');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('End');
    await page.keyboard.press('Shift+Enter');
    await page.keyboard.insertText('Editing near the beginning');
    await assertCaretVisible('middle edit');
    assert.ok(await editor.evaluate(el => el.scrollTop < el.scrollHeight - el.clientHeight - 50),
      name + ': editing earlier text must not jump to the end');
    await page.keyboard.press('Control+End');
    await page.evaluate(() => {
      const clipboardData = new DataTransfer();
      clipboardData.setData('text/plain', '\n' + 'Pasted line\n'.repeat(15));
      document.querySelector('.jx-composerEditor').dispatchEvent(
        new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
    });
    await assertCaretVisible('paste');
    await editor.evaluate(el => { el.scrollTop = 0; });
    await editor.dispatchEvent('pointerup');
    await page.keyboard.press('Shift');
    await page.waitForTimeout(80);
    assert.equal(await editor.evaluate(el => el.scrollTop), 0,
      name + ': reading older text must not snap back without input');
    assert.equal(await page.evaluate(() => !!window.sent), false, 'Shift+Enter must not send');
    await page.keyboard.press('Control+End');
    await editor.dispatchEvent('compositionstart', { data: '' });
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => !!window.sent), false, 'IME Enter must not send');
    await page.keyboard.insertText('中文候选');
    await editor.dispatchEvent('compositionend', { data: '中文候选' });
    await assertCaretVisible('composition end');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => !!window.sent), true, 'Enter after composition must send');
    console.log(name + ': newline, typing, middle edit, paste, manual scroll, composition events and send checks passed');
    await page.close();
  }
} finally {
  await browser.close();
}
