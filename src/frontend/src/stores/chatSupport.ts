import { removeLocal, userScopedKey, writeLocal } from '../storage';
import type { ChatItem } from '../types';
import { normalizeChatInvocation } from '../utils/chatInvocation';
import { usePageConfigStore } from './pageConfigStore';
import type { StoreApi } from 'zustand';
import type { ChatState, ChatMode, QueuedChatMessage, ThinkingEffort } from './chatState';

export const SITES_PLUGIN_SLUG = 'sites';

export const VALID_CHAT_MODES: readonly ChatMode[] = ['turbo', 'fast', 'low', 'medium', 'high', 'xhigh', 'max'];

export function adminDefaultChatMode(): ChatMode {
  const defaults = usePageConfigStore.getState().config.defaults;
  const raw = defaults?.chat_mode as string | undefined;
  if (raw && (VALID_CHAT_MODES as readonly string[]).includes(raw)) {
    return raw as ChatMode;
  }
  // Fallback for the legacy field
  return defaults?.thinking_mode ? 'medium' : 'fast';
}

export function applyDefaultChatMode(): { chatMode: ChatMode; lastStandardMode: ThinkingEffort } {
  const mode = adminDefaultChatMode();
  return { chatMode: mode, lastStandardMode: mode === 'turbo' ? 'fast' : mode };
}

export function isThinkingMode(mode: ChatMode): boolean {
  return mode !== 'fast' && mode !== 'turbo';
}

export function isTurboMode(mode: ChatMode): boolean {
  return mode === 'turbo';
}

export const PENDING_SCROLL_MESSAGE_TS_KEY = 'hugagent_pending_scroll_message_ts';

export function isAutomationEntry(chatId: string): boolean {
  return chatId.startsWith('automation:');
}

export function loadPendingScrollMessageTs(userId: string | null | undefined) {
  if (typeof window === 'undefined') return null;
  const key = userScopedKey(PENDING_SCROLL_MESSAGE_TS_KEY, userId);
  if (!key) return null;
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function savePendingScrollMessageTs(userId: string | null | undefined, ts: number | null) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(PENDING_SCROLL_MESSAGE_TS_KEY, userId);
  if (!key) return;
  if (ts === null) {
    removeLocal(key);
    return;
  }
  writeLocal(key, String(ts));
}

export const QUEUED_MESSAGES_KEY = 'hugagent_queued_messages';

export function loadQueuedMessages(userId: string | null | undefined): Record<string, QueuedChatMessage> {
  if (typeof window === 'undefined') return {};
  const key = userScopedKey(QUEUED_MESSAGES_KEY, userId);
  if (!key) return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, QueuedChatMessage>;
    const out: Record<string, QueuedChatMessage> = {};
    for (const [chatId, q] of Object.entries(parsed || {})) {
      if (!q || typeof q.content !== 'string' || !q.content || typeof q.id !== 'string') continue;
      if (!['queued', 'steering', 'applied'].includes(q.status)) continue;
      if (q.status !== 'queued' && !q.targetRunId) continue;
      out[chatId] = { ...q, invocation: normalizeChatInvocation(q.invocation) };
    }
    return out;
  } catch {
    return {};
  }
}

export function saveQueuedMessages(
  userId: string | null | undefined,
  map: Record<string, QueuedChatMessage>,
) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(QUEUED_MESSAGES_KEY, userId);
  if (!key) return;
  const persistable = Object.entries(map).filter(([, q]) => (
    q?.status === 'queued' || !!q?.targetRunId
  ));
  try {
    if (persistable.length === 0) removeLocal(key);
    else writeLocal(key, JSON.stringify(Object.fromEntries(persistable)));
  } catch { /* localStorage 不可用时放弃持久化，不影响内存态 */ }
}

export function restoredEffort(
  chat?: ChatItem,
): Partial<{ chatMode: ChatMode; lastStandardMode: ThinkingEffort }> {
  const saved = chat?.thinkingEffort;
  if (!saved || !(VALID_CHAT_MODES as readonly string[]).includes(saved)) return {};
  return saved === 'turbo'
    ? { chatMode: saved }
    : { chatMode: saved, lastStandardMode: saved };
}

export interface ChatActionsContext {
  set: StoreApi<ChatState>['setState'];
  get: StoreApi<ChatState>['getState'];
  initializeDraftRunTarget: (id: string, newlyCreated?: boolean) => void;
  syncChatUrl: (id: string, opts?: { replace?: boolean }) => void;
  publishCurrentChatUrl: () => void;
}
