import { authFetch, chatTargetHeaders } from '../api';
import { useChatStore } from '../stores/chatStore';
import { useAuthStore } from '../stores/authStore';

const pending = new Map<string, Promise<void>>();

/** Follow the server-owned auxiliary title without waiting for the answer stream. */
export function syncConversationTitle(apiUrl: string, chatId: string): Promise<void> {
  const owner = useAuthStore.getState().authUser?.user_id;
  const identity = useChatStore.getState().store.chats[chatId];
  if (!apiUrl || !identity || identity.titleManuallySet) return Promise.resolve();
  const headers = chatTargetHeaders(chatId);
  const key = JSON.stringify([apiUrl, owner, chatId, headers]);
  const existing = pending.get(key);
  if (existing) return existing;
  const active = () => {
    const chat = useChatStore.getState().store.chats[chatId];
    return useAuthStore.getState().authUser?.user_id === owner
      && chat?.createdAt === identity.createdAt && !chat?.titleManuallySet;
  };
  const work = (async () => {
    const deadline = Date.now() + 40_000;
    while (active() && Date.now() < deadline) {
      try {
        const response = await authFetch(`${apiUrl}/v1/chats/${encodeURIComponent(chatId)}`, {
          headers, signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) return;
        const { data } = await response.json();
        if (!active()) return;
        const status = data?.metadata?.title_generation?.status;
        if (data?.title && (status === 'completed' || data?.metadata?.title_manually_set)) {
          useChatStore.getState().updateStore(prev => {
            const chat = prev.chats[chatId];
            if (!chat || chat.titleManuallySet) return prev;
            return { ...prev, chats: { ...prev.chats, [chatId]: {
              ...chat, title: data.title,
              ...(data.metadata.title_manually_set ? { titleManuallySet: true } : {}),
            } } };
          });
          return;
        }
        if (status !== 'pending') return;
      } catch { return; /* The persisted title is recovered on the next history load. */ }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  })().finally(() => pending.delete(key));
  pending.set(key, work);
  return work;
}
