import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/personal-agent-name-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { usePersonalAgentName } from './src/personalAgentEdition';
import { useAuthStore } from './src/stores/authStore';
window.switchUser = (id) => useAuthStore.setState({authUser:id ? {user_id:id,can_use_personal_agent:true} : null});
function Name() { return <output>{usePersonalAgentName()}</output>; }
createRoot(document.getElementById('root')).render(<Name />);
`, loader:'tsx', resolveDir:process.cwd() },
  outfile:resolve(output,'fixture.js'), bundle:true, format:'esm', jsx:'automatic',
  loader:{'.woff2':'dataurl','.woff':'dataurl','.ttf':'dataurl'},
  define:{'import.meta.env':'{"VITE_DEFAULT_LANGUAGE":"zh-CN"}'},
  alias:{'@hugagent/workbench-protocol':resolve('../../packages/workbench-protocol/dist/index.js')},
});
const browser = await chromium.launch({headless:true});
try {
  for (const delayed of [false, true]) {
    const page = await browser.newPage();
    let owner = 'A', releaseA, requests = 0;
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://127.0.0.1:18506/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/fixture.js') return route.fulfill({contentType:'text/javascript',body:await readFile(resolve(output,'fixture.js'))});
      if (path === '/api/v1/personal-agent') {
        const selected = owner;
        requests++;
        if (selected === 'A' && delayed) await new Promise(resolve => { releaseA = resolve; });
        return route.fulfill({json:{code:10000,data:{id:selected,config:{name:selected+' private name'}}}});
      }
      if (path.startsWith('/api/')) return route.fulfill({json:{code:10000,data:{}}});
      return route.fulfill({contentType:'text/html',body:'<div id="root"></div><script type="module" src="/fixture.js"></script>'});
    });
    await page.goto('http://127.0.0.1:18506');
    const firstRequest = page.waitForRequest(req => req.url().endsWith('/personal-agent'));
    await page.evaluate(() => window.switchUser('A'));
    if (delayed) {
      await firstRequest;
    } else await page.getByText('A private name',{exact:true}).waitFor();
    owner = 'B';
    await page.evaluate(() => window.switchUser('B'));
    await page.getByText('B private name',{exact:true}).waitFor({timeout:3000});
    if (delayed) {
      releaseA();
      await page.waitForTimeout(100);
      assert.equal(await page.locator('output').textContent(),'B private name','old account response must be discarded');
    }
    assert.equal(requests,2);
    await page.evaluate(() => window.switchUser(null));
    assert.equal(await page.locator('output').textContent(),'');
    assert.deepEqual(errors,[]);
    await page.close();
  }
  console.log('PASS: account switch reloads the name and discards a previous account delayed response.');
} finally { await browser.close(); }
