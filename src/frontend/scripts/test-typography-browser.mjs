import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/typography');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
    import 'antd/dist/reset.css';
    import './src/index.css';
    import './src/styles';
    import { createRoot } from 'react-dom/client';
    import { Button } from 'antd';
    import { AppThemeProvider } from './src/AppThemeProvider';
    createRoot(document.getElementById('component')).render(<AppThemeProvider><Button>组件文字</Button></AppThemeProvider>);
  `, resolveDir: process.cwd(), loader: 'tsx' },
  bundle: true, format: 'esm', jsx: 'automatic', define: {'import.meta.env':'{}'}, outfile: resolve(output, 'fixture.js'),
  loader: { '.woff2': 'dataurl', '.woff': 'dataurl', '.ttf': 'dataurl', '.svg': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
});
const html = `<html><head><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"></head>
<body><div class="jx-moduleSidebar">
<button class="jx-newChatBtn">新建对话</button>
<div class="jx-projectRow"><button class="jx-projectRowToggle"><span class="jx-projectRowName">项目标题 Project</span></button></div>
<div class="jx-projectRow active"><button class="jx-projectRowToggle"><span class="jx-projectRowName">选中项目</span></button></div>
<div class="jx-historyItem"><span class="jx-historyTitle">会话标题 Conversation 很长的会话标题应该保持省略而不换行</span></div>
<div class="jx-historyItem active"><span class="jx-historyTitle">选中会话</span></div>
</div><div class="jx-msg assistant"><div class="jx-bubble">正文内容</div></div>
<div class="jx-emptyCenter"><div class="jx-heroBg">首页文字</div></div>
<div id="component"></div>
<code>const value = 1;</code><script type="module" src="/fixture.js"></script></body></html>`;
const browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM} : {})});
try {
  for (const [platform, userAgent, expected, first] of [
    ['MacIntel', 'Macintosh', 'apple', '"PingFang SC"'],
    ['Win32', 'Windows NT 10.0', 'windows', '"Microsoft YaHei UI"'],
    ['Linux x86_64', 'Linux', 'other', '"Noto Sans CJK SC"'],
    ['iPhone', 'iPhone', 'apple', '"PingFang SC"'],
    ['', 'Mozilla/5.0 (Windows NT 10.0)', 'windows', '"Microsoft YaHei UI"'],
  ]) {
    const page = await browser.newPage();
    await page.addInitScript(({platform,userAgent}) => {
      Object.defineProperty(navigator,'platform',{get:()=>platform});
      Object.defineProperty(navigator,'userAgent',{get:()=>userAgent});
    }, {platform,userAgent});
    await page.route('http://typography.test/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({body:await readFile(resolve(output,path.slice(1))),contentType:path.endsWith('.css')?'text/css':'text/javascript'});
      return route.fulfill({body:html,contentType:'text/html'});
    });
    await page.goto('http://typography.test/');
    await page.locator('#component .ant-btn').waitFor();
    for (const theme of ['light','dark']) for (const width of [390,1440]) {
      await page.setViewportSize({width,height:900});
      await page.evaluate(theme => { document.documentElement.dataset.theme=theme; }, theme);
      const result = await page.evaluate(() => {
        const style = selector => { const el=document.querySelector(selector), s=getComputedStyle(el); return {family:s.fontFamily,size:s.fontSize,weight:s.fontWeight,line:s.lineHeight,overflow:s.textOverflow,whiteSpace:s.whiteSpace}; };
        return {platform:document.documentElement.dataset.fontPlatform,root:style('html'),
          titles:['.jx-projectRow:not(.active) .jx-projectRowName','.jx-historyItem:not(.active) .jx-historyTitle'].map(style),
          selected:['.jx-projectRow.active .jx-projectRowName','.jx-historyItem.active .jx-historyTitle'].map(style),
          button:style('.jx-newChatBtn'),other:['.jx-bubble','.jx-emptyCenter','.jx-heroBg','#component .ant-btn'].map(style),code:style('code')};
      });
      assert.equal(result.platform,expected);
      assert.ok(result.root.family.startsWith(first),result.root.family);
      for(const title of [...result.titles,...result.selected,result.button]) {
        assert.equal(title.size,'14px'); assert.equal(title.line,'20px'); assert.equal(title.family,result.root.family);
      }
      for(const title of result.titles) {assert.equal(title.weight,'400');assert.equal(title.overflow,'ellipsis');assert.equal(title.whiteSpace,'nowrap');}
      for(const title of [...result.selected,result.button]) assert.equal(title.weight,'500');
      for(const text of result.other) assert.equal(text.family,result.root.family);
      assert.ok(result.code.family.startsWith('ui-monospace'));
    }
    await page.close();
  }
  console.log('PASS: platform priority, shared fonts, sidebar hierarchy, themes and responsive widths (20 combinations)');
} finally {await browser.close();}

