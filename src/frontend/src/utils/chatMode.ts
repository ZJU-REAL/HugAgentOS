import type { ChatItem } from '../types';

type PlanModeChat = Pick<ChatItem, 'planChat' | 'planModeActive'>;
type BatchModeChat = Pick<ChatItem, 'batchChat' | 'batchModeActive'>;
type WorkflowModeChat = Pick<ChatItem, 'workflowChat' | 'workflowModeActive'>;

/** Resolve the composer routing mode independently from the chat's historical plan marker. */
export function resolvePlanModeActive(chat?: PlanModeChat): boolean {
  if (!chat) return false;
  if (typeof chat.planModeActive === 'boolean') return chat.planModeActive;
  return chat.planChat === true;
}

/** Resolve batch-execution routing the same way: the historical batchChat marker is only the
 *  default, an explicit batchModeActive === false means the user closed the mode. */
export function resolveBatchModeActive(chat?: BatchModeChat): boolean {
  if (!chat) return false;
  if (typeof chat.batchModeActive === 'boolean') return chat.batchModeActive;
  return chat.batchChat === true;
}

/** 工作流模式（批量作业）同理：历史标记只是默认值，显式 workflowModeActive === false 表示用户关掉了。
 *  这是**用户显式触发**的模式——不触发就不注册 run_job、不注入批量提示，普通问答完全不受影响。 */
export function resolveWorkflowModeActive(chat?: WorkflowModeChat): boolean {
  if (!chat) return false;
  if (typeof chat.workflowModeActive === 'boolean') return chat.workflowModeActive;
  return chat.workflowChat === true;
}

/** History loading may restore the legacy default, but must respect an explicit user opt-out. */
export function shouldRestorePlanModeFromHistory(chat?: PlanModeChat): boolean {
  return chat?.planModeActive !== false;
}

/** 这段对话的模式 slug：记录上没有（老数据 / 没显式选过）= 标准模式。 */
export function resolveModeSlug(chat?: Pick<ChatItem, 'modeSlug'>): string {
  return chat?.modeSlug || 'standard';
}
