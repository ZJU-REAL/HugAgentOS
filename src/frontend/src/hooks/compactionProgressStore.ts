import { getChatContextState } from '../api';
import { useChatStore } from '../stores';
import { mergeCompactionProgress, parseCompactionProgress } from '../utils/compactionProgress';

const expiryTimers = new Map<string, ReturnType<typeof setTimeout>>();

export function scheduleCompactionExpiry(chatId: string, raw: unknown) {
  if (!Array.isArray(raw)) return;
  for (const item of raw) {
    const progress = parseCompactionProgress(item);
    if (!progress || progress.chat_id !== chatId) continue;
    const existing = expiryTimers.get(progress.id);
    if (progress.status !== 'running') {
      if (existing) clearTimeout(existing);
      expiryTimers.delete(progress.id);
      continue;
    }
    if (existing || !progress.expires_at || !progress.message_id) continue;
    const userId = useChatStore.getState().currentUserId;
    const delay = Math.min(2_147_483_647, Math.max(0, progress.expires_at - Date.now() + 100));
    expiryTimers.set(progress.id, setTimeout(async () => {
      expiryTimers.delete(progress.id);
      if (useChatStore.getState().currentUserId !== userId) return;
      try {
        const state = await getChatContextState(chatId);
        if (useChatStore.getState().currentUserId !== userId) return;
        applyCompactionProgress(chatId, state.compaction_progress);
      } catch {
        if (useChatStore.getState().currentUserId !== userId) return;
        // An expired observation is distinct from an inferred failed summary.
        applyCompactionProgress(chatId, [{
          ...progress, status: 'interrupted', reason: 'expired', ended_at: progress.expires_at,
        }]);
      }
    }, delay));
  }
}

export function applyCompactionProgress(chatId: string, raw: unknown) {
  scheduleCompactionExpiry(chatId, raw);
  useChatStore.getState().updateStore(prev => {
    const chat = prev.chats[chatId];
    if (!chat) return prev;
    return { ...prev, chats: { ...prev.chats, [chatId]: {
      ...chat, messages: mergeCompactionProgress(chat.messages, chatId, raw),
    } } };
  });
}

export function progressForMessage(raw: unknown, messageId?: string) {
  if (!Array.isArray(raw) || !messageId) return [];
  return raw.map(parseCompactionProgress).filter(p => p?.message_id === messageId);
}
