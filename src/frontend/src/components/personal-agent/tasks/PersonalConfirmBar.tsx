import { useState } from 'react';
import { message } from 'antd';
import { CloseOutlined, SafetyCertificateFilled } from '@ant-design/icons';
import { describeApproval } from './approvalText';
import { personalApi, type PersonalTask } from '../api';
import { taskTitle } from './taskState';
import { t } from '../../../i18n';

/**
 * Pending approvals above the composer, in the same bar as the HugAgentOS chat
 * (FileConfirmBar): one item at a time, the rest counted as queued.
 */
export function PersonalConfirmBar({ tasks, onDone, onError }: { tasks: PersonalTask[]; onDone: () => Promise<void>; onError: (exc: unknown) => void }) {
  const [busy, setBusy] = useState(false);
  const queue = tasks.filter(task => task.pending_approval);
  const task = queue[0];
  if (!task?.pending_approval) return null;
  const pending = task.pending_approval;
  const { op, detail, preview, steps } = describeApproval(pending.action);
  // The backend's full account of the action. Older data has none: the card then shows what it can read from the action.
  const full = pending.detail;
  const lines = [...(full?.lines || [])];
  if (full?.place && !lines.some(line => line.includes(full.place!))) lines.push(t('位置：{place}', { place: full.place }));
  if (full?.chat_title && !lines.some(line => line.includes(full.chat_title!))) lines.push(t('对话：「{title}」', { title: full.chat_title }));
  // All of the text that would go out, never cut: the owner approves what they can read to the end.
  const body = full ? full.body || '' : preview || '';
  // Email is never allowed for a whole task: every message is checked. Nor is anything a proactive run started.
  const scoped = pending.action.kind !== 'email.send' && !task.proactive;

  async function decide(accept: boolean, scope: 'once' | 'task' = 'once') {
    if (busy) return;
    setBusy(true);
    try {
      await personalApi.approve(task.id, pending.id, accept, scope);
      if (!accept) message.info(t('已拒绝本次操作。'));
      else if (scope === 'task') message.success(t('已允许本任务内的同类操作，后续不再逐项确认。'));
      await onDone();
    } catch (exc) { onError(exc); }
    finally { setBusy(false); }
  }

  return <div className={'jx-confirmBar pa-confirmBar' + (body ? ' pa-confirmBar--full' : '')} role="alertdialog" aria-label={t('操作确认')} data-testid="pa-confirm">
    <span className="jx-confirmBar-icon" aria-hidden="true"><SafetyCertificateFilled /></span>
    <div className="jx-confirmBar-body" key={pending.id}>
      <div className="jx-confirmBar-title">
        {t('需要你确认一项操作')}
        {queue.length > 1 && <span className="jx-confirmBar-count">{t('（还有 {n} 项排队，逐个确认）', { n: queue.length - 1 })}</span>}
      </div>
      {task.proactive && <div className="pa-confirmBar-unasked">{t('这是我在主动服务中自己提出的操作，不是你要求的。')}</div>}
      {lines.length ? <ul className="pa-confirmBar-lines">{lines.map((line, index) => <li key={index}>{line}</li>)}</ul>
        : <div className="jx-confirmBar-detail">
          <span className="jx-confirmBar-op">{op}</span>
          <span className="jx-confirmBar-path" title={detail}>{detail}</span>
        </div>}
      {!lines.length && steps && <ul className="pa-confirmBar-lines">{steps.map((step, index) => <li key={index}>{step}</li>)}</ul>}
      {body && <>
        <div className="pa-confirmBar-bodyLabel" id={'pa-confirm-body-' + pending.id}>{t('将要发出的内容（全文，共 {n} 字）', { n: body.length })}</div>
        <div className="pa-confirmBar-text" tabIndex={0} role="region" aria-labelledby={'pa-confirm-body-' + pending.id}>{body}</div>
      </>}
      <div className="jx-confirmBar-hint">{t('来自任务「{title}」。允许后将继续执行。', { title: taskTitle(task).slice(0, 60) })}</div>
    </div>
    <div className="jx-confirmBar-actions">
      <button type="button" className="jx-confirmBar-btn jx-confirmBar-btn--ghost" disabled={busy} onClick={() => void decide(false)}>
        <CloseOutlined className="jx-confirmBar-btnIcon" />{t('拒绝')}
      </button>
      {scoped && <button type="button" className="jx-confirmBar-btn jx-confirmBar-btn--soft" disabled={busy} onClick={() => void decide(true, 'task')}>
        {t('本任务内同类操作均允许')}
      </button>}
      <button type="button" className="jx-confirmBar-btn jx-confirmBar-btn--primary" disabled={busy} onClick={() => void decide(true)}>
        {t('允许并继续')}
      </button>
    </div>
  </div>;
}
