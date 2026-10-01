import { useCallback, useMemo } from 'react';
import { t } from '../../i18n';
import { useChatStore, useSidebarOrderStore } from '../../stores';
import { useProjectStore } from '../../stores/projectStore';
import type { ChatItem } from '../../types';
import { isAutomationHistoryChat } from '../../utils/history';
import { compareSidebarItems } from '../../utils/sidebarOrder';
type HistoryGroupKey = 'history';
export interface SidebarProjectGroup {
  projectId: string;
  name: string;
  pinned: boolean;
  canAdmin: boolean;
  /** Project creator or team owner: the only roles allowed to remove the project. */
  canDelete: boolean;
  /** Project is known in the project list (false = only reconstructed as a fallback from leftover chat.projectId) */
  known: boolean;
  isLocal: boolean;
  isTeam: boolean;
  items: ChatItem[];
  lastActivity: number;
}

export function useSidebarHistory() {
  const store = useChatStore(s => s.store);
  const projects = useProjectStore(s => s.list);
  const manualOrder = useSidebarOrderStore(s => s.order);
  const sortedHistoryList = useMemo(() => {
    const index = new Map(manualOrder.map((id, i) => [id, i]));
    return store.order.map(id => store.chats[id]).filter(item => item && !isAutomationHistoryChat(item)).sort((a, b) => compareSidebarItems(a, b, index));
  }, [store, manualOrder]);
  const knownProjectIds = useMemo(
    () => new Set(projects.map((p) => p.project_id)),
    [projects],
  );

  /** Whether a chat is a "project orphan": its bound project is neither in the project list nor has a locally cached project name
   *  (typical case: the project was deleted, and the chat fetched back from the backend only has a project_id). Orphans fall back to History,
   *  avoiding a nameless fallback project group in the sidebar. */
  const isProjectOrphan = useCallback(
    (item: ChatItem) =>
      !!item.projectId && !knownProjectIds.has(item.projectId) && !item.projectName,
    [knownProjectIds],
  );

  // Grouping: Automation (shown only when there are tasks) + History (title always shown).
  // Chats belonging to a project don't go into the History group — they're nested under their respective projects in the "Projects" section above;
  // project orphans (see isProjectOrphan) are the exception and fall back to History.
  // Pinned items are no longer a separate group; per the sort above they appear at the top of the History group + rendered with 📌.
  const groupedHistoryList = useMemo(() => {
    const historyItems = sortedHistoryList.filter(
      (item) => !item.automationRun && (!item.projectId || isProjectOrphan(item)));
    const result: Array<{ key: HistoryGroupKey; label: string; items: ChatItem[] }> = [];
    result.push({ key: 'history', label: t('历史对话'), items: historyItems });
    return result;
  }, [sortedHistoryList, isProjectOrphan]);

  // "Projects" section: one group per project in the project list, with its owned chats attached (reusing the pinned+updated-time sort).
  // When the project list doesn't yet contain a projectId (not fetched / lost access) but a chat has cached a project name, use that name
  // to build a fallback group, so these chats don't vanish from the sidebar; orphans without even a name fall back to History.
  const projectGroups = useMemo<SidebarProjectGroup[]>(() => {
    const chatsByProject = new Map<string, ChatItem[]>();
    for (const item of sortedHistoryList) {
      if (item.automationRun || !item.projectId || isProjectOrphan(item)) continue;
      const arr = chatsByProject.get(item.projectId);
      if (arr) arr.push(item); else chatsByProject.set(item.projectId, [item]);
    }
    const groups: SidebarProjectGroup[] = projects.map((p) => {
      const items = chatsByProject.get(p.project_id) || [];
      return {
        projectId: p.project_id,
        name: p.name,
        pinned: !!p.pinned,
        canAdmin: p.permission === 'admin',
        canDelete: !!p.is_owner,
        known: true,
        isLocal: (p.kind as string) === "local",
        isTeam: false,
        items,
        lastActivity: Math.max(
          p.last_activity_at ? new Date(p.last_activity_at).getTime() : 0,
          ...items.map((i) => i.updatedAt || 0),
        ),
      };
    });
    for (const [pid, items] of chatsByProject) {
      if (knownProjectIds.has(pid)) continue;
      groups.push({
        projectId: pid,
        name: items.find((i) => i.projectName)?.projectName || t('项目'),
        pinned: false,
        canAdmin: false,
        canDelete: false,
        known: false,
        isLocal: false,
        isTeam: false,
        items,
        lastActivity: Math.max(...items.map((i) => i.updatedAt || 0)),
      });
    }
    groups.sort((a, b) =>
      (Number(b.pinned) - Number(a.pinned)) || (b.lastActivity - a.lastActivity));
    return groups;
  }, [sortedHistoryList, projects, knownProjectIds, isProjectOrphan]);

  return { projectGroups, groupedHistoryList };
}
