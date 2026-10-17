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
  const { op, detail, preview } = describeApproval(pending.action);
  // Email is never allowed for a whole task: every message is checked.
  const scoped = pending.action.kind !== 'email.send';

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

  return <div className="jx-confirmBar pa-confirmBar" role="alertdialog" aria-label={t('操作确认')} data-testid="pa-confirm">
    <span className="jx-confirmBar-icon" aria-hidden="true"><SafetyCertificateFilled /></span>
    <div className="jx-confirmBar-body" key={pending.id}>
      <div className="jx-confirmBar-title">
        {t('需要你确认一项操作')}
        {queue.length > 1 && <span className="jx-confirmBar-count">{t('（还有 {n} 项排队，逐个确认）', { n: queue.length - 1 })}</span>}
      </div>
      <div className="jx-confirmBar-detail">
        <span className="jx-confirmBar-op">{op}</span>
        <span className="jx-confirmBar-path" title={detail}>{detail}</span>
      </div>
      {preview && <div className="pa-confirmBar-preview">{preview}</div>}
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
