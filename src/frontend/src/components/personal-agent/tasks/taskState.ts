import { type PersonalAttention, type PersonalTask } from '../api';
import { t } from '../../../i18n';

const TERMINAL = new Set(['failed', 'cancelled', 'completed']);
const DAY = 24 * 3600 * 1000;

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

/**
 * Tasks the 需要你处理 bar lists, questions first. Failures older than a day drop out;
 * pending approvals have their own confirmation bar above the composer.
 */
export function attentionTasks(tasks: PersonalTask[], now: number) {
  const waiting = tasks.filter(task => !task.pending_approval && needsUser(task) && (task.status !== 'failed' || now - Date.parse(task.updated_at || '') < DAY));
  return [...waiting.filter(task => task.question), ...waiting.filter(task => !task.question)];
}

/** Everything that waits for the user: the bar's items plus pending approvals. */
export function attentionCount(tasks: PersonalTask[], attention: PersonalAttention | undefined, now: number) {
  return tasks.filter(task => task.pending_approval).length + attentionTasks(tasks, now).length
    + (attention?.turns.length || 0) + (attention?.deliveries.length || 0);
}

/** One line about what the agent is working on, for the header. Empty when idle. */
export function workSummary(tasks: PersonalTask[], attention: PersonalAttention | undefined, now: number) {
  const running = tasks.filter(task => task.status === 'running' || task.status === 'queued');
  if (running.length === 1) return t('正在处理：{title}', { title: taskTitle(running[0]) });
  if (running.length > 1) return t('正在处理 {count} 件事', { count: running.length });
  const count = attentionCount(tasks, attention, now);
  return count ? t('有 {count} 件事等你处理', { count }) : '';
}
