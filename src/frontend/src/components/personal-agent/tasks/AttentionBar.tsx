import { useState } from 'react';
import { Button, Input } from 'antd';
import { ExclamationCircleOutlined } from '@ant-design/icons';
import { TaskActions } from './TaskActions';
import { attentionTasks, taskTitle } from './taskState';
import { personalApi, type PersonalAttention, type PersonalTask } from '../api';
import { CHANNELS } from '../channels/channelMeta';
import { t } from '../../../i18n';

interface AttentionBarProps {
  tasks: PersonalTask[];
  attention?: PersonalAttention;
  chatId?: string;
  now: number;
  onDone: () => Promise<void>;
  onError: (exc: unknown) => void;
  onOpen: (task: PersonalTask) => void;
}

/** The answer to a task's question goes to that task as a new instruction. */
function QuestionReply({ task, chatId, onDone, onError }: { task: PersonalTask; chatId?: string; onDone: () => Promise<void>; onError: (exc: unknown) => void }) {
  const [answer, setAnswer] = useState('');
  const [requestKey, setRequestKey] = useState<string>();
  const [sending, setSending] = useState(false);
  async function send() {
    if (!answer.trim() || sending) return;
    const key = requestKey || crypto.randomUUID();
    setRequestKey(key);
    setSending(true);
    try {
      await personalApi.send(answer.trim(), key, [task.id], [], 'update', undefined, chatId);
      setAnswer(''); setRequestKey(undefined);
      await onDone();
    } catch (exc) { onError(exc); }
    finally { setSending(false); }
  }
  return <div className="pa-attention-question">
    <p className="pa-attention-ask">{task.question}</p>
    <div className="pa-attention-reply">
      <Input size="small" aria-label={t('回答「{title}」的问题', { title: taskTitle(task).slice(0, 40) })} placeholder={t('输入你的回答')}
        value={answer} disabled={sending} onChange={event => { setAnswer(event.target.value); setRequestKey(undefined); }}
        onPressEnter={event => { if (!event.nativeEvent.isComposing) void send(); }} />
      <Button size="small" type="primary" loading={sending} disabled={!answer.trim()} onClick={() => void send()}>{t('回答')}</Button>
    </div>
  </div>;
}

/** One action button that refreshes the page after it ran. */
function RunButton({ label, action, onDone, onError }: { label: string; action: () => Promise<unknown>; onDone: () => Promise<void>; onError: (exc: unknown) => void }) {
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try { await action(); await onDone(); } catch (exc) { onError(exc); }
    finally { setBusy(false); }
  }
  return <div className="pa-task-actions"><Button size="small" loading={busy} onClick={() => void run()}>{label}</Button></div>;
}

/**
 * Everything that waits for the user, in the main conversation and not only in the monitor: questions first,
 * then tasks that stopped, then messages that failed and replies that did not reach IM. Each item has its action.
 */
export function AttentionBar({ tasks, attention, chatId, now, onDone, onError, onOpen }: AttentionBarProps) {
  const items = attentionTasks(tasks, now);
  const turns = attention?.turns || [];
  const deliveries = attention?.deliveries || [];
  const count = items.length + turns.length + deliveries.length;
  if (!count) return null;
  return <section className="pa-attention" aria-label={t('需要你处理')} data-testid="pa-attention">
    <header><ExclamationCircleOutlined /> {t('需要你处理')} ({count})</header>
    {items.map(task => <div className="pa-attention-item" key={task.id}>
      <Button type="link" className="pa-task-title" onClick={() => onOpen(task)}>{taskTitle(task).slice(0, 80)}</Button>
      {task.question
        ? <QuestionReply task={task} chatId={chatId} onDone={onDone} onError={onError} />
        : <TaskActions task={task} onDone={onDone} onError={onError} />}
    </div>)}
    {turns.map(turn => <div className="pa-attention-item" key={'turn:' + turn.message_id} data-testid="pa-attention-turn">
      <span className="pa-attention-label">{turn.kind === 'task_event' ? t('任务结果没有汇报成功') : t('这条消息没有处理成功')}</span>
      <p>{turn.preview}</p>
      <RunButton label={t('重新处理')} action={() => personalApi.reprocess(turn.message_id)} onDone={onDone} onError={onError} />
    </div>)}
    {deliveries.map(item => {
      const channel = CHANNELS.find(entry => entry.type === item.channel_type);
      return <div className="pa-attention-item" key={'delivery:' + item.message_id} data-testid="pa-attention-delivery">
        <span className="pa-attention-label">{channel ? t('回复没有发到{channel}', { channel: t(channel.label) }) : t('回复没有发到渠道')}</span>
        <p>{item.preview}</p>
        <RunButton label={t('重新发送')} action={() => personalApi.retryDelivery(item.message_id)} onDone={onDone} onError={onError} />
      </div>;
    })}
  </section>;
}
