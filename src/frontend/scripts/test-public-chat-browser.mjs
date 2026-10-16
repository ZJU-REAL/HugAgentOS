import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import('playwright');
const fixture = resolve('node_modules/.tmp/public-chat-browser');
await mkdir(fixture, { recursive: true });
await build({stdin:{contents:`
import React from 'react';
import { createRoot } from 'react-dom/client';
import { createPublicChatEntry } from './src/public-chat/publicChatEntry';
import { AppThemeProvider } from './src/AppThemeProvider';
import 'antd/dist/reset.css';
import './src/index.css';
import './src/styles';
const Entry=createPublicChatEntry();
createRoot(document.getElementById('root')).render(<AppThemeProvider><Entry /></AppThemeProvider>);
`,resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(fixture,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',
define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN"}'},
external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl','.png':'dataurl','.svg':'dataurl','.md':'text'}});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
const token = 'A'.repeat(43);
const shots = resolve('node_modules/.tmp/public-chat-evidence');
await mkdir(shots, { recursive: true });
const mime = path => path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.svg') ? 'image/svg+xml' : path.endsWith('.png') ? 'image/png' : 'text/html';
try {
  for (const kind of ['agents','projects']) {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 }, reducedMotion: 'reduce' });
    page.setDefaultTimeout(12000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const calls = [];
    let chatId, sent = false, failHistory = false, disabled = false;
    const sessions = () => chatId ? [{ chat_id: chatId, title: '测试问题', created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:01Z', metadata: kind === 'agents' ? { agent_id:'resource',agent_name:'政策助手' } : {}, project_id: kind === 'projects' ? 'resource' : null }] : [];
    await page.route('https://public-chat.test/**', async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      if (path.startsWith('/api/')) {
        calls.push({ path, headers: req.headers() });
        assert.equal(req.headers()['x-public-chat'], token);
        assert.match(req.headers()['x-public-visitor'], /^[a-f0-9]{48}$/);
        if (path === '/api/v1/public-chats/session' && disabled) return route.fulfill({status:401,json:{detail:'disabled'}});
        if (path === '/api/v1/public-chats/session') return route.fulfill({ json: { code:10000,data:{ id:'resource',kind,name:'政策助手',description:'面向当前应用的专属对话',suggested_questions:['有哪些扶持政策？'],visitor_id:'visitor-'+kind,chats:sessions() } } });
        if (path === '/api/v1/agents/responses') {
          const body = req.postDataJSON();
          assert.deepEqual(Object.keys(body).sort(), ['attachments','chat_mode','chat_id','message','stream',kind === 'agents' ? 'agent_id' : 'project_id'].sort());
          assert.equal(body.message,'测试问题'); chatId=body.chat_id; sent=true;
          return route.fulfill({ contentType:'text/event-stream',body:
            'data: '+JSON.stringify({type:'run_started',run_id:'run-1',message_id:'answer-1'})+'\n\n'+
            'data: '+JSON.stringify({type:'content',delta:'</think>这是独立页面的回复。'})+'\n\n'+
            'data: '+JSON.stringify({type:'meta',message_id:'answer-1'})+'\n\ndata: [DONE]\n\n' });
        }
        if (path.endsWith('/active-run')) return route.fulfill({json:{code:10000,data:null}});
        if (path.endsWith('/messages')) {
          if (failHistory) return route.fulfill({status:503,json:{detail:'temporary'}});
          return route.fulfill({json:{code:10000,data:{items:[
            {message_id:'user-1',role:'user',content:'测试问题',created_at:'2026-10-14T00:00:00Z',chat_seq:1},
            {message_id:'answer-1',role:'assistant',content:'这是独立页面的回复。',created_at:'2026-10-14T00:00:01Z',chat_seq:2},
          ],pagination:{has_next:false}}}});
        }
        if (path === '/api/v1/chats/'+chatId) return route.fulfill({json:{code:10000,data:sessions()[0]}});
        // Background optional UI reads may fail closed; they must never return account data.
        return route.fulfill({status:403,json:{detail:'scope denied'}});
      }
      if (path.startsWith('/chat/')) return route.fulfill({contentType:'text/html',body:'<div id="root"></div><link rel="stylesheet" href="/fixture.css"><script type="module" src="/fixture.js"></script>'});
      if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({contentType:mime(path),body:await readFile(resolve(fixture,path.slice(1)))});
      const asset = path.slice(1);
      const body = await readFile(resolve('dist',asset)).catch(()=>null);
      return route.fulfill({status:body?200:404,contentType:mime(asset),body:body||''});
    });
    await page.goto(`https://public-chat.test/chat/${kind}/${token}`);
    await page.locator('.jx-richComposer[contenteditable=true]').waitFor();
    await page.locator('h1').filter({hasText:'政策助手'}).waitFor();
    assert.equal(await page.getByText('我的空间',{exact:true}).count(),0);
    assert.equal(await page.getByText('能力中心',{exact:true}).count(),0);
    assert.equal(await page.title(),'政策助手');
    await page.screenshot({path:resolve(shots,kind+'-desktop.png')});
    await page.locator('.jx-richComposer[contenteditable=true]').fill('测试问题');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.getByText('这是独立页面的回复。',{exact:true}).waitFor();
    assert.equal(sent,true);
    await page.waitForURL(/\/c\//);
    await page.reload();
    await page.getByText('这是独立页面的回复。',{exact:true}).waitFor();
    await page.screenshot({path:resolve(shots,kind+'-after-refresh.png')});
    await page.locator('.jx-publicTopbar .ant-btn').last().click();
    await page.locator('h1').filter({hasText:'政策助手'}).waitFor();
    await page.locator('.jx-publicHistory button').first().click();
    await page.getByText('这是独立页面的回复。',{exact:true}).waitFor();
    await page.waitForURL(/\/c\//);
    await page.goBack();
    await page.screenshot({path:resolve(shots,kind+'-back.png')});
    await page.locator('h1').filter({hasText:'政策助手'}).waitFor();
    await page.goForward();
    await page.getByText('这是独立页面的回复。',{exact:true}).waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.locator('.jx-publicMenu').click();
    await page.locator('.jx-publicSidebar.is-open').waitFor();
    await page.screenshot({path:resolve(shots,kind+'-mobile-menu.png')});
    await page.locator('.jx-publicSidebarClose').click();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'no horizontal overflow');
    await page.emulateMedia({colorScheme:'dark'});
    await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
    await page.screenshot({path:resolve(shots,kind+'-mobile-dark.png')});
    assert.deepEqual(errors,[]);
    assert.equal(calls.some(call => /session\/check|\/v1\/me$/.test(call.path)),false,'public entry must not initialize account session');
    assert.equal(new Set(calls.map(call=>call.headers['x-public-visitor'])).size,1,'visitor survives refresh');
    disabled = true;
    await page.reload();
    await page.getByText('链接不可用或已停用',{exact:true}).waitFor();
    disabled = false;
    await page.getByRole('button',{name:/重\s*试/}).click();
    await page.getByText('这是独立页面的回复。',{exact:true}).waitFor();
    await page.close();
  }
  console.log('Public agent/project browser checks passed: send, refresh, history, back/forward, mobile, dark mode');
} finally { await browser.close(); }
