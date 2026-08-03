import type { ChatItem } from '../types';

type PlanModeChat = Pick<ChatItem, 'planChat' | 'planModeActive'>;

/** Resolve the composer routing mode independently from the chat's historical plan marker. */
export function resolvePlanModeActive(chat?: PlanModeChat): boolean {
  if (!chat) return false;
  if (typeof chat.planModeActive === 'boolean') return chat.planModeActive;
  return chat.planChat === true;
}

/** History loading may restore the legacy default, but must respect an explicit user opt-out. */
export function shouldRestorePlanModeFromHistory(chat?: PlanModeChat): boolean {
  return chat?.planModeActive !== false;
}
