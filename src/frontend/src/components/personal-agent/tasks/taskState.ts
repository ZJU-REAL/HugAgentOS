import { type PersonalTask } from '../api';
import { t } from '../../../i18n';

const TERMINAL = new Set(['failed', 'cancelled', 'completed']);

/** True when the task will not run again on its own. */
export function isTerminal(task: PersonalTask) {
  return TERMINAL.has(task.status);
}

/** Only stopped tasks can ask the owner to reconcile an unknown result. */
export function needsResultCheck(task: PersonalTask) {
  return task.status === 'waiting' && !!task.uncertain_actions?.length;
}

/** True when the user must act before the task can continue. */
export function needsUser(task: PersonalTask) {
  return !!task.pending_approval || task.status === 'waiting' || task.status === 'failed';
}

/** The short name the agent gave the task, or the start of its goal. */
export function taskTitle(task: PersonalTask) {
  return task.title || task.goal;
}

/** One line about what the agent is working on, for the header. Empty when idle. */
export function workSummary(tasks: PersonalTask[]) {
  const running = tasks.filter(task => task.status === 'running' || task.status === 'queued');
  if (running.length === 1) return t('正在处理：{title}', { title: taskTitle(running[0]) });
  if (running.length > 1) return t('正在处理 {count} 件事', { count: running.length });
  const waiting = tasks.filter(task => needsUser(task) && task.status !== 'failed');
  return waiting.length ? t('有 {count} 件事等你处理', { count: waiting.length }) : '';
}
