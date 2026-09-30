/**
 * 会话地址（react-router）：真浏览器里跑一遍「地址 ↔ 状态」两个方向的同步。
 *
 * 用真的 appRouter / chatStore / catalogStore，只把 App 外壳换成空组件——被验的是路由
 * 接线，不是聊天界面本身。
 */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const playwright = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const chromium = playwright.chromium ?? playwright.default?.chromium;

const output = resolve('node_modules/.tmp/chat-routing');
await mkdir(output, { recursive: true });

const stubApp = {
  name: 'stub-app',
  setup(build) {
    build.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'stub-app', namespace: 'stub' }));
    build.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
      contents: `
import { useEffect } from 'react';
import { usePanel } from './src/routing/usePanel';
export default function App() {
  const panel = usePanel();
  useEffect(() => { window.__panelFromHook = panel; }, [panel]);
  return null;
}`,
      loader: 'jsx',
      resolveDir: process.cwd(),
    }));
  },
};

await build({
  stdin: {
    contents: `
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';
import { createAppRouter } from './src/routing/routes';
import { bindRouter } from './src/routing/navigation';
import { useChatStore, useCatalogStore, useAuthStore } from './src/stores';
import { panelFromPath } from './src/routing/navigation';
import { abilitySlug, abilityTabFromSubs, kbTabFrom, mySpaceTabFromSubs } from './src/routing/subPages';
import { useCatalogStore as catalog } from './src/stores/catalogStore';
import { useMySpaceStore } from './src/stores/mySpaceStore';

const chat = (id, messages) => ({
  id, title: id, createdAt: 1, updatedAt: 1, messages, favorite: false, pinned: false,
  businessTopic: '综合咨询',
});

useChatStore.setState({
  currentUserId: 'u1',
  store: { chats: { 'chat-a': chat('chat-a', [{ role: 'user', content: 'a' }]), 'chat-b': chat('chat-b', [{ role: 'user', content: 'b' }]) }, order: ['chat-a', 'chat-b'] },
  backendSessionIds: new Set(['chat-a', 'chat-b']),
});

window.__routing = {
  authenticate: () => useAuthStore.setState({authUser: {user_id:'u1',username:'tester'},authChecking:false}),
  title: (id) => useChatStore.getState().store.chats[id]?.title,
  owner: (id) => useChatStore.getState().store.chats[id]?.automationTaskId,
  mode: () => ({slug:useChatStore.getState().modeSlug,effort:useChatStore.getState().chatMode}),

  chatId: () => useChatStore.getState().currentChatId,
  panel: () => window.__panelFromHook,
  panelFromUrl: () => panelFromPath(),
  select: (id) => useChatStore.getState().setCurrentChatId(id),
  newChat: () => useChatStore.getState().newChat(),
  setPanel: (p) => useCatalogStore.getState().setPanel(p),
  setAbilityTab: (t) => catalog.getState().setPanel('ability_center', abilitySlug(t)),
  setKbTab: (t) => catalog.getState().setKbTab(t),
  setMySpaceTab: (t) => useMySpaceStore.getState().setTab(t),
  subs: () => ({
    ability: abilityTabFromSubs(location.pathname.split('/').filter(Boolean).slice(1)),
    myspace: mySpaceTabFromSubs(location.pathname.split('/').filter(Boolean).slice(1)),
    kb: kbTabFrom(),
  }),
  // 启动阶段的任意一次写库（拉回服务端会话列表等）。此时开着的是一段还没发过消息的
  // 新草稿，地址必须仍停在首页。
  touchStore: () => useChatStore.getState().updateStore((s) => ({ ...s })),

  // 复现「新对话页变成 /c/<草稿id>」：草稿 id 登记的那张浏览器名单有数量上限，
  // 旧 id 会被挤掉。名单里查不到时，这段空白对话依然不该获得地址。
  forgetDraftRegistry: () => {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith('hugagent_ui_draft_chat_ids_v1')) localStorage.removeItem(key);
    }
  },

  // 流式输出期间每来一段文字都会写一次库，模拟之。
  streamTick: (id) => useChatStore.getState().updateStore((s) => ({
    ...s,
    chats: { ...s.chats, [id]: { ...s.chats[id], messages: [...(s.chats[id]?.messages || []), { role: 'assistant', content: '…' }] } },
  })),

  // 与真实发送路径一致：往 store 里写第一条消息，地址升级由 updateStore 自己完成
  publish: () => {
    const id = useChatStore.getState().currentChatId;
    useChatStore.getState().updateStore((s) => ({
      chats: { ...s.chats, [id]: chat(id, [{ role: 'user', content: 'x' }]) },
      order: [id, ...s.order],
    }));
    return id;
  },
};

const appRouter = createAppRouter();
bindRouter(appRouter);
createRoot(document.getElementById('root')).render(<RouterProvider router={appRouter} />);
`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile: resolve(output, 'fixture.js'),
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  plugins: [stubApp],
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.css': 'empty' },
});

const origin = 'http://127.0.0.1:5199';
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}) });
try {
  const page = await browser.newPage();
  const errors = [];
  const taskSessionRequests = [];
  page.on('pageerror', (e) => { errors.push(e.message); console.error('browser error:', e.message); });
  await page.context().route(origin + '/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js') {
      return route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')) });
    }
    if (url.pathname === '/api/v1/chats/task-chat') {
      taskSessionRequests.push(url.pathname);
      return route.fulfill({json:{code:10000,data:{chat_id:'task-chat',title:'Scheduled result',metadata:{automation_task_id:'task-one',automation_run:true,mode_slug:'deep-research',thinking_effort:'high'},created_at:'2026-09-30T10:00:00Z',updated_at:'2026-09-30T10:00:00Z'}}});
    }
    if (url.pathname === '/api/v1/automations/task-one') return route.fulfill({json:{code:10000,data:{task_id:'task-one',name:'Daily report',status:'active'}}});
    if (url.pathname === '/api/v1/automations/task-one/runs') return route.fulfill({json:{code:10000,data:[{run_id:'run-one',task_id:'task-one',chat_id:'task-chat',status:'completed',started_at:'2026-09-30T10:00:00Z'}]}});
    if (url.pathname.startsWith('/api')) return route.fulfill({ json: { code: 10000, data: {} } });
    // nginx / 桌面端本地代理都是这个行为：静态资源没命中就回落 index.html
    return route.fulfill({ contentType: 'text/html', body: '<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });

  const path = () => page.evaluate(() => location.pathname);
  const chatId = () => page.evaluate(() => window.__routing.chatId());
  const panel = () => page.evaluate(() => window.__routing.panel());

  // 1. 直接打开一段会话的链接 → 进去就停在这段会话
  await page.goto(origin + '/c/chat-a');
  await page.waitForFunction(() => !!window.__routing);
  assert.equal(await chatId(), 'chat-a', '打开 /c/chat-a 应落在 chat-a');
  assert.equal(await panel(), 'chat', 'usePanel 应从地址算出 chat');
  assert.equal(await page.evaluate(() => window.__routing.panelFromUrl()), 'chat');

  // 2. 切换会话 → 地址跟着换
  await page.evaluate(() => window.__routing.select('chat-b'));
  assert.equal(await path(), '/c/chat-b', '切到 chat-b 后地址应为 /c/chat-b');

  // 3. 后退 → 回到上一段会话（地址 → 状态方向）
  await page.goBack();
  await page.waitForFunction(() => window.__routing.chatId() === 'chat-a');
  assert.equal(await path(), '/c/chat-a');

  // 4. 前进 → 再回到 chat-b
  await page.goForward();
  await page.waitForFunction(() => window.__routing.chatId() === 'chat-b');

  // 5. 新对话停在首页，发出第一条消息后才换成自己的地址，且不留一条空白草稿的历史
  await page.evaluate(() => window.__routing.newChat());
  assert.equal(await path(), '/', '新对话应停在 /');
  const before = await page.evaluate(() => history.length);
  const published = await page.evaluate(() => window.__routing.publish());
  assert.equal(await path(), '/c/' + published, '首条消息后地址应变成这段会话');
  assert.equal(await page.evaluate(() => history.length), before, '首条消息应 replace，不新增历史条目');
  await page.goBack();
  await page.waitForFunction(() => window.__routing.chatId() === 'chat-b');

  // 6. 面板也各有地址
  await page.evaluate(() => window.__routing.setPanel('my_space'));
  assert.equal(await path(), '/my-space');
  await page.waitForFunction(() => window.__panelFromHook === 'my_space');
  await page.goBack();
  await page.waitForFunction(() => window.__routing.panel() === 'chat');
  assert.equal(await path(), '/c/chat-b');

  // 7. 不认识的地址回首页
  await page.goto(origin + '/no-such-page');
  await page.waitForFunction(() => location.pathname === '/');

  // 8. 新对话就是首页：冷启动后随便一次写库，都不能把空草稿写进地址
  await page.waitForFunction(() => !!window.__routing);
  await page.evaluate(() => window.__routing.newChat());
  assert.equal(await path(), '/');
  await page.evaluate(() => { window.__routing.forgetDraftRegistry(); window.__routing.touchStore(); });
  assert.equal(await path(), '/', '草稿被挤出名单后，新对话页仍必须停在 /，不能出现 /c/<草稿id>');

  // 9. 二级页各有地址：能力中心类别、我的空间模块、知识库分档
  await page.evaluate(() => window.__routing.setAbilityTab('mcp'));
  assert.equal(await path(), '/ability-center/connectors', '连接器应有自己的地址');
  assert.equal((await page.evaluate(() => window.__routing.subs())).ability, 'mcp');

  await page.evaluate(() => window.__routing.setMySpaceTab('favorites'));
  assert.equal(await path(), '/my-space/favorites', '我的空间各模块应有自己的地址');

  await page.evaluate(() => window.__routing.setMySpaceTab('kb'));
  await page.evaluate(() => window.__routing.setKbTab('private'));
  assert.equal(await path(), '/my-space/kb/private', '我的空间下的知识库分档应落到第三层地址');
  assert.equal((await page.evaluate(() => window.__routing.subs())).kb, 'private');

  // 10. 正在流式输出时切到别的页面，不许被拽回会话
  await page.evaluate(() => window.__routing.select('chat-a'));
  assert.equal(await path(), '/c/chat-a');
  await page.evaluate(() => window.__routing.setPanel('my_space'));
  assert.equal(await path(), '/my-space');
  await page.evaluate(() => { for (let i = 0; i < 5; i += 1) window.__routing.streamTick('chat-a'); });
  assert.equal(await path(), '/my-space', '流式输出期间切到别的页面，不能被强制切回会话');

  // 11. 开着多段会话：在看 A，B 在后台流式输出，地址必须待在 A
  await page.evaluate(() => window.__routing.select('chat-a'));
  await page.evaluate(() => { for (let i = 0; i < 5; i += 1) window.__routing.streamTick('chat-b'); });
  assert.equal(await path(), '/c/chat-a', '后台会话输出不能把地址切到它自己');

  // Task conversations wait for identity, then restore their own metadata and module.
  taskSessionRequests.length = 0;
  await page.goto(origin + '/automation/task-one/conversations/task-chat');
  await page.waitForFunction(() => !!window.__routing);
  await page.waitForTimeout(100);
  assert.equal(taskSessionRequests.length, 0, '认证完成前不能读取任务会话');
  await page.evaluate(() => window.__routing.authenticate());
  await page.waitForFunction(() => window.__routing.chatId() === 'task-chat');
  assert.equal(await panel(), 'automation');
  assert.equal(await page.evaluate(() => window.__routing.title('task-chat')), 'Scheduled result');
  assert.equal(await page.evaluate(() => window.__routing.owner('task-chat')), 'task-one');
  assert.deepEqual(await page.evaluate(() => window.__routing.mode()),{slug:'deep-research',effort:'high'},'元数据恢复后同步输入模式与思考强度');
  await page.evaluate(() => window.__routing.select('chat-a'));
  assert.equal(await path(), '/c/chat-a');
  await page.goBack();
  await page.waitForFunction(() => window.__routing.chatId() === 'task-chat');
  assert.equal(await panel(), 'automation');
  await page.reload();
  await page.waitForFunction(() => !!window.__routing);
  await page.evaluate(() => window.__routing.authenticate());
  await page.waitForFunction(() => window.__routing.chatId() === 'task-chat');
  assert.equal(await panel(), 'automation', '刷新任务会话仍留在定时任务模块');
  assert.deepEqual(errors, [], '不应有运行时报错');
  console.log('chat routing: 普通会话、模块地址与定时任务刷新/历史导航通过');
} finally {
  await browser.close();
}
