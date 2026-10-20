import { useEffect, useEffectEvent } from 'react';
import { authFetch, chatTargetHeaders, getApiUrl } from '../api';
import { useAuthStore, useChatStore } from '../stores';
import { parseSSE } from '../utils/sse';
import { reloadChatHistory } from './useChatInit';

type WakeupEvent = { type: 'command_runs'; run_ids: string[] };

/** Keep a notification stream open before a background command wakes its model. */
export function useCommandCompletions(followRun: (chatId: string) => Promise<boolean | undefined>) {
  const chatId = useChatStore(state => state.currentChatId);
  const registered = useChatStore(state => state.backendSessionIds.has(state.currentChatId));
  const userId = useAuthStore(state => state.authUser?.user_id);
  const follow = useEffectEvent(followRun);

  useEffect(() => {
    if (!chatId || !userId || !registered) return;
    let active = true;
    let processing = false;
    let connection: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout>;
    let endPause: (() => void) | undefined;
    const seen = new Set<string>();
    const pending = new Set<string>();

    const drain = async () => {
      const state = useChatStore.getState();
      if (!active || processing || !pending.size
        || state.sendingChatIds.has(chatId) || state.activeRuns[chatId]) return;
      processing = true;
      const batch = [...pending];
      let delivered = false;
      try {
        const settled = await follow(chatId);
        if (!active) return;
        const latest = useChatStore.getState();
        if (latest.sendingChatIds.has(chatId) || latest.activeRuns[chatId]) return;
        if (!settled) throw new Error('Wakeup output subscription did not settle');
        // The reply may have finished before this browser received the notification.
        if (!await reloadChatHistory(chatId)) throw new Error('Wakeup history load failed');
        if (!active) return;
        batch.forEach(id => { seen.add(id); pending.delete(id); });
        delivered = true;
      } catch (error) {
        if (active) {
          console.warn('[command completion] Delivery failed', error);
          connection?.abort();
        }
      } finally {
        processing = false;
      }
      if (delivered && pending.size) void drain();
    };

    // A notification received while another response is streaming remains queued.
    // Store changes release it as soon as the current response ends.
    const unsubscribe = useChatStore.subscribe(() => { void drain(); });
    const connect = async () => {
      let reconnectMs = 500;
      while (active) {
        connection = new AbortController();
        try {
          const response = await authFetch(
            `${getApiUrl()}/v1/commands/events?chat_id=${encodeURIComponent(chatId)}`,
            { headers: chatTargetHeaders(chatId), signal: connection.signal },
          );
          if ([401, 403, 404].includes(response.status)) return;
          if (!response.ok || !response.body) throw new Error(`Wakeup subscription HTTP ${response.status}`);
          for await (const event of parseSSE<WakeupEvent>(response)) {
            if (!active) return;
            if (event?.type !== 'command_runs' || !Array.isArray(event.run_ids)) continue;
            reconnectMs = 500;
            event.run_ids.forEach(id => {
              if (typeof id === 'string' && id && !seen.has(id)) pending.add(id);
            });
            void drain();
          }
        } catch (error) {
          if (active && !connection.signal.aborted) {
            console.warn('[command completion] Subscription interrupted', error);
          }
        } finally {
          connection.abort();
        }
        if (active) {
          // Reconnect a broken transport. Task discovery never uses a polling timer.
          await new Promise<void>(resolve => {
            endPause = resolve;
            timer = setTimeout(resolve, reconnectMs);
          });
          endPause = undefined;
          reconnectMs = Math.min(reconnectMs * 2, 10000);
        }
      }
    };
    void connect();
    return () => {
      active = false;
      connection?.abort();
      clearTimeout(timer);
      endPause?.();
      unsubscribe();
    };
  }, [chatId, registered, userId]);
}
