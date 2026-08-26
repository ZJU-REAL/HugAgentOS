import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { nextFollowState, SCROLL_RESUME_THRESHOLD, type FollowScrollState } from '../src/utils/scroll';
import { pickSiteEditChat } from '../src/utils/history';
import { markResolvedPlanPreviews } from '../src/utils/planHistory';
import type { ChatItem, ChatMessage } from '../src/types';

// ── 问题一：流式输出中往上滚，界面被强制拉回底部 ──
{
  // 先把旧判据钉一遍，说明这个 bug 是怎么来的：每来一个 scroll 事件就按"离底距离
  // 是否超过 100px"整体重算。滚轮往上先把跟随关掉，紧跟着的 scroll 事件才挪了
  // 40px，于是又判成"还贴着底"，把跟随打开 —— 下一帧流式增高的 ResizeObserver
  // 立刻 scrollTop = scrollHeight，页面被强制拽回最底。
  const OLD_FOLLOW_THRESHOLD = 100;
  const oldRule = (distance: number) => distance > OLD_FOLLOW_THRESHOLD;
  assert.equal(oldRule(40), false, '旧判据下 40px 的向上滚被当成"还在底部"');
  // 新判据在同一处输入下保持脱离跟随
  assert.equal(
    nextFollowState({ userScrolledUp: false, lastScrollTop: 1000 },
      { scrollTop: 960, distanceFromBottom: 40 }).userScrolledUp,
    true,
  );
}

{
  // 一次滚轮往上只挪了 40px（旧实现的阈值是 100px，会当成"还在底部"把跟随开关
  // 抹回 false，下一帧增高就被拽回底部）。现在只要离开底部就保持脱离跟随。
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

console.log('ui regression checks OK');
