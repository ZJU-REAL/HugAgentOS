import type { ChatItem } from '../../types';
import { isAutomationHistoryChat } from '../../utils/history';

export interface AutomationSidebarEntry {
  item: ChatItem;
  targetChatId: string;
  chatIds: string[];
}

/** Keep a stable conversation as the sidebar anchor; navigation opens the newest execution. */
export function buildAutomationSidebarEntries(chats: ChatItem[]): AutomationSidebarEntry[] {
  const groups = new Map<string, ChatItem[]>();
  for (const chat of chats) {
    if (!isAutomationHistoryChat(chat)) continue;
    if (chat.automationTaskId === 'new' && chat.messages.length === 0) continue;
    // Setup conversations and legacy runs have no individual task identity.
    const key = chat.automationTaskId && chat.automationTaskId !== 'new'
      ? `task:${chat.automationTaskId}` : `chat:${chat.id}`;
    const group = groups.get(key);
    if (group) group.push(chat);
    else groups.set(key, [chat]);
  }
  return Array.from(groups.values(), group => {
    const chronological = [...group].sort((a, b) => a.createdAt - b.createdAt || a.id.localeCompare(b.id));
    const anchor = chronological[0];
    const latest = chronological[chronological.length - 1];
    return {
      item: { ...anchor, updatedAt: Math.max(...group.map(chat => chat.updatedAt)) },
      targetChatId: latest.id,
      chatIds: chronological.map(chat => chat.id),
    };
  });
}
