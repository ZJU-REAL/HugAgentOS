import { useEffect, useRef, useState } from 'react';
import { t } from '../../../i18n';
import { personalApi, errorText, type PersonalConversation, type PersonalMonitor, type PersonalMessage } from '../api';
import { POLL_INTERVAL, REPLY_POLL_INTERVAL } from '../shared/limits';

/** Conversation-scoped loading, polling and pagination; disposed requests cannot update another chat. */
export function useConversationFeed(chatId: string | undefined, onError: (message: string) => void) {
  const [conversations, setConversations] = useState<PersonalConversation[]>([]);
  const [monitor, setMonitor] = useState<PersonalMonitor>();
  const [messages, setMessages] = useState<PersonalMessage[]>([]);
  const [older, setOlder] = useState(true);
  const [pollError, setPollError] = useState('');
  const [checkedAt, setCheckedAt] = useState(0);
  const [loading, setLoading] = useState(true);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  // While the agent is replying, poll faster so its answer appears as it is written.
  const replying = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      const [state, rows, channels] = await Promise.all([personalApi.monitor(controller.signal, chatId), personalApi.messages(0, controller.signal, chatId), personalApi.conversations(controller.signal)]);
      if (disposed) return;
      replying.current = !!state.turns?.length || rows.some(row => row.streaming);
      setMonitor(state);
      setConversations(channels);
      window.dispatchEvent(new Event('hugagent:personal-agent-updated'));
      setCheckedAt(Date.now());
      if (rows.length) {
        setMessages(previous => {
          const merged = new Map(previous.map(row => [row.id, row]));
          // An unchanged row keeps its object, so rendered messages (and any text selection in them) stay put.
          rows.forEach(row => { const old = merged.get(row.id); merged.set(row.id, old && JSON.stringify(old) === JSON.stringify(row) ? old : row); });
          return [...merged.values()].sort((a, b) => a.seq - b.seq);
        });
      }
    };
    refreshRef.current = refresh;
    const tick = async () => {
      // A hidden tab skips the poll; the next visible tick catches up.
      if (document.visibilityState === 'visible') {
        try { await refresh(); if (!disposed) setPollError(''); } catch { if (!disposed && !controller.signal.aborted) setPollError(t('连接中断，正在重新连接。')); }
      }
      if (!disposed) timer = setTimeout(() => void tick(), replying.current ? REPLY_POLL_INTERVAL : POLL_INTERVAL);
    };
    void personalApi.initialize().then(() => tick()).catch(exc => { if (!disposed) onError(errorText(exc)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [chatId, onError]);
  async function history() {
    if (!messages.length) return;
    try {
      const rows = await personalApi.history(messages[0].seq, chatId);
      setMessages(previous => [...rows.filter(row => !previous.some(old => old.id === row.id)), ...previous]);
      setOlder(rows.length === 100);
    } catch (exc) { onError(errorText(exc)); }
  }
  return { conversations, monitor, messages, older, pollError, checkedAt, loading, refreshRef, history };
}
