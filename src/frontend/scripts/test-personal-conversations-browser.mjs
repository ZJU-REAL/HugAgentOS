import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/personal-conversations-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import { PersonalAgentPanel } from './src/components/personal-agent/PersonalAgentPanel';
import './src/styles/variables.css';
createRoot(document.getElementById('root')).render(<ConfigProvider theme={{token:{motion:false}}}><div style={{height:'100vh'}}><PersonalAgentPanel /></div></ConfigProvider>);
`, loader: 'tsx', resolveDir: process.cwd() },
  loader: { '.woff2':'dataurl', '.woff':'dataurl', '.ttf':'dataurl' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') },
});
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  page.setDefaultTimeout(12000);
  const errors = [], sent = [];
  page.on('pageerror', error => errors.push(error.message));
  const origin = 'http://127.0.0.1:18505';
  const agent = { id:'agent', chat_id:'main', status:'ready', config:{name:'Hugo'}, computer_status:'not_provisioned' };
  const channels = [
    { chat_id:'alice', title:'o9cq80xoZU1tVg4KqBPhEE6Trzfg', channel_type:'weixin', channel_id:'wx', chat_type:'p2p', bot_name:'我的微信', preview:'Alice 的消息' },
    { chat_id:'group', title:'项目讨论群', channel_type:'lark', channel_id:'lark', chat_type:'group', bot_name:'工作机器人', preview:'群里的消息' },
  ];
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname;
    if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({
      contentType:path.endsWith('.js')?'text/javascript':'text/css',body:await readFile(resolve(output,path.slice(1))) });
    if (!path.startsWith('/api/')) return route.fulfill({contentType:'text/html',body:'<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>'});
    let data = {};
    const selected = url.searchParams.get('chat_id') || 'main';
    if (path.endsWith('/initialize')) data = agent;
    else if (path.endsWith('/monitor')) data = { agent, tasks:[], turns:[], devices:[], schedules:[], outputs:{total:0,images:0,documents:0,other:0,items:[],has_more:false}, memory:{enabled:false,write_enabled:false,profile:''} };
    else if (path.endsWith('/conversations')) data = channels;
    else if (path.endsWith('/messages')) {
      if (req.method() === 'POST') { sent.push({ chat:selected, ...req.postDataJSON() }); data = {message_id:'sent',status:'pending',work_item_ids:[]}; }
      else data = [{id:selected+'-message',seq:1,role:'user',content:{main:'网页里的私人内容',alice:'Alice 独立会话的内容',group:'项目群独立会话的内容'}[selected],work_item_ids:[],task_links:[]}];
    } else if (path.endsWith('/channels')) data = [
      ...channels.map(item => ({channel_id:item.channel_id,channel_type:item.channel_type,display_name:item.bot_name,enabled:true,status:'connected',personal_agent:true})),
      {channel_id:'ding',channel_type:'dingtalk',enabled:true,status:'connected',personal_agent:true},
      {channel_id:'wecom',channel_type:'wecom',enabled:true,status:'connected',personal_agent:true},
    ];
    else if (path.includes('/channel-bots')) data = [];
    return route.fulfill({json:{code:10000,message:'Success',data}});
  });
  await page.goto(origin);
  async function selectChannel(type) {
    await page.getByRole('button',{name:'接入频道',exact:true}).click();
    await page.locator('[data-channel="'+type+'"] button').filter({hasText:'渠道会话'}).click();
  }

  const log = page.getByRole('log'), input = page.getByRole('textbox', { name:'发送给 Personal Agent' });
  await log.getByText('网页里的私人内容',{exact:true}).waitFor();
  assert.equal(await page.locator('.pa-conversations').count(),0);
  assert.equal(await page.getByRole('button',{name:'网页主对话',exact:true}).count(),0);
  await page.getByRole('button',{name:'接入频道',exact:true}).click();
  await page.getByRole('button',{name:'渠道会话',exact:true}).first().waitFor();
  assert.equal(await page.locator('.pa-channel-card').count(),4);
  assert.equal(await page.getByText('o9cq80xoZU1tVg4KqBPhEE6Trzfg').count(),0);
  await page.waitForTimeout(300);
  await page.screenshot({path:resolve(output,'channel-cards.png')});
  await page.getByRole('button',{name:'Close',exact:true}).click();
  await input.fill('网页草稿');
  await selectChannel('weixin');
  await log.getByText('Alice 独立会话的内容',{exact:true}).waitFor();
  assert.equal(await log.getByText('网页里的私人内容').count(),0);
  assert.equal(await input.inputValue(),'');
  await input.fill('给 Alice 的回复');
  const posted = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/messages'));
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await posted;
  assert.equal(sent.at(-1).chat,'alice');
  assert.equal(sent.at(-1).content,'给 Alice 的回复');
  await selectChannel('lark');
  await log.getByText('项目群独立会话的内容',{exact:true}).waitFor();
  assert.equal(await log.getByText('Alice 独立会话的内容').count(),0);
  await page.screenshot({path:resolve(output,'channel-conversations.png')});
  assert.deepEqual(errors,[]);
  console.log('PASS: real panel switches isolated histories, routes replies to selected session, and hides the web conversation button.');
} finally { await browser.close(); }
