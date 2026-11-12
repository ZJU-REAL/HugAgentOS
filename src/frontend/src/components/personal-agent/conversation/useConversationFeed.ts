import { useEffect, useRef, useState } from 'react';
import { t } from '../../../i18n';
import { personalApi, errorText, type PersonalConversation, type PersonalMonitor, type PersonalMessage } from '../api';
import { MESSAGE_PAGE } from '../shared/limits';
import { subscribePersonalChanges } from '../shared/liveFeed';

/** Conversation-scoped loading, live updates and pagination; disposed requests cannot update another chat. */
export function useConversationFeed(chatId: string | undefined, onError: (message: string) => void) {
  const [conversations, setConversations] = useState<PersonalConversation[]>([]);
  const [monitor, setMonitor] = useState<PersonalMonitor>();
  const [messages, setMessages] = useState<PersonalMessage[]>([]);
  const [older, setOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const olderBusy = useRef(false);
  const [pollError, setPollError] = useState('');
  const [checkedAt, setCheckedAt] = useState(0);
  const [loading, setLoading] = useState(true);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  // A page of history requested for one chat is dropped if the reader has switched to another meanwhile.
  const chatIdRef = useRef(chatId);
  chatIdRef.current = chatId;
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let first = true;
    // The web conversation's own id, learned from the first snapshot; a signal for it refreshes the messages.
    let ownChat = chatId;
    const load = async (withMessages: boolean) => {
      // Only the newest page of messages is read; older pages load as the reader scrolls up.
      const [state, rows, channels] = await Promise.all([personalApi.monitor(controller.signal, chatId),
        withMessages ? personalApi.messages(0, controller.signal, chatId, MESSAGE_PAGE) : null, personalApi.conversations(controller.signal)]);
      if (disposed) return;
      ownChat = chatId || state.agent?.chat_id;
      setMonitor(state);
      setConversations(channels);
      setCheckedAt(Date.now());
      if (!rows) return;
      if (first) {
        first = false;
        setMessages(rows);
        setOlder(rows.length === MESSAGE_PAGE);
      } else if (rows.length) {
        setMessages(previous => {
          // More new messages than one page arrived since the last read (the connection was down): the loaded rows
          // and the new page no longer meet, so the view restarts from the newest page and scrolling up fills the rest.
          const newest = previous.reduce((max, row) => Math.max(max, row.seq), 0);
          if (rows.length === MESSAGE_PAGE && rows[0].seq > newest) { setOlder(true); return rows; }
          const merged = new Map(previous.map(row => [row.id, row]));
          // An unchanged row keeps its object, so rendered messages (and any text selection in them) stay put.
          rows.forEach(row => { const old = merged.get(row.id); merged.set(row.id, old && JSON.stringify(old) === JSON.stringify(row) ? old : row); });
          return [...merged.values()].sort((a, b) => a.seq - b.seq);
        });
      }
    };
    // One read at a time: signals that arrive meanwhile are merged into a single read after it, and a caller's
    // promise settles only once a read that started after its call has finished.
    let running: Promise<void> | null = null;
    let wanted: boolean | null = null;
    const refresh = (withMessages = true): Promise<void> => {
      wanted = (wanted ?? false) || withMessages;
      running ??= (async () => {
        while (wanted !== null && !disposed) {
          const next = wanted;
          wanted = null;
          try { await load(next); if (!disposed) setPollError(''); }
          catch (exc) { if (!disposed && !controller.signal.aborted) onError(errorText(exc)); }
        }
      })().finally(() => { running = null; });
      return running;
    };
    refreshRef.current = () => refresh(true);
    let ready = false;
    const unsubscribe = subscribePersonalChanges(signal => {
      if (signal.type === 'down') { if (!disposed) setPollError(t('连接中断，正在重新连接。')); return; }
      if (!ready) return;
      // A (re)connection reloads everything; a change reloads the messages only when they are this conversation's.
      void refresh(signal.type === 'ready' || !ownChat || signal.topics.includes(ownChat));
    });
    void personalApi.initialize().then(() => { ready = true; return refresh(true); })
      .catch(exc => { if (!disposed) onError(errorText(exc)); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; controller.abort(); unsubscribe(); };
  }, [chatId, onError]);
  async function history() {
    if (!messages.length || olderBusy.current) return;
    olderBusy.current = true;
    setLoadingOlder(true);
    const requestedFor = chatId;
    try {
      const rows = await personalApi.history(messages[0].seq, chatId, MESSAGE_PAGE);
      if (requestedFor !== chatIdRef.current) return;
      setMessages(previous => {
        const known = new Set(previous.map(row => row.id));
        return [...rows.filter(row => !known.has(row.id)), ...previous];
      });
      setOlder(rows.length === MESSAGE_PAGE);
    } catch (exc) { onError(errorText(exc)); }
    finally { olderBusy.current = false; setLoadingOlder(false); }
  }
  return { conversations, monitor, messages, older, loadingOlder, pollError, checkedAt, loading, refreshRef, history };
}
