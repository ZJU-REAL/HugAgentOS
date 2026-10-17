import { useCallback } from 'react';
import { useReadAtBottom } from '../../../hooks/useReadAtBottom';
import { useTaskNotificationStore } from '../../../stores/taskNotificationStore';
import { useEffect, useRef, useMemo, useState } from 'react';
import { Alert, Button, Modal } from 'antd';
import { MessageBody } from '../../chat/MessageBody';
import { ToolMessageContext } from '../../tool/ToolMessageContext';
import { parseHistoryMessage } from '../../../hooks/chatHistoryMessage';
import { type ChatMessage } from '../../../types';
import { personalApi, type PersonalTask, errorText } from '../api';
import { t } from '../../../i18n';
import { TaskActions } from './TaskActions';
import { taskTitle } from './taskState';
interface HistoryMessage extends Record<string, unknown> { chat_seq: number; is_streaming: boolean }
interface Thread { chat_id: string; task_run_id?:string | null; task_status:string; terminal_message_id?:string; has_more: boolean; messages: HistoryMessage[] }
function mergeMessages(previous: HistoryMessage[], items: HistoryMessage[]) {
  const merged = new Map(previous.map(row => [row.message_id, row]));items.forEach(row => merged.set(row.message_id, row));
  return [...merged.values()].sort((a, b) => a.chat_seq - b.chat_seq);
}
export function PersonalTaskDialog({ task, onClose, onChange }: { task: PersonalTask; onClose: () => void; onChange: () => Promise<void> }) {
  const [viewport,setViewport]=useState<HTMLDivElement|null>(null);
  const notice=useTaskNotificationStore(s=>s.notices['personal:'+task.id]);
  const acknowledge=useCallback(()=>{if(notice)useTaskNotificationStore.getState().markSeen(notice.key,notice.id);},[notice]);
  const [thread, setThread] = useState<Thread>();
  const [records, setRecords] = useState<HistoryMessage[]>([]);
  const [hasOlder, setHasOlder] = useState(false);const loaded = useRef(false);
  const rows = useMemo<ChatMessage[]>(() => records.map(item => {
    const parsed = parseHistoryMessage(item);
    const raw = Array.isArray(item.tool_calls) ? item.tool_calls as Record<string, unknown>[] : [];
    return { ...parsed, isStreaming: item.is_streaming, toolCalls: parsed.toolCalls?.map((tool, index) => raw[index]?.status === 'running' ? { ...tool, status: item.is_streaming ? 'running' as const : 'interrupted' as const } : tool) };
  }), [records]);

  useReadAtBottom(viewport,!!notice&&!notice.seen&&thread?.task_run_id===task.run_id&&thread?.task_status===task.status&&records.some(r=>r.message_id===thread?.terminal_message_id&&!r.is_streaming)&&['completed','failed'].includes(task.status),acknowledge);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try { const result = await personalApi.conversation<Thread>(task.id, undefined, controller.signal);if (!controller.signal.aborted) { setThread(result);setRecords(previous => mergeMessages(previous, result.messages));if (!loaded.current) { loaded.current = true;setHasOlder(result.has_more); } } }
      catch (exc) { if (!controller.signal.aborted) setError(errorText(exc)); }
      if (!controller.signal.aborted) timer = setTimeout(() => void load(), 2000);
    };
    void load();return () => { controller.abort();clearTimeout(timer); };
  }, [task.id]);
  async function history() {
    try { const result = await personalApi.conversation<Thread>(task.id, records[0].chat_seq);setRecords(previous => mergeMessages(previous, result.messages));setHasOlder(result.has_more); }
    catch (exc) { setError(errorText(exc)); }
  }
  return <Modal open title={taskTitle(task).slice(0, 120)} width={1050} onCancel={onClose} footer={null}>
    {error && <Alert type="error" message={error} />}
    <div className="pa-task-dialog-actions"><TaskActions task={task} onDone={onChange} onError={exc => setError(errorText(exc))} /></div>
    {hasOlder && <Button onClick={() => void history()}>{t('查看更早的消息')}</Button>}
    <div ref={setViewport} className="pa-task-conversation" data-testid="task-conversation" data-chat-id={thread?.chat_id}>{rows.map((row, index) => <div className={'jx-msg ' + (row.role === 'user' ? 'user' : 'assistant')} key={row.uid}><div className="jx-msgInner"><div className="jx-msgContent"><ToolMessageContext.Provider value={{ chatId: thread?.chat_id || '', messageId: row.messageId, messageUid: row.uid, readOnly: true }}><MessageBody m={row} showProcess messageIndex={index} currentChatId={thread?.chat_id || ''} send={() => undefined} /></ToolMessageContext.Provider></div></div></div>)}</div>
  </Modal>;
}
