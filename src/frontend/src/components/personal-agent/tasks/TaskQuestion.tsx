import { useId, useState, type KeyboardEvent } from 'react';
import { Button } from 'antd';
import { DownOutlined, ExportOutlined, LeftOutlined, RightOutlined, UpOutlined } from '@ant-design/icons';
import { AnswerField } from '../../chat/AskUserQuestionComposer';
import { taskTitle } from './taskState';
import { personalApi, type PersonalTask } from '../api';
import { t } from '../../../i18n';

interface TaskQuestionProps {
  tasks: PersonalTask[];
  chatId?: string;
  onDone: () => Promise<void>;
  onError: (exc: unknown) => void;
  onOpen: (task: PersonalTask) => void;
}

function without(record: Record<string, string>, key: string) {
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * Questions tasks ask the owner, in the same card the chat uses for the model's own questions.
 * The answer goes to its task as a new instruction; drafts are kept per task while paging.
 */
export function TaskQuestion({ tasks, chatId, onDone, onError, onOpen }: TaskQuestionProps) {
  const [index, setIndex] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [requestKeys, setRequestKeys] = useState<Record<string, string>>({});
  const [sending, setSending] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const titleId = `${useId()}-title`;
  const task = tasks[Math.min(index, tasks.length - 1)];
  if (!task) return null;
  const answer = drafts[task.id] || '';
  const position = tasks.indexOf(task);

  async function send() {
    if (!answer.trim() || sending) return;
    const key = requestKeys[task.id] || crypto.randomUUID();
    setRequestKeys(current => ({ ...current, [task.id]: key }));
    setSending(true);
    try {
      await personalApi.send(answer.trim(), key, [task.id], [], 'update', undefined, chatId);
      setDrafts(current => without(current, task.id));
      setRequestKeys(current => without(current, task.id));
      await onDone();
    } catch (exc) { onError(exc); }
    finally { setSending(false); }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    void send();
  }

  return <section className={'jx-askQ pa-question' + (minimized ? ' jx-askQ--minimized' : '')} role="dialog"
    aria-labelledby={titleId} aria-busy={sending} data-testid="pa-question">
    <header className="jx-askQ-header">
      <div className="jx-askQ-heading">
        <div className="jx-askQ-eyebrow">{taskTitle(task).slice(0, 80)}</div>
        <h2 id={titleId} className="jx-askQ-title">{task.question}</h2>
      </div>
      <div className="jx-askQ-headerActions">
        <button type="button" className="jx-askQ-iconButton" aria-label={t('查看任务')} title={t('查看任务')} onClick={() => onOpen(task)}>
          <ExportOutlined />
        </button>
        <button type="button" className="jx-askQ-iconButton" aria-label={minimized ? t('展开提问') : t('收起提问')}
          title={minimized ? t('展开提问') : t('收起提问')} aria-expanded={!minimized} onClick={() => setMinimized(value => !value)}>
          {minimized ? <UpOutlined /> : <DownOutlined />}
        </button>
      </div>
    </header>
    {!minimized && <>
      <div className="jx-askQ-body">
        <div className="jx-askQ-options">
          <AnswerField variant="block" value={answer} disabled={sending} placeholder={t('请输入回答…')} onKeyDown={onKeyDown}
            onChange={event => {
              setDrafts(current => ({ ...current, [task.id]: event.target.value }));
              setRequestKeys(current => without(current, task.id));
            }} />
        </div>
      </div>
      <footer className="jx-askQ-footer">
        {tasks.length > 1 && <div className="jx-askQ-pager">
          <button type="button" className="jx-askQ-iconButton" aria-label={t('上一题')} title={t('上一题')}
            disabled={position === 0 || sending} onClick={() => setIndex(position - 1)}><LeftOutlined /></button>
          <span className="jx-askQ-progress">{t('{current} / {total}', { current: position + 1, total: tasks.length })}</span>
          <button type="button" className="jx-askQ-iconButton" aria-label={t('下一题')} title={t('下一题')}
            disabled={position === tasks.length - 1 || sending} onClick={() => setIndex(position + 1)}><RightOutlined /></button>
        </div>}
        <div className="jx-askQ-feedback" />
        <div className="jx-askQ-actions">
          <Button size="small" type="primary" loading={sending} disabled={!answer.trim()} onClick={() => void send()}>{t('提交回答')}</Button>
        </div>
      </footer>
    </>}
  </section>;
}
