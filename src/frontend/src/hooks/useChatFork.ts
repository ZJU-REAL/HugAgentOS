import { chatDraftKey, readComposer } from '../stores/composerStore';
import { useCallback, useSyncExternalStore } from 'react';
import { message } from 'antd';
import { authFetch, chatTargetHeaders, getApiUrl, isLocalChat, registerLocalChat } from '../api';
import { ChatForkError, createChatFork, type ChatForkSession } from '../api/chatForks';
import { isLocalDraftChat, useChatStore } from '../stores/chatStore';
import type { ChatItem } from '../types';
import { parseHistoryMessage } from './chatHistoryMessage';
import { t } from '../i18n';
import { mergeHistoryPage } from '../utils/historyMerge';

const PAGE_SIZE = 30;
const active = new Map<string, Promise<boolean>>();
const retryIds = new Map<string, string>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const changed = () => listeners.forEach((listener) => listener());
let accountEpoch = 0;
let navigationEpoch = 0;
useChatStore.subscribe((state, previous) => {
  if (state.currentUserId !== previous.currentUserId) {
    accountEpoch += 1;
    retryIds.clear();
  }
  if (state.currentChatId !== previous.currentChatId) navigationEpoch += 1;
});

function requestIdFor(key: string): string {
  const prior = retryIds.get(key);
  if (prior) return prior;
  // Bound uncertain network retries without keeping an unbounded per-account history.
  if (retryIds.size >= 100) retryIds.delete(retryIds.keys().next().value!);
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  const id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  retryIds.set(key, id);
  return id;
}

function sessionItem(session: ChatForkSession, source: ChatItem | undefined, local: boolean): ChatItem {
  const metadata = session.metadata;
  return {
    id: session.chat_id,
    title: session.title,
    titleManuallySet: true,
    createdAt: Date.parse(session.created_at) || Date.now(),
    updatedAt: Date.parse(session.updated_at) || Date.now(),
    messages: [],
    runTarget: local ? 'local' : 'cloud',
    projectId: session.project_id || undefined,
    projectName: session.project_id === source?.projectId ? source?.projectName : undefined,
    agentId: typeof metadata.agent_id === 'string' ? metadata.agent_id : undefined,
    agentName: typeof metadata.agent_name === 'string' ? metadata.agent_name : undefined,
    modeSlug: source?.modeSlug,
    thinkingEffort: source?.thinkingEffort,
    // A historical plan card must never implicitly re-enter an execution mode.
    planModeActive: false,
    batchModeActive: false,
    workflowModeActive: false,
  };
}

/** A retry may discover a branch already opened and continued via a sidebar refresh. */
function reconcileSession(session: ChatForkSession, source: ChatItem | undefined, local: boolean, prior?: ChatItem): ChatItem {
  const canonical = sessionItem(session, source, local);
  if (!prior) return canonical;
  return {
    ...canonical,
    ...prior,
    id: canonical.id,
    title: prior.titleManuallySet ? prior.title : canonical.title,
    updatedAt: Math.max(prior.updatedAt, canonical.updatedAt),
    projectId: canonical.projectId,
    projectName: prior.projectId === canonical.projectId ? prior.projectName ?? canonical.projectName : canonical.projectName,
    agentId: canonical.agentId,
    agentName: canonical.agentName,
    runTarget: canonical.runTarget,
  };
}

/** Check account ownership again after the response, before touching any user cache. */
async function loadForkHistory(chatId: string, valid: () => boolean): Promise<boolean> {
  const response = await authFetch(
    `${getApiUrl()}/v1/chats/${encodeURIComponent(chatId)}/messages?page=1&page_size=${PAGE_SIZE}&order=desc`,
    { headers: chatTargetHeaders(chatId) },
  );
  if (!response.ok) return false;
  const body = await response.json() as { data?: { items?: unknown[]; pagination?: { has_next?: boolean } } };
  if (!valid()) return false;
  // The ordinary loader may already have hydrated, edited, or continued this branch.
  if (useChatStore.getState().loadedMsgIds.has(chatId)) return true;
  const items = Array.isArray(body.data?.items) ? body.data.items : [];
  const messages = items.map(parseHistoryMessage);
  const state = useChatStore.getState();
  state.updateStore((store) => {
    const chat = store.chats[chatId];
    if (!chat) return store;
    const merged = mergeHistoryPage(chat.messages, messages, {
      localIsWriter: useChatStore.getState().sendingChatIds.has(chatId),
    });
    return { ...store, chats: { ...store.chats, [chatId]: { ...chat, messages: merged } } };
  });
  const paging = useChatStore.getState().messagePaging[chatId];
  state.setMessagePaging(chatId, {
    nextPage: Math.max(2, paging?.nextPage ?? 0),
    hasOlder: paging && paging.nextPage > 2 ? paging.hasOlder : !!body.data?.pagination?.has_next,
    loading: paging?.loading ?? false,
  });
  state.addLoadedMsgId(chatId);
  return true;
}

/** Shared mutation entry point used by the slash command and every reply button. */
export function forkConversation(chatId: string, throughMessageId?: string, options?: { clearInput?: string }): Promise<boolean> {
  const start = useChatStore.getState();
  const actor = start.currentUserId;
  if (!actor || !chatId || isLocalDraftChat(chatId)) {
    void message.info(t('请先完成一轮聊天，再创建聊天分支'));
    return Promise.resolve(false);
  }
  const lockKey = `${accountEpoch}:${actor}:${chatId}`;
  if (active.has(lockKey)) return active.get(lockKey)!;
  const local = isLocalChat(chatId);
  const retryKey = JSON.stringify([actor, local, chatId, throughMessageId ?? null]);
  const requestId = requestIdFor(retryKey);
  const epoch = accountEpoch;
  const navEpoch = navigationEpoch;
  const startPath = typeof window === 'undefined' ? '' : window.location.pathname;
  const source = start.store.chats[chatId];
  const valid = () => accountEpoch === epoch && useChatStore.getState().currentUserId === actor;
  // Defer to a microtask so the shared lock exists before network work starts.
  const task = Promise.resolve().then(async () => {
    if (!valid()) return false;
    try {
      const session = await createChatFork(chatId, requestId, throughMessageId);
      if (!valid()) return false;
      retryIds.delete(retryKey);
      if (local) registerLocalChat(session.chat_id);
      const state = useChatStore.getState();
      state.updateStore((store) => ({
        ...store,
        chats: { ...store.chats, [session.chat_id]: reconcileSession(session, source, local, store.chats[session.chat_id]) },
        order: store.order.includes(session.chat_id) ? store.order : [session.chat_id, ...store.order],
      }));
      state.addBackendSessionId(session.chat_id);
      // Failure here leaves a real, addressable branch. The normal loader retries it.
      let loaded = useChatStore.getState().loadedMsgIds.has(session.chat_id);
      if (!loaded) {
        try { loaded = await loadForkHistory(session.chat_id, valid); } catch { /* preserve the created session */ }
      }
      if (!valid()) return false;
      if (navigationEpoch === navEpoch && useChatStore.getState().currentChatId === chatId
        && (typeof window === 'undefined' || window.location.pathname === startPath)) {
        const current = useChatStore.getState();
        const draft = readComposer(chatDraftKey(chatId));
        if (options?.clearInput !== undefined && draft.input === options.clearInput) draft.setInput('');
        current.setCurrentChatId(session.chat_id);
      }
      if (loaded) void message.success(t('已创建聊天分支'));
      else void message.warning(t('聊天分支已创建，历史加载失败，可重新打开该分支重试'));
      return true;
    } catch (error) {
      // Explicit rejection is safe to retry as a new operation; uncertain failures retain the key.
      if (error instanceof ChatForkError && error.status >= 400 && error.status < 500) retryIds.delete(retryKey);
      if (valid()) void message.error(error instanceof Error ? error.message : t('创建聊天分支失败，请重试'));
      return false;
    }
  }).finally(() => { active.delete(lockKey); changed(); });
  active.set(lockKey, task);
  changed();
  return task;
}

export function useChatFork(chatId?: string) {
  const currentChatId = useChatStore((state) => state.currentChatId);
  const actor = useChatStore((state) => state.currentUserId);
  const sourceId = chatId ?? currentChatId;
  const key = `${accountEpoch}:${actor}:${sourceId}`;
  const pending = useSyncExternalStore(subscribe, () => active.has(key), () => false);
  const forkChat = useCallback((throughMessageId?: string, options?: { clearInput?: string }) => forkConversation(sourceId, throughMessageId, options), [sourceId]);
  return { forkChat, pending };
}
