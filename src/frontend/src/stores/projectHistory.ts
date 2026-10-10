import type { ChatItem, ProjectChatSummary } from '../types';
import { getSession, isLocalProject, registerLocalChat } from '../api';
import { parseServerTime } from '../utils/date';
import { useChatStore } from './chatStore';

/** Authorized project history shares the ordinary sidebar and conversation loader. */
export async function mergeProjectHistory(
  items: ProjectChatSummary[], projectId: string, projectName: string | undefined, isCancelled: () => boolean,
) {
  const initial = useChatStore.getState();
  const sessions = new Map<string, ChatItem>();
  const missing = items.filter(item => !initial.backendSessionIds.has(item.chat_id));
  let next = 0;
  const failures: unknown[] = [];
  await Promise.all(Array.from({ length: Math.min(4, missing.length) }, async () => {
    while (next < missing.length && !isCancelled()) {
      const item = missing[next++];
      try {
        const local = isLocalProject(projectId);
        if (local) registerLocalChat(item.chat_id);
        const session = await getSession(item.chat_id);
        sessions.set(item.chat_id, session);
      } catch (error) { failures.push(error); }
    }
  }));
  if (isCancelled() || useChatStore.getState().currentUserId !== initial.currentUserId) return;
  const chat = useChatStore.getState();
  chat.updateStore(prev => {
    const chats = { ...prev.chats };
    const order = [...prev.order];
    for (const item of items) {
      const id = item.chat_id;
      // A summary lacks mode/task metadata. Never treat it as a complete session.
      if (!sessions.has(id) && !initial.backendSessionIds.has(id)) continue;
      chats[id] = {
        ...sessions.get(id), ...chats[id], id, title: item.title,
        createdAt: chats[id]?.createdAt || parseServerTime(item.created_at),
        updatedAt: parseServerTime(item.last_message_at || item.updated_at || item.created_at),
        messages: chats[id]?.messages || [], projectId, projectName,
        pinned: item.pinned, favorite: item.favorite,
      };
      if (!order.includes(id)) order.push(id);
    }
    return { ...prev, chats, order };
  });
  for (const id of sessions.keys()) chat.addBackendSessionId(id);
  if (failures.length) throw failures[0];
}
