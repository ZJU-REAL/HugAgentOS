import { useEffect } from 'react';
import { listSessions } from '../api';
import { useAuthStore } from '../stores/authStore';
import { useChatStore } from '../stores/chatStore';
import { conversationTitle } from '../utils/conversationTitle';
import { isAutomationHistoryChat } from '../utils/history';

/** Discover sessions created by the scheduler while this module is open. */
export function useAutomationConversations(userId: string, enabled: boolean, localReady: boolean) {
  useEffect(() => {
    if (!userId || !enabled) return;
    let cancelled = false;
    let fetching = false;
    const refresh = async () => {
      if (fetching || document.hidden) return;
      fetching = true;
      const known = new Set(Object.keys(useChatStore.getState().store.chats));
      try {
        const { items } = await listSessions(1, 100);
        if (cancelled || useAuthStore.getState().authUser?.user_id !== userId) return;
        const chat = useChatStore.getState();
        const sessions = items.filter(isAutomationHistoryChat)
          // A deletion completed during the request must not be undone by its stale response.
          .filter(item => !known.has(item.id) || !!chat.store.chats[item.id]);
        chat.updateStore(prev => {
          const chats = { ...prev.chats };
          const order = [...prev.order];
          for (const session of sessions) {
            const prior = chats[session.id];
            chats[session.id] = prior ? {
              ...session, ...prior,
              title: conversationTitle(prior.title),
              updatedAt: Math.max(session.updatedAt, prior.updatedAt),
              automationTaskId: session.automationTaskId,
              automationRun: session.automationRun,
            } : session;
            if (!order.includes(session.id)) order.push(session.id);
          }
          return { chats, order };
        });
        sessions.forEach(session => chat.addBackendSessionId(session.id));
      } catch (error) {
        if (!cancelled) console.warn('Task conversation refresh failed', error);
      } finally {
        fetching = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', visible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [userId, enabled, localReady]);
}
