import type { ChatItem } from '../types';

type PlanModeChat = Pick<ChatItem, 'planChat' | 'planModeActive'>;
type BatchModeChat = Pick<ChatItem, 'batchChat' | 'batchModeActive'>;

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

/** History loading may restore the legacy default, but must respect an explicit user opt-out. */
export function shouldRestorePlanModeFromHistory(chat?: PlanModeChat): boolean {
  return chat?.planModeActive !== false;
}

/** 这段对话的模式 slug：记录上没有（老数据 / 没显式选过）= 标准模式。 */
export function resolveModeSlug(chat?: Pick<ChatItem, 'modeSlug'>): string {
  return chat?.modeSlug || 'standard';
}
