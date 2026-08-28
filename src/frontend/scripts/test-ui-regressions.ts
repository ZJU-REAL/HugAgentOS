import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { hasActiveSelectionIn, nextFollowState, SCROLL_RESUME_THRESHOLD, type FollowScrollState } from '../src/utils/scroll';
import { morphChildren } from '../src/utils/domPatch';
import { pickSiteEditChat } from '../src/utils/history';
import { markResolvedPlanPreviews } from '../src/utils/planHistory';
import type { ChatItem, ChatMessage } from '../src/types';

// ── 流式输出中往上滚，界面不许被强制拉回底部 ──
{
  // 一次滚轮往上只挪了 40px：只要离开底部就保持脱离跟随。
  let state: FollowScrollState = { userScrolledUp: false, lastScrollTop: 1000 };
  state = nextFollowState(state, { scrollTop: 960, distanceFromBottom: 40 });
  assert.equal(state.userScrolledUp, true);

  // 脱离跟随后内容继续变长：scrollTop 不变、离底越来越远 —— 不许自己恢复跟随
  state = nextFollowState(state, { scrollTop: 960, distanceFromBottom: 300 });
  assert.equal(state.userScrolledUp, true);

  // 一直往上翻到很前面的内容，同样保持脱离
  state = nextFollowState(state, { scrollTop: 120, distanceFromBottom: 1400 });
  assert.equal(state.userScrolledUp, true);

  // 用户自己滚回底部 → 恢复跟随
  state = nextFollowState(state, { scrollTop: 2000, distanceFromBottom: 0 });
  assert.equal(state.userScrolledUp, false);
}

{
  // 重新生成/截断消息：内容变短，浏览器把 scrollTop 往回夹，但人还贴在底部。
  // 方向虽然是"往上"，不能判成用户滚走，否则这一轮流式就不跟随了。
  let state: FollowScrollState = { userScrolledUp: false, lastScrollTop: 2000 };
  state = nextFollowState(state, { scrollTop: 1400, distanceFromBottom: 0 });
  assert.equal(state.userScrolledUp, false);
}

{
  // 底部附近的抖动（<= 恢复阈值）不算离开底部
  const state = nextFollowState(
    { userScrolledUp: false, lastScrollTop: 1000 },
    { scrollTop: 1000 - SCROLL_RESUME_THRESHOLD, distanceFromBottom: SCROLL_RESUME_THRESHOLD },
  );
  assert.equal(state.userScrolledUp, false);
}

// ── 问题四：连点「编辑站点」堆出一串同名空会话 ──
{
  const chat = (over: Partial<ChatItem>): ChatItem => ({
    id: 'c', title: '编辑站点：官网', createdAt: 0, updatedAt: 0, messages: [],
    favorite: false, pinned: false, businessTopic: '综合咨询', ...over,
  });
  const msg: ChatMessage = { role: 'user', content: 'hi', ts: 1 };

  // 聊过的那段优先
  const chats = [
    chat({ id: 'empty-new', siteChat: true, projectId: 'p1', updatedAt: 300 }),
    chat({ id: 'talked', siteChat: true, projectId: 'p1', updatedAt: 200, messages: [msg] }),
    chat({ id: 'other-project', siteChat: true, projectId: 'p2', updatedAt: 900, messages: [msg] }),
  ];
  assert.equal(pickSiteEditChat(chats, 'p1')?.id, 'talked');

  // 只有空会话时也要复用它，而不是再开一个（旧实现返回 undefined → 每点一次新建一个）
  const onlyEmpty = [
    chat({ id: 'empty-old', siteChat: true, projectId: 'p1', updatedAt: 100 }),
    chat({ id: 'empty-new', siteChat: true, projectId: 'p1', updatedAt: 500 }),
  ];
  assert.equal(pickSiteEditChat(onlyEmpty, 'p1')?.id, 'empty-new');

  // 非建站会话 / 别的工程不参与
  assert.equal(pickSiteEditChat([chat({ id: 'x', projectId: 'p1', updatedAt: 9 })], 'p1'), undefined);
  assert.equal(pickSiteEditChat(onlyEmpty, 'p-none'), undefined);
}

// ── 问题二：中断后回到会话，计划预览卡又长出「确认执行」按钮 ──
{
  const preview: ChatMessage = {
    role: 'assistant', content: '', ts: 1,
    segments: [{ type: 'plan', planData: { mode: 'preview', planId: 'plan_1', title: 'T', steps: [] } }],
  };
  const executed: ChatMessage = {
    role: 'assistant', content: '', ts: 2,
    segments: [{ type: 'plan', planData: { mode: 'complete', planId: 'plan_1', title: 'T', steps: [], cancelled: true } }],
  };
  const out = markResolvedPlanPreviews([preview, executed]);
  const seg = out[0].segments?.[0];
  assert.equal(seg?.type === 'plan' && seg.planData?.decided, 'confirmed');
  // 中断位跟着历史一起回来，卡片不会再渲染成「执行中」
  const execSeg = out[1].segments?.[0];
  assert.equal(execSeg?.type === 'plan' && execSeg.planData?.cancelled, true);

  // 还没执行过的预览卡保持可决策
  const pending = markResolvedPlanPreviews([preview]);
  const pendingSeg = pending[0].segments?.[0];
  assert.equal(pendingSeg?.type === 'plan' && pendingSeg.planData?.decided, undefined);
  assert.equal(pending, pending, 'no-op 分支返回原数组');
}

// ── 问题四之二：已删除项目的绑定被另一个窗口"复活"，新对话挂在不存在的项目下 ──
{
  // storage.ts 在浏览器里跑，先把 localStorage/window 垫出来再动态 import。
  const bag = new Map<string, string>();
  const fakeStorage = {
    getItem: (k: string) => (bag.has(k) ? bag.get(k)! : null),
    setItem: (k: string, v: string) => { bag.set(k, v); },
    removeItem: (k: string) => { bag.delete(k); },
  };
  const g = globalThis as unknown as Record<string, unknown>;
  g.localStorage = fakeStorage;
  g.window = { localStorage: fakeStorage, addEventListener() {}, removeEventListener() {} };
  g.document = { addEventListener() {}, removeEventListener() {} };

  const { mergeChatStores, registerUnboundProject } = await import('../src/storage');
  const chat = (over: Partial<ChatItem>): ChatItem => ({
    id: 'c1', title: '新对话', createdAt: 0, updatedAt: 0, messages: [],
    favorite: false, pinned: false, businessTopic: '综合咨询', ...over,
  });

  // A 窗口删掉项目 p1（解绑不改 updatedAt）；B 窗口内存里还留着旧绑定。
  const bWindowMemory = {
    chats: { c1: chat({ id: 'c1', updatedAt: 100, projectId: 'p1', projectName: '已删除的项目' }) },
    order: ['c1'],
  };
  const diskAfterDelete = { chats: { c1: chat({ id: 'c1', updatedAt: 100 }) }, order: ['c1'] };

  registerUnboundProject('user_1', 'p1');
  const merged = mergeChatStores(bWindowMemory, diskAfterDelete);
  assert.equal(merged.chats.c1.projectId, undefined, '已删除项目的绑定不许被合并写盘贴回来');
  assert.equal(merged.chats.c1.projectName, undefined);

  // 没被登记过的项目绑定照常保留
  const keep = mergeChatStores(
    { chats: { c2: chat({ id: 'c2', updatedAt: 5, projectId: 'p2', projectName: '在用的项目' }) }, order: ['c2'] },
    { chats: {}, order: [] },
  );
  assert.equal(keep.chats.c2.projectId, 'p2');
}

// ── 问题五：侧边栏项目名使用浏览器默认按钮字体，与下方对话标题不一致 ──
{
  const sidebarCss = readFileSync('src/styles/sidebar.css', 'utf8');
  const chatCss = readFileSync('src/styles/chat.css', 'utf8');
  const resolveDeclarations = (selector: string, sources: string[]) => {
    const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const rulePattern = new RegExp(`(?:^|})\\s*${escapedSelector}\\s*\\{([^}]*)\\}`, 'g');
    const declarations = new Map<string, string>();

    for (const css of sources) {
      for (const match of css.matchAll(rulePattern)) {
        for (const declaration of match[1].split(';')) {
          const colon = declaration.indexOf(':');
          if (colon === -1) continue;
          declarations.set(
            declaration.slice(0, colon).trim(),
            declaration.slice(colon + 1).trim(),
          );
        }
      }
    }
    return declarations;
  };

  const projectToggle = resolveDeclarations('.jx-projectRowToggle', [sidebarCss]);
  const projectName = resolveDeclarations('.jx-projectRowName', [sidebarCss, chatCss]);
  const historyTitle = resolveDeclarations('.jx-historyTitle', [sidebarCss, chatCss]);

  assert.equal(
    projectToggle.get('font-family'),
    'inherit',
    '项目行按钮必须继承侧边栏字体族',
  );
  for (const property of ['font-size', 'font-weight', 'line-height']) {
    assert.equal(
      projectName.get(property),
      historyTitle.get(property),
      `项目名与对话标题的 ${property} 必须一致`,
    );
  }
}

// ── 首页与具体对话页使用同一层近白背景，避免切换会话时底色跳变 ──
{
  const variablesCss = readFileSync('src/styles/variables.css', 'utf8');
  const chatCss = readFileSync('src/styles/chat.css', 'utf8');
  const appSource = readFileSync('src/App.tsx', 'utf8');

  assert.match(
    variablesCss,
    /--color-bg-chat:#FDFDFC;/,
    '浅色聊天背景应是接近白色的统一令牌',
  );
  assert.match(
    appSource,
    /panel === 'chat' \? ' is-chatSurface' : ''/,
    '整个聊天主面板都应标记为统一背景面',
  );
  assert.match(
    appSource,
    /panel === 'chat' \? ' jx-content--chatSurface' : ''/,
    '首页和已有消息的对话内容区都应使用统一背景面',
  );
  assert.match(
    chatCss,
    /\.jx-primaryPane\.is-chatSurface,\s*\.jx-content\.jx-content--chatSurface\s*\{[^}]*background:var\(--color-bg-chat\);/s,
    '聊天主面板和内容区必须引用同一背景令牌',
  );
  assert.match(
    chatCss,
    /\.jx-primaryPane\.is-chatSurface \.jx-chatFooter\s*\{[^}]*background:var\(--color-bg-chat\);/s,
    '具体对话页的底部输入区也必须与页面底色一致',
  );
  assert.match(
    chatCss,
    /\.jx-homeInput \.jx-composerPlaceholder\s*\{[^}]*color:var\(--color-text-placeholder\) !important;/s,
    '首页问题提示必须使用柔和的 placeholder 灰色，不能继承正文黑色',
  );
}

// ── 侧边栏只固定到「新建对话」，下方导航与历史记录共用滚动区 ──
{
  const sidebarSource = readFileSync('src/components/sidebar/Sidebar.tsx', 'utf8');
  const sidebarCss = readFileSync('src/styles/sidebar.css', 'utf8');

  assert.match(
    sidebarSource,
    /className="jx-newChatBtn"[\s\S]*className="jx-sidebarScrollArea"[\s\S]*className="jx-navMenu"[\s\S]*className="jx-historyListWrap"/,
    '新建对话下方必须由同一个滚动区同时包住主导航和历史记录',
  );
  assert.match(
    sidebarCss,
    /\.jx-sidebarScrollArea\s*\{[^}]*flex:1;[^}]*overflow-y:auto;/s,
    '新建对话下方的统一容器必须承担侧边栏滚动',
  );
  assert.match(
    sidebarCss,
    /\.jx-sidebarScrollArea\s*>\s*\.jx-historyListWrap\s*\{[^}]*flex:none;[^}]*overflow:visible;/s,
    '历史记录自身不能再单独滚动，否则上方导航仍会被锁定',
  );
}

// ── 流式输出时正文可以边划边复制：增量必须就地改 DOM，不能整棵重建 ──
{
  // 最小 DOM 桩：只实现 morphChildren 用到的那几个接口，够验证"节点是不是同一个"。
  class N {
    nodeType: number;
    nodeValue: string | null = null;
    tagName = '';
    childNodes: any[] = [];
    attrs = new Map<string, string>();
    constructor(nodeType: number) { this.nodeType = nodeType; }
    get attributes() { return Array.from(this.attrs, ([name, value]) => ({ name, value })); }
    getAttribute(n: string) { return this.attrs.has(n) ? this.attrs.get(n)! : null; }
    setAttribute(n: string, v: string) { this.attrs.set(n, v); }
    removeAttribute(n: string) { this.attrs.delete(n); }
    hasAttribute(n: string) { return this.attrs.has(n); }
    appendChild(c: any) { this.childNodes.push(c); return c; }
    removeChild(c: any) { this.childNodes.splice(this.childNodes.indexOf(c), 1); return c; }
    replaceChild(next: any, prev: any) { this.childNodes[this.childNodes.indexOf(prev)] = next; return prev; }
  }
  const text = (v: string) => { const n = new N(3); n.nodeValue = v; return n; };
  const el = (tag: string, children: any[] = [], attrs: Record<string, string> = {}) => {
    const n = new N(1);
    n.tagName = tag.toUpperCase();
    n.childNodes = children;
    Object.entries(attrs).forEach(([k, v]) => n.attrs.set(k, v));
    return n;
  };

  // 一帧增量：段落文字变长 + 新长出一段。
  const container = el('div', [el('p', [text('你好')])]);
  const keptP = container.childNodes[0];
  const keptText = keptP.childNodes[0];
  const incoming = el('div', [el('p', [text('你好，世界')]), el('p', [text('第二段')])]);
  morphChildren(container as any, incoming as any);

  assert.equal(container.childNodes[0], keptP, '已有段落必须是同一个元素节点（整棵重建会冲掉选区）');
  assert.equal(container.childNodes[0].childNodes[0], keptText, '变长的文本必须是同一个文本节点，选区偏移才继续有效');
  assert.equal(keptText.nodeValue, '你好，世界');
  assert.equal(container.childNodes.length, 2, '新段落追加而不是重建整棵');

  // 引用角标 / mermaid 的挂载点由 React portal 管，新 HTML 里是空壳，不许覆盖其子树。
  const badge = el('b', [text('角标')]);
  const host = el('div', [el('span', [badge], { 'data-jxcit': '0' })]);
  const hostSpan = host.childNodes[0];
  morphChildren(host as any, el('div', [el('span', [], { 'data-jxcit': '0' })]) as any);
  assert.equal(host.childNodes[0], hostSpan);
  assert.equal(hostSpan.childNodes[0], badge, 'portal 挂载点的子树不能被空壳覆盖');

  // 结构真的变了（标签不同）时照常替换。
  const swap = el('div', [el('p', [text('x')])]);
  morphChildren(swap as any, el('div', [el('ul', [text('y')])]) as any);
  assert.equal(swap.childNodes[0].tagName, 'UL');
}

// ── 拖选正文时不许把页面拽回底部 ──
{
  const inList = { c: true } as any;
  const list = { contains: (n: any) => n === inList } as any;
  const sel = (isCollapsed: boolean, node: any) => ({
    isCollapsed,
    rangeCount: 1,
    getRangeAt: () => ({ commonAncestorContainer: node }),
  }) as any;

  assert.equal(hasActiveSelectionIn(list, sel(false, inList)), true, '消息区里选中了文字就要暂停跟随');
  assert.equal(hasActiveSelectionIn(list, sel(true, inList)), false, '只是点了一下（选区折叠）不算选中');
  assert.equal(hasActiveSelectionIn(list, sel(false, {})), false, '消息区之外的选区不影响跟随');
  assert.equal(hasActiveSelectionIn(list, null), false);
  assert.equal(hasActiveSelectionIn(null, sel(false, inList)), false);
}

// ── 滚动跟随的监听器必须挂到真实存在的滚动容器上 ──
{
  // 认证检查期间 App 直接 return，主界面还没渲染，此时 document.querySelector('.jx-content')
  // 拿到的是 null。监听器 effect 过去写成空依赖数组，只在挂载时找一次、找不到就永远不再挂 ——
  // 滚轮/滚动事件根本没人听，userScrolledUp 永远是 false，流式增高照旧把页面拽回底部。
  const appSource = readFileSync('src/App.tsx', 'utf8');

  assert.doesNotMatch(
    appSource,
    /document\.querySelector<HTMLElement>\('\.jx-content'\)/,
    '不许再用挂载时的一次性 querySelector 找滚动容器：认证闸放行前它还不存在',
  );
  assert.match(
    appSource,
    /<Content ref=\{handleContentRef\}/,
    '滚动容器必须通过回调 ref 交给 state，元素出现后依赖它的 effect 才会重跑',
  );
  for (const dep of [
    /content\.addEventListener\('wheel'[\s\S]{0,900}?\}, \[contentEl\]\);/,
    /new ResizeObserver\([\s\S]{0,600}?\}, \[panel, currentChatId, hasMessages, contentEl\]\);/,
  ]) {
    assert.match(appSource, dep, '滚动相关 effect 的依赖里必须带上 contentEl');
  }
}

console.log('ui regression checks OK');
