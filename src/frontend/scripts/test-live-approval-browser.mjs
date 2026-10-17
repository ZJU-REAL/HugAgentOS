import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({
  stdin: { contents: `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {reduceControls} from './src/hooks/chatStream/controls';
import {FileConfirmBar} from './src/components/chat/FileConfirmBar';
import {useUIStore, useChatStore} from './src/stores';
useChatStore.setState({currentChatId:'chat'});
window.enqueue = id => useUIStore.getState().enqueuePendingConfirm('chat', {
  confirmId:id, op:'delete', logicalPath:'/myspace/a.txt', kind:'myspace', message:'Delete'});
window.resolve = id => reduceControls({chatId:'chat'}, {confirm_id:id,expired:true,resolved:true},{},'file_confirm');
window.pending = () => useUIStore.getState().pendingConfirm.chat ?? [];
createRoot(document.getElementById('root')).render(<FileConfirmBar/>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  bundle:true, write:false, format:'esm', jsx:'automatic',
  define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN"}'},
  loader:{'.css':'empty','.woff':'dataurl','.woff2':'dataurl','.svg':'dataurl','.png':'dataurl'},
});
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const decisions=[];
  const errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.route('**/*', async route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/') return route.fulfill({contentType:'text/html',
      body:'<div id="root"></div><script type="module" src="/fixture.js"></script>'});
    if(url.pathname==='/fixture.js') return route.fulfill({
      contentType:'application/javascript',body:result.outputFiles[0].text});
    if(url.pathname.endsWith('/file-confirm')) {
      decisions.push(JSON.parse(route.request().postData()));
      return route.fulfill({json:{code:200,data:{ok:true}}});
    }
    return route.fulfill({json:{code:200,data:{mode:'full'}}});
  });
  await page.goto('http://approval.test/');
  await page.waitForFunction(()=>typeof window.enqueue==='function');
  await page.evaluate(()=>window.enqueue('module-prompt'));
  await page.getByRole('alertdialog').waitFor();
  assert.equal(decisions.length,0,'browser must never replace scoped policy with account full');
  await page.evaluate(()=>window.resolve('module-prompt'));
  await page.waitForFunction(()=>window.pending().length===0);
  await page.getByRole('alertdialog').waitFor({state:'detached'});
  assert.equal(await page.getByText('一项「我的空间」写确认已超时取消，如仍需要请重新发起。').count(),0);
  assert.equal(decisions.length,0,'server resolution needs no browser approval POST');
  await page.evaluate(()=>window.enqueue('human-prompt'));
  await page.getByRole('alertdialog').waitFor();
  await page.getByRole('button',{name:/拒绝|Deny/}).click();
  await page.waitForFunction(()=>window.pending().length===0);
  assert.equal(decisions.length,1,'only a human choice sends an approval decision');
  assert.deepEqual(errors,[]);
  console.log('approval browser: scoped prompt waits; server resolution clears silently; human choice posts');
} finally { await browser.close(); }
