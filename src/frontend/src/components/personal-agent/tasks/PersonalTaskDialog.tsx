import { useCallback, useState } from 'react';
import { Alert, Button, Modal } from 'antd';
import { useReadAtBottom } from '../../../hooks/useReadAtBottom';
import { useTaskNotificationStore } from '../../../stores/taskNotificationStore';
import { MessageBody } from '../../chat/MessageBody';
import { ToolMessageContext } from '../../tool/ToolMessageContext';
import { type PersonalTask, errorText } from '../api';
import { t } from '../../../i18n';
import { TaskActions } from './TaskActions';
import { taskTitle } from './taskState';
import { useTaskThread } from './useTaskThread';

export function PersonalTaskDialog({ task, onClose, onChange }: { task: PersonalTask; onClose: () => void; onChange: () => Promise<void> }) {
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const notice = useTaskNotificationStore(s => s.notices['personal:' + task.id]);
  const acknowledge = useCallback(() => { if (notice) useTaskNotificationStore.getState().markSeen(notice.key, notice.id); }, [notice]);
  const { thread, messages, hasOlder, loadOlder, error, setError } = useTaskThread(task.id, task.status, task.run_id);
  const chatId = thread?.chat_id || '';

  useReadAtBottom(viewport, !!notice && !notice.seen && thread?.task_run_id === task.run_id && thread?.task_status === task.status
    && messages.some(m => m.messageId === thread?.terminal_message_id && !m.isStreaming) && ['completed', 'failed'].includes(task.status), acknowledge);
  return <Modal open title={taskTitle(task).slice(0, 120)} width={1050} onCancel={onClose} footer={null}>
    {error && <Alert type="error" message={error} />}
    <div className="pa-task-dialog-actions"><TaskActions task={task} onDone={onChange} onError={exc => setError(errorText(exc))} /></div>
    {hasOlder && <Button onClick={() => void loadOlder()}>{t('查看更早的消息')}</Button>}
    <div ref={setViewport} className="pa-task-conversation" data-testid="task-conversation" data-chat-id={thread?.chat_id}>{messages.map((row, index) => <div className={'jx-msg ' + (row.role === 'user' ? 'user' : 'assistant')} key={row.uid}><div className="jx-msgInner"><div className="jx-msgContent"><ToolMessageContext.Provider value={{ chatId, messageId: row.messageId, messageUid: row.uid, readOnly: true }}><MessageBody m={row} messageIndex={index} currentChatId={chatId} send={() => undefined} /></ToolMessageContext.Provider></div></div></div>)}</div>
  </Modal>;
}
