import { t } from '../src/i18n';
import { chatDraftKey, readComposer } from '../src/stores/composerStore';
/* eslint-disable react-refresh/only-export-components -- SSR test harness captures hook actions for event assertions without a browser renderer. */
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import React from 'react';
import { renderToString } from 'react-dom/server';
import { message } from 'antd';
import type { ComposerOptions } from '../src/components/chat/composerTypes';

const memory = new Map<string, string>();
const storage = {
  getItem: (key: string) => memory.get(key) ?? null,
  setItem: (key: string, value: string) => { memory.set(key, value); },
  removeItem: (key: string) => { memory.delete(key); },
};
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
Object.assign(globalThis, {
  localStorage: storage,
  window: {
    location: { pathname: '/', origin: 'http://localhost' },
    localStorage: storage, sessionStorage: storage, setTimeout, clearTimeout,
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    history: { pushState() {}, replaceState() {} },
  },
  document: { documentElement: {}, addEventListener() {}, removeEventListener() {} },
});
const { useChatStore } = await import('../src/stores/chatStore');
const { useComposerState } = await import('../src/components/chat/useComposerState');
const { useComposerSuggestions } = await import('../src/components/chat/useComposerSuggestions');
const { useComposerEditor } = await import('../src/components/chat/useComposerEditor');
const { createComposerKeyHandler } = await import('../src/components/chat/composerKeyboard');
const { classifyForkCommand } = await import('../src/utils/chatForkCommands');

assert.equal(classifyForkCommand(' \n/fork\t'), 'fork');
assert.equal(classifyForkCommand('/fork argument'), 'invalid');
assert.equal(classifyForkCommand('Explain /fork'), null);
assert.equal(classifyForkCommand('/forked'), null);

// SSR invokes the real hooks without firing unrelated catalog-loading effects.
let state!: ReturnType<typeof useComposerState>;
let suggestions!: ReturnType<typeof useComposerSuggestions>;
let editor!: ReturnType<typeof useComposerEditor>;
let generations = 0;
function Harness({ options }: { options: ComposerOptions }) {
  // Zustand's server snapshot intentionally stays at store initialization. Supply
  // the current chat snapshot to these browser event handlers explicitly.
  state = {
    ...useComposerState(options), ...useChatStore.getState(), ...readComposer(chatDraftKey('source')), draftKey:chatDraftKey('source'),
    _currentChat: useChatStore.getState().currentChat(),
  };
  suggestions = useComposerSuggestions(state, options);
  editor = useComposerEditor(state, suggestions, {
    inputRef: { current: null }, fileInputRef: { current: null },
    send: () => { generations += 1; }, projectComposer: options.projectComposer,
  });
  return null;
}

let postCount = 0;
let shouldFail = false;
let waitForResponse: Promise<void> | undefined;
const notices: unknown[] = [];
const originals = { success: message.success, warning: message.warning, error: message.error, info: message.info };
for (const level of ['success', 'warning', 'error', 'info'] as const) {
  message[level] = ((value: unknown) => { notices.push(value); return () => {}; }) as typeof message[typeof level];
}
globalThis.fetch = async (input, init) => {
  const url = String(input);
  if (url.endsWith('/fork')) {
    assert.equal(init?.method, 'POST');
    postCount += 1;
    const branchId = 'branch-' + postCount;
    await waitForResponse;
    if (shouldFail) return Response.json({ message: 'temporary failure' }, { status: 503 });
    return Response.json({ code: 200, data: {
      chat_id: branchId, title: 'Source · 分支', user_id: 'fork-composer-user',
      project_id: null, created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z',
      message_count: 2, metadata: { fork: {
        source_chat_id: 'source', source_message_id: 'reply', source_chat_seq: 2,
        source_title: 'Source', message_count: 2, created_at: '2026-09-26T00:00:00Z',
      } },
    } });
  }
  if (url.includes('/messages?')) {
    return Response.json({ code: 200, data: { items: [], pagination: { has_next: false } } });
  }
  throw new Error('Unexpected request: ' + url);
};
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));
function mount(input: string, projectComposer = false, projectId?: string) {
  const source = { id: 'source', title: 'Source', createdAt: 1, updatedAt: 1, messages: [], projectId };
  useChatStore.setState({
    currentUserId: 'fork-composer-user', currentChatId: 'source', sending: false,
    store: { chats: { source }, order: ['source'] }, backendSessionIds: new Set(['source']),
    planMode: false, loopMode: false,
  });
  readComposer(chatDraftKey('source')).setInput(input);
  renderToString(<Harness options={{ projectComposer, forceSendMode: projectComposer, disableMention: false, activeMode: null }} />);
}
function keyEvent(key = 'Enter', isComposing = false): React.KeyboardEvent<HTMLDivElement> {
  const event = {
    key, shiftKey: false, defaultPrevented: false,
    nativeEvent: { isComposing, keyCode: isComposing ? 229 : 13 },
    preventDefault() { this.defaultPrevented = true; },
  };
  return event as unknown as React.KeyboardEvent<HTMLDivElement>;
}

try {
  mount('/');
  const action = suggestions.slashEntries.find((entry) => entry.kind === 'chat_action');
  assert.ok(action, 'fork is offered independently from project-only /init');
  assert.equal(action.name, t('创建聊天分支'));
  editor.onSlashEntrySelect(action);
  await settle();
  assert.equal(postCount, 1);
  assert.equal(generations, 0);
  assert.equal(useChatStore.getState().currentChatId, 'branch-1', JSON.stringify(notices));
  assert.equal(readComposer().input, '');

  mount('/fork', false, 'project-1');
  assert.ok(suggestions.slashEntries.some((entry) => entry.kind === 'chat_action'), 'existing project conversations support forks');
  const menuKeyboard = createComposerKeyHandler(state, { ...suggestions, slashVisible: true, sIdx: 0 }, editor);
  menuKeyboard(keyEvent());
  await settle();
  assert.equal(postCount, 2, 'Enter selects the visible fork menu action without generating');
  assert.equal(generations, 0);

  mount(' /fork ');
  createComposerKeyHandler(state, suggestions, editor)(keyEvent());
  await settle();
  assert.equal(postCount, 3, 'hidden-menu Enter intercepts an exact trimmed command');
  mount('/fork');
  editor.sendFromComposer();
  editor.sendFromComposer();
  await settle();
  assert.equal(postCount, 4, 'two clicks share the mutation lock');
  assert.equal(generations, 0);

  mount('/fork extra');
  editor.sendFromComposer();
  await settle();
  assert.equal(postCount, 4);
  assert.equal(generations, 0);
  assert.equal(readComposer().input, '/fork extra');
  assert.ok(notices.includes(t('用法：/fork（不支持参数）')));
  mount('Please explain /fork');
  editor.sendFromComposer();
  assert.equal(generations, 1, 'ordinary text mentioning the command is still sent');

  mount('/fork', true);
  assert.ok(suggestions.slashEntries.every((entry) => entry.kind !== 'chat_action'));
  editor.sendFromComposer();
  await settle();
  assert.equal(postCount, 4, 'project landing composer cannot fork an unrelated current chat');
  assert.equal(generations, 1);

  mount('/fork');
  createComposerKeyHandler(state, suggestions, editor)(keyEvent('Enter', true));
  await settle();
  assert.equal(postCount, 4, 'IME confirmation must not fork');

  mount('/fork');
  shouldFail = true;
  editor.sendFromComposer();
  await settle();
  assert.equal(postCount, 5);
  assert.equal(readComposer().input, '/fork', 'rejected fork preserves the command');
  assert.equal(useChatStore.getState().currentChatId, 'source');
  shouldFail = false;

  mount('/fork');
  let release!: () => void;
  waitForResponse = new Promise<void>((resolve) => { release = resolve; });
  editor.sendFromComposer();
  await settle();
  readComposer().setInput('New draft while waiting');
  release();
  await settle();
  assert.equal(readComposer(chatDraftKey('source')).input, 'New draft while waiting', 'success preserves edits made during creation');
  waitForResponse = undefined;

  mount('/init');
  editor.sendFromComposer();
  assert.equal(generations, 1, '/init still enforces project permissions');
  assert.equal(postCount, 6);
  console.log('PASS: fork menu/click/Enter, invalid arguments, project scope, IME, duplicate requests, draft retention, and /init guard');
} finally {
  Object.assign(message, originals);
}
