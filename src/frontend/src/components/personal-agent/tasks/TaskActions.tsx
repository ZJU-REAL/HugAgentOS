import { Button } from 'antd';
import { PersonalConfirmBar } from './PersonalConfirmBar';
import { personalApi, type PersonalTask } from '../api';
import { needsResultCheck } from './taskState';
import { t } from '../../../i18n';


/** Approval, result check and retry controls for one task. */
export function TaskActions({ task, onDone, onError }: { task: PersonalTask; onDone: () => Promise<void>; onError: (exc: unknown) => void }) {
  async function run(action: () => Promise<unknown>) {
    try { await action(); await onDone(); } catch (exc) { onError(exc); }
  }
  const pending = task.pending_approval;
  const blocked = !!pending || needsResultCheck(task);
  return <>
    {task.waiting_reason && <p>{task.waiting_reason}</p>}
    {pending && <PersonalConfirmBar tasks={[task]} onDone={onDone} onError={onError} />}
    {needsResultCheck(task) && task.uncertain_actions?.map(actionId => <div key={actionId}><p>{t('请先核对操作结果')}</p><div className="pa-task-actions">
      <Button size="small" onClick={() => void run(() => personalApi.reconcile(task.id, actionId, 'executed'))}>{t('已执行')}</Button>
      <Button size="small" onClick={() => void run(() => personalApi.reconcile(task.id, actionId, 'not_executed'))}>{t('未执行')}</Button>
    </div></div>)}
    <div className="pa-task-actions">
      {['failed', 'waiting'].includes(task.status) && !blocked && <Button size="small" onClick={() => void run(() => personalApi.retry(task.id))}>{t('重试')}</Button>}
    </div>
  </>;
}
