import { useCallback, useEffect, useRef, useState } from 'react';
import { getActiveChatRun } from '../../../api';
import { parseHistoryMessage } from '../../../hooks/chatHistoryMessage';
import { followRunStream, isFollowableRun, withRunFollowLock } from '../../../hooks/runFollow';
import { useChatStore } from '../../../stores';
import type { ChatMessage } from '../../../types';
import { mergeHistoryPage } from '../../../utils/historyMerge';
import { personalApi, errorText } from '../api';
import { subscribePersonalChanges } from '../shared/liveFeed';

interface HistoryRow extends Record<string, unknown> {
  message_id: string; chat_seq: number; is_streaming: boolean; metadata?: Record<string, unknown>;
}
interface Thread {
  chat_id: string; task_run_id?: string | null; task_status: string; terminal_message_id?: string | null;
  has_more: boolean; messages: HistoryRow[];
}
export type TaskThreadInfo = Omit<Thread, 'messages' | 'has_more'>;

const EMPTY: ChatMessage[] = [];

/** A row of the task endpoint in the shape ordinary chat history has: only the live row carries `in_flight`. */
function toMessage(row: HistoryRow): ChatMessage {
  const { in_flight: marker, ...metadata } = row.metadata || {};
  return parseHistoryMessage({ ...row, metadata, ...(row.is_streaming && marker ? { in_flight: marker } : {}) });
}

/** Put the task's Worker session in the chat store, outside the sidebar, so the ordinary stream reducer can write to it. */
function ensureChat(chatId: string) {
  const st = useChatStore.getState();
  if (st.store.chats[chatId]) return;
  const now = Date.now();
  st.updateStore(prev => prev.chats[chatId] ? prev : {
    ...prev, chats: { ...prev.chats, [chatId]: { id: chatId, title: '', createdAt: now, updatedAt: now, messages: [] } },
  });
}

function merge(chatId: string, page: ChatMessage[], prepend = false) {
  useChatStore.getState().updateStore(prev => {
    const chat = prev.chats[chatId];
    if (!chat) return prev;
    const known = new Set(chat.messages.map(m => m.messageId).filter(Boolean));
    const messages = prepend
      ? [...page.filter(m => !known.has(m.messageId)), ...chat.messages]
      : mergeHistoryPage(chat.messages, page, { localIsWriter: useChatStore.getState().sendingChatIds.has(chatId) });
    return { ...prev, chats: { ...prev.chats, [chatId]: { ...chat, messages } } };
  });
}

/**
 * A Personal Agent task's conversation, read the way an ordinary chat is: history from the database, and a run in
 * flight followed over its own event stream, so text, reasoning, tool arguments and tool progress arrive as they
 * happen. The Personal Agent change feed says when the history changed (a new run, a finished one); nothing polls.
 */
export function useTaskThread(taskId: string, taskStatus: string, taskRunId: string | null | undefined) {
  const [thread, setThread] = useState<TaskThreadInfo>();
  const [hasOlder, setHasOlder] = useState(false);
  const [error, setError] = useState('');
  const oldestSeq = useRef(0);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const chatId = thread?.chat_id;
  const messages = useChatStore(s => (chatId && s.store.chats[chatId]?.messages) || EMPTY);

  useEffect(() => {
    const controller = new AbortController();
    let following = false;
    let ownChat = '';

    // Attached exactly as an ordinary chat attaches to a run it did not start (hooks/runFollow.ts).
    async function follow(id: string) {
      if (following || controller.signal.aborted) return;
      following = true;
      let followed = false;
      try {
        const active = await getActiveChatRun(id);
        if (!isFollowableRun(active) || controller.signal.aborted || useChatStore.getState().sendingChatIds.has(id)) return;
        // The rows were just read; the stream's own snapshot brings the run up to date.
        followed = await withRunFollowLock(active.run_id, async () => {
          if (useChatStore.getState().sendingChatIds.has(id)) return false;
          await followRunStream(id, active, { signal: controller.signal, reload: async () => undefined });
          return true;
        }, false);
      } catch (exc) {
        if (!controller.signal.aborted) setError(errorText(exc));
      } finally {
        following = false;
      }
      // The database holds the run's final form; it may also show the next run already started.
      if (followed && !controller.signal.aborted) void refresh();
    }

    async function load() {
      const result = await personalApi.conversation<Thread>(taskId, undefined, controller.signal);
      if (controller.signal.aborted) return;
      const { messages: rows, has_more, ...info } = result;
      ownChat = info.chat_id;
      ensureChat(ownChat);
      merge(ownChat, rows.map(toMessage));
      if (!oldestSeq.current) { oldestSeq.current = rows[0]?.chat_seq || 0; setHasOlder(has_more); }
      setThread(info);
      setError('');
      if (rows.some(row => row.is_streaming)) void follow(ownChat);
    }

    // One read at a time; signals that arrive meanwhile are merged into a single read after it.
    let running: Promise<void> | null = null;
    let again = false;
    function refresh(): Promise<void> {
      again = true;
      running ??= (async () => {
        while (again && !controller.signal.aborted) {
          again = false;
          try { await load(); } catch (exc) { if (!controller.signal.aborted) setError(errorText(exc)); }
        }
      })().finally(() => { running = null; });
      return running;
    }
    refreshRef.current = refresh;

    // While the run's own stream is attached it is the source: the run's periodic saves also signal this
    // conversation, and reading them back would only repeat what the stream already showed. The read after the
    // stream ends picks up anything that changed meanwhile.
    const unsubscribe = subscribePersonalChanges(signal => {
      if (signal.type === 'ready' || (signal.type === 'changed' && !following && (!ownChat || signal.topics.includes(ownChat)))) void refresh();
    });
    void refresh();
    return () => { controller.abort(); unsubscribe(); };
  }, [taskId]);

  // The task's own status changes without a conversation topic; read again so the reply that answers it is known.
  const seen = useRef(`${taskStatus}|${taskRunId}`);
  useEffect(() => {
    const now = `${taskStatus}|${taskRunId}`;
    if (now === seen.current) return;
    seen.current = now;
    void refreshRef.current();
  }, [taskStatus, taskRunId]);

  const loadOlder = useCallback(async () => {
    if (!chatId || !oldestSeq.current) return;
    try {
      const result = await personalApi.conversation<Thread>(taskId, oldestSeq.current);
      merge(chatId, result.messages.map(toMessage), true);
      oldestSeq.current = result.messages[0]?.chat_seq || oldestSeq.current;
      setHasOlder(result.has_more);
    } catch (exc) { setError(errorText(exc)); }
  }, [chatId, taskId]);

  return { thread, messages, hasOlder, loadOlder, error, setError };
}
