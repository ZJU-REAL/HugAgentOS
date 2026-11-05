import { useState } from 'react';
import { ExclamationCircleFilled } from '@ant-design/icons';
import { TaskQuestion } from './TaskQuestion';
import { attentionTasks, needsResultCheck, taskTitle } from './taskState';
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

interface Action {
  label: string;
  tone: 'primary' | 'soft' | 'ghost';
  run: () => Promise<unknown> | void;
}

interface Item {
  key: string;
  testId: string;
  title: string;
  op: string;
  detail: string;
  hint?: string;
  actions: Action[];
}

function taskItem(task: PersonalTask, onOpen: (task: PersonalTask) => void): Item {
  const open: Action = { label: t('查看任务'), tone: 'soft', run: () => onOpen(task) };
  const base = { key: task.id, testId: 'pa-attention-task', op: t('任务'), detail: taskTitle(task).slice(0, 80) };
  const actionId = needsResultCheck(task) ? task.uncertain_actions?.[0] : undefined;
  if (actionId) return { ...base, title: t('请先核对操作结果'), hint: task.waiting_reason, actions: [
    { label: t('未执行'), tone: 'ghost', run: () => personalApi.reconcile(task.id, actionId, 'not_executed') },
    open,
    { label: t('已执行'), tone: 'primary', run: () => personalApi.reconcile(task.id, actionId, 'executed') },
  ] };
  return { ...base, title: task.waiting_reason || t('需要你处理'),
    actions: [open, { label: t('重试'), tone: 'primary', run: () => personalApi.retry(task.id) }] };
}

/**
 * Everything that waits for the user, in the chat's own docked bars: questions get the question card, and
 * stopped tasks, failed messages and undelivered replies queue in the confirm bar (as approvals do), one at a time.
 */
export function AttentionBar({ tasks, attention, chatId, now, onDone, onError, onOpen }: AttentionBarProps) {
  const [busy, setBusy] = useState(false);
  const waiting = attentionTasks(tasks, now);
  const questions = waiting.filter(task => task.question);
  const items: Item[] = [
    ...waiting.filter(task => !task.question).map(task => taskItem(task, onOpen)),
    ...(attention?.turns || []).map(turn => ({
      key: 'turn:' + turn.message_id, testId: 'pa-attention-turn', op: t('消息'), detail: turn.preview,
      title: turn.kind === 'task_event' ? t('任务结果没有汇报成功') : t('这条消息没有处理成功'),
      actions: [{ label: t('重新处理'), tone: 'primary' as const, run: () => personalApi.reprocess(turn.message_id) }],
    })),
    ...(attention?.deliveries || []).map(item => {
      const channel = CHANNELS.find(entry => entry.type === item.channel_type);
      return {
        key: 'delivery:' + item.message_id, testId: 'pa-attention-delivery', op: t('回复'), detail: item.preview,
        title: channel ? t('回复没有发到{channel}', { channel: t(channel.label) }) : t('回复没有发到渠道'),
        actions: [{ label: t('重新发送'), tone: 'primary' as const, run: () => personalApi.retryDelivery(item.message_id) }],
      };
    }),
  ];
  const item = items[0];

  async function run(action: Action) {
    if (busy) return;
    setBusy(true);
    try {
      const result = action.run();
      if (result) { await result; await onDone(); }
    } catch (exc) { onError(exc); }
    finally { setBusy(false); }
  }

  return <>
    {!!questions.length && <TaskQuestion tasks={questions} chatId={chatId} onDone={onDone} onError={onError} onOpen={onOpen} />}
    {item && <div className="jx-confirmBar pa-confirmBar" role="alertdialog" aria-label={t('需要你处理')} data-testid="pa-attention">
      <span className="jx-confirmBar-icon" aria-hidden="true"><ExclamationCircleFilled /></span>
      <div className="jx-confirmBar-body" key={item.key} data-testid={item.testId}>
        <div className="jx-confirmBar-title">
          {item.title}
          {items.length > 1 && <span className="jx-confirmBar-count">{t('（还有 {n} 项排队，逐个确认）', { n: items.length - 1 })}</span>}
        </div>
        <div className="jx-confirmBar-detail">
          <span className="jx-confirmBar-op">{item.op}</span>
          <span className="jx-confirmBar-path" title={item.detail}>{item.detail}</span>
        </div>
        {item.hint && <div className="jx-confirmBar-hint">{item.hint}</div>}
      </div>
      <div className="jx-confirmBar-actions">
        {item.actions.map(action => <button key={action.label} type="button" disabled={busy}
          className={'jx-confirmBar-btn jx-confirmBar-btn--' + action.tone} onClick={() => void run(action)}>{action.label}</button>)}
      </div>
    </div>}
  </>;
}
