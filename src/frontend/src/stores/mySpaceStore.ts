import { create } from 'zustand';
import type {
  ResourceItem,
  MySpaceTab,
  AutomationNotification,
  AutomationTask,
  PersonalFolderNode,
} from '../types';
import type {
  FileScope,
  MyTeamItem,
  TeamFolderNode,
} from '../types/teamFiles';
import { scopeCacheKey } from '../types/teamFiles';
import {
  getArtifacts,
  getFavoriteChats,
  deleteArtifact,
  updateSession,
  getAutomationNotifications,
  markNotificationsRead,
  deleteNotifications,
  listSidebarAutomations,
  listMyTeamsWithPermissions,
  listTeamFolderTree,
  listTeamFiles,
  uploadTeamFile,
  deleteTeamFile,
  moveTeamFile,
  moveArtifactToTeam,
  copyArtifactToTeam,
  copyFolderToTeam,
  uploadFile,
  listPersonalFolderTree,
  createPersonalFolder,
  renamePersonalFolder,
  deletePersonalFolder,
  moveArtifactToPersonalFolder,
  copyArtifactToPersonalFolder,
} from '../api';
import { ROOT_FOLDER_SENTINEL } from '../utils/constants';
import { childrenOfFolder, findFolderById } from '../utils/folderTree';
import { useAutomationChatStore } from './automationChatStore';
import { t } from '../i18n';

export type AssetFilter = 'document' | 'image';
type SourceFilter = 'all' | 'user_upload' | 'ai_generated';

const AUTOMATION_FAVORITE_CHAT_PREFIX = 'automation:';
const AUTOMATION_FAVORITE_ITEM_PREFIX = 'favorite-automation:';

function isAutomationFavoriteChatId(chatId: string): boolean {
  return chatId.startsWith(AUTOMATION_FAVORITE_CHAT_PREFIX);
}

function isAutomationFavoriteItem(item: ResourceItem): boolean {
  return typeof item.source_chat_id === 'string' && isAutomationFavoriteChatId(item.source_chat_id);
}

function toTime(value?: string): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function getAutomationTaskTitle(task: AutomationTask): string {
  const promptTitle = task.prompt?.trim();
  return task.name?.trim()
    || task.plan_title?.trim()
    || (promptTitle ? promptTitle.slice(0, 48) : '')
    || t('自动化任务');
}

function getAutomationTaskPreview(task: AutomationTask): string {
  const description = task.description?.trim();
  const prompt = task.prompt?.trim();
  if (description) return description;
  if (prompt) return prompt;
  if (task.plan_title?.trim()) return task.plan_title.trim();
  return t('已执行 {n} 次', { n: task.run_count });
}

function matchesAutomationFavoriteKeyword(item: ResourceItem, keyword?: string): boolean {
  if (!keyword) return true;
  const q = keyword.trim().toLowerCase();
  if (!q) return true;
  return [item.name, item.source_chat_title, item.content_preview]
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    .some((value) => value.toLowerCase().includes(q));
}

function dedupeAndSortFavorites(items: ResourceItem[]): ResourceItem[] {
  const deduped = new Map<string, ResourceItem>();
  items.forEach((item) => deduped.set(item.id, item));
  return Array.from(deduped.values()).sort(
    (a, b) => toTime(b.created_at) - toTime(a.created_at),
  );
}

async function loadAutomationFavoriteItems(keyword?: string): Promise<ResourceItem[]> {
  const automationStore = useAutomationChatStore.getState();
  const favoriteTaskIds = Object.entries(automationStore.sidebarPrefs)
    .filter(([, pref]) => pref.favorite)
    .map(([taskId]) => taskId);

  if (favoriteTaskIds.length === 0) {
    return [];
  }

  let tasksById = new Map(
    automationStore.sidebarTasks.map((task) => [task.task_id, task] as const),
  );

  if (favoriteTaskIds.some((taskId) => !tasksById.has(taskId))) {
    try {
      const remoteTasks = await listSidebarAutomations();
      useAutomationChatStore.getState().setSidebarTasks(remoteTasks);
      tasksById = new Map(remoteTasks.map((task) => [task.task_id, task] as const));
    } catch (error) {
      console.error('Failed to load automation sidebar tasks for favorites:', error);
    }
  }

  return favoriteTaskIds
    .map((taskId) => tasksById.get(taskId))
    .filter((task): task is AutomationTask => !!task)
    .map((task) => {
      const title = getAutomationTaskTitle(task);
      return {
        id: `${AUTOMATION_FAVORITE_ITEM_PREFIX}${task.task_id}`,
        type: 'favorite' as const,
        name: title,
        source_chat_id: `${AUTOMATION_FAVORITE_CHAT_PREFIX}${task.task_id}`,
        source_chat_title: title,
        content_preview: getAutomationTaskPreview(task),
        created_at: task.last_run_at || task.updated_at || task.created_at,
      };
    })
    .filter((item) => matchesAutomationFavoriteKeyword(item, keyword));
}

interface MySpaceState {
  resources: ResourceItem[];
  favorites: ResourceItem[];
  loading: boolean;
  tab: MySpaceTab;
  searchKeyword: string;
  assetFilter: AssetFilter;
  sourceFilter: SourceFilter;
  page: number;
  total: number;
  hasMore: boolean;
  favPage: number;
  favTotal: number;
  favHasMore: boolean;
  // ── Team folders (new) ──
  /** File-assets inner sub-Tab: personal files / team folders */
  assetScope: 'personal' | 'team';
  selectedScope: FileScope;
  myTeams: MyTeamItem[];
  folderTreesByTeam: Record<string, TeamFolderNode[]>;
  // ── Personal folders ──
  personalFolderTree: PersonalFolderNode[];
  /** Direct child folders (used to render the list under the current personalFolderId) */
  personalChildFolders: PersonalFolderNode[];
  // Notifications
  notifications: AutomationNotification[];
  notifLoading: boolean;
  notifUnreadCount: number;
  notifSelectedIds: Set<string>;
  setTab: (tab: MySpaceTab) => void;
  setSearchKeyword: (keyword: string) => void;
  setAssetFilter: (value: AssetFilter) => void;
  setSourceFilter: (value: SourceFilter) => void;
  fetchResources: (reset?: boolean) => Promise<void>;
  fetchFavorites: (reset?: boolean) => Promise<void>;
  deleteResource: (id: string) => Promise<void>;
  unfavoriteChat: (chatId: string) => Promise<void>;
  removeFavorite: (chatId: string) => void;
  loadMore: () => Promise<void>;
  // ── Team folder actions ──
  setAssetScope: (scope: 'personal' | 'team') => void;
  loadMyTeams: () => Promise<void>;
  loadTeamFolderTree: (teamId: string) => Promise<void>;
  setScope: (scope: FileScope) => void;
  fetchScopedFiles: (reset?: boolean) => Promise<void>;
  uploadToCurrentScope: (file: File) => Promise<void>;
  deleteCurrentScopeFile: (id: string) => Promise<void>;
  movePersonalToTeam: (artifactIds: string[], teamId: string, folderId: string | null) => Promise<number>;
  copyPersonalToTeam: (artifactIds: string[], teamId: string, folderId: string | null) => Promise<number>;
  copyFolderToTeam: (personalFolderId: string, teamId: string, folderId: string | null) => Promise<{ folders: number; files: number }>;
  moveWithinTeam: (artifactId: string, targetFolderId: string | null) => Promise<void>;
  getCurrentPermission: () => 'view' | 'edit' | 'admin' | 'personal';
  // ── Personal folder actions ──
  loadPersonalFolderTree: () => Promise<void>;
  enterPersonalFolder: (folderId: string | null) => Promise<void>;
  createPersonalFolderAction: (name: string, parentFolderId: string | null) => Promise<string | null>;
  renamePersonalFolderAction: (folderId: string, name: string) => Promise<void>;
  deletePersonalFolderAction: (folderId: string) => Promise<number>;
  moveArtifactsToPersonalFolderAction: (artifactIds: string[], folderId: string | null) => Promise<number>;
  copyArtifactsToPersonalFolderAction: (artifactIds: string[], folderId: string | null) => Promise<number>;
  uploadPersonalFile: (file: File) => Promise<void>;
  // Notification actions
  fetchNotifications: () => Promise<void>;
  markNotificationRead: (id: string) => Promise<void>;
  markAllNotificationsRead: () => Promise<void>;
  markSelectedNotificationsRead: () => Promise<void>;
  deleteNotification: (id: string) => Promise<void>;
  deleteSelectedNotifications: () => Promise<void>;
  toggleNotifSelected: (id: string) => void;
  toggleNotifSelectAll: () => void;
  clearNotifSelection: () => void;
  setNotifUnreadCount: (n: number) => void;
}

const PAGE_SIZE = 20;

export const useMySpaceStore = create<MySpaceState>((set, get) => ({
  resources: [],
  favorites: [],
  loading: false,
  tab: 'assets',
  searchKeyword: '',
  assetFilter: 'document',
  sourceFilter: 'all',
  page: 1,
  total: 0,
  hasMore: false,
  favPage: 1,
  favTotal: 0,
  favHasMore: false,
  assetScope: 'personal',
  selectedScope: { kind: 'personal', folderId: null },
  myTeams: [],
  folderTreesByTeam: {},
  personalFolderTree: [],
  personalChildFolders: [],
  notifications: [],
  notifLoading: false,
  notifUnreadCount: 0,
  notifSelectedIds: new Set<string>(),
  setTab: (tab) => {
    const prev = get().tab;
    set({ tab, page: 1, favPage: 1, resources: [], favorites: [], hasMore: false, favHasMore: false });
    if (prev === 'notifications' && tab !== 'notifications') {
      set({ notifSelectedIds: new Set<string>() });
    }
    const { fetchResources, fetchFavorites, fetchNotifications } = get();
    if (tab === 'favorites') {
      void fetchFavorites(true);
    } else if (tab === 'assets') {
      void fetchResources(true);
    } else if (tab === 'notifications') {
      void fetchNotifications();
    }
  },

  setSearchKeyword: (keyword) => set({ searchKeyword: keyword }),

  setAssetFilter: (value) => {
    if (get().assetFilter === value) return;
    set({ assetFilter: value, page: 1, resources: [], hasMore: false });
    void get().fetchResources(true);
  },

  setSourceFilter: (value) => {
    if (get().sourceFilter === value) return;
    set({ sourceFilter: value, page: 1, resources: [], hasMore: false });
    void get().fetchResources(true);
  },

  fetchResources: async (reset = false) => {
    // Uniformly use the scope-aware version
    await get().fetchScopedFiles(reset);
  },

  fetchScopedFiles: async (reset = false) => {
    const { selectedScope, searchKeyword, page, resources, assetFilter, sourceFilter } = get();
    const currentPage = reset ? 1 : page;
    set({ loading: true });
    try {
      if (selectedScope.kind === 'personal') {
        const folderId = selectedScope.folderId ?? null;
        const res = await getArtifacts({
          type: assetFilter,
          source_kind: sourceFilter === 'all' ? undefined : sourceFilter,
          keyword: searchKeyword || undefined,
          scope: 'personal',
          folder_id: folderId ?? ROOT_FOLDER_SENTINEL,
          page: currentPage,
          page_size: PAGE_SIZE,
        });
        const items = res.items || [];
        set({
          resources: reset ? items : [...resources, ...items],
          total: res.total,
          page: currentPage,
          hasMore: res.has_more,
          loading: false,
        });
      } else {
        const res = await listTeamFiles({
          teamId: selectedScope.teamId,
          folderId: selectedScope.folderId,
          type: assetFilter,
          keyword: searchKeyword || undefined,
          page: currentPage,
          page_size: PAGE_SIZE,
        });
        const items = res.items || [];
        set({
          resources: reset ? items : [...resources, ...items],
          total: res.total,
          page: currentPage,
          hasMore: res.has_more,
          loading: false,
        });
      }
    } catch (e) {
      console.error('Failed to fetch files for scope:', e);
      set({ loading: false });
    }
  },

  loadMyTeams: async () => {
    try {
      const teams = await listMyTeamsWithPermissions();
      set({ myTeams: teams });
    } catch (e) {
      console.error('Failed to load my teams:', e);
    }
  },

  loadTeamFolderTree: async (teamId: string) => {
    try {
      const tree = await listTeamFolderTree(teamId);
      set((s) => ({ folderTreesByTeam: { ...s.folderTreesByTeam, [teamId]: tree } }));
    } catch (e) {
      console.error('Failed to load team folder tree:', e);
    }
  },

  setScope: (scope: FileScope) => {
    if (scopeCacheKey(get().selectedScope) === scopeCacheKey(scope)) return;
    set({ selectedScope: scope, resources: [], page: 1, hasMore: false });
    void get().fetchScopedFiles(true);
  },

  setAssetScope: (scope: 'personal' | 'team') => {
    if (get().assetScope === scope) return;
    if (scope === 'personal') {
      set({
        assetScope: 'personal',
        selectedScope: { kind: 'personal', folderId: null },
        resources: [],
        page: 1,
        hasMore: false,
      });
      void get().loadPersonalFolderTree();
    } else {
      // Enter team: if the current selectedScope is already a team, keep it; otherwise default back to the team list (no selection)
      const current = get().selectedScope;
      const next: FileScope = current.kind === 'team' ? current : { kind: 'team', teamId: '', folderId: null };
      set({ assetScope: 'team', selectedScope: next, resources: [], page: 1, hasMore: false });
      // Fetch the teams list for left-side rendering
      void get().loadMyTeams();
    }
    if (scope === 'personal' || (scope === 'team' && get().selectedScope.kind === 'team' && (get().selectedScope as any).teamId)) {
      void get().fetchScopedFiles(true);
    }
  },

  uploadToCurrentScope: async (file: File) => {
    const { selectedScope } = get();
    if (selectedScope.kind !== 'team') {
      throw new Error(t('当前作用域不支持上传'));
    }
    await uploadTeamFile(selectedScope.teamId, selectedScope.folderId, file);
    await get().fetchScopedFiles(true);
  },

  deleteCurrentScopeFile: async (id: string) => {
    const { selectedScope } = get();
    if (selectedScope.kind === 'personal') {
      await deleteArtifact(id);
    } else {
      await deleteTeamFile(selectedScope.teamId, id);
    }
    set((state) => ({
      resources: state.resources.filter((r) => r.id !== id),
      total: Math.max(0, state.total - 1),
    }));
  },

  movePersonalToTeam: async (artifactIds, teamId, folderId) => {
    const results = await Promise.allSettled(
      artifactIds.map((id) => moveArtifactToTeam(id, teamId, folderId)),
    );
    const moved = results.filter((r) => r.status === 'fulfilled').length;
    results.forEach((r, idx) => {
      if (r.status === 'rejected') {
        console.error('move_to_team failed for', artifactIds[idx], r.reason);
      }
    });
    if (moved > 0) {
      await get().fetchScopedFiles(true);
    }
    return moved;
  },

  copyPersonalToTeam: async (artifactIds, teamId, folderId) => {
    const results = await Promise.allSettled(
      artifactIds.map((id) => copyArtifactToTeam(id, teamId, folderId)),
    );
    const copied = results.filter((r) => r.status === 'fulfilled').length;
    results.forEach((r, idx) => {
      if (r.status === 'rejected') {
        console.error('copy_to_team failed for', artifactIds[idx], r.reason);
      }
    });
    // Copy is non-destructive; the personal list is unchanged, no need to refresh the current scope
    return copied;
  },

  copyFolderToTeam: async (personalFolderId, teamId, folderId) => {
    return copyFolderToTeam(personalFolderId, teamId, folderId);
  },

  moveWithinTeam: async (artifactId, targetFolderId) => {
    const { selectedScope } = get();
    if (selectedScope.kind !== 'team') return;
    await moveTeamFile(selectedScope.teamId, artifactId, targetFolderId);
    await get().fetchScopedFiles(true);
  },

  getCurrentPermission: () => {
    const { selectedScope, myTeams } = get();
    if (selectedScope.kind === 'personal') return 'personal';
    const found = myTeams.find((t) => t.team_id === selectedScope.teamId);
    if (!found) return 'view';
    return found.resolved === 'none' ? 'view' : found.resolved;
  },

  // ── Personal folder actions ──────────────────────────────────────
  loadPersonalFolderTree: async () => {
    try {
      const tree = await listPersonalFolderTree();
      set({ personalFolderTree: tree });
      // Recompute child folders based on the current folderId
      const { selectedScope } = get();
      if (selectedScope.kind === 'personal') {
        set({ personalChildFolders: childrenOfFolder(tree, selectedScope.folderId ?? null) });
      }
    } catch (e) {
      console.error('Failed to load personal folder tree:', e);
    }
  },

  enterPersonalFolder: async (folderId: string | null) => {
    const { selectedScope, personalFolderTree } = get();
    if (selectedScope.kind === 'personal' && (selectedScope.folderId ?? null) === folderId) {
      return;
    }
    set({
      selectedScope: { kind: 'personal', folderId },
      personalChildFolders: childrenOfFolder(personalFolderTree, folderId),
      resources: [],
      page: 1,
      hasMore: false,
    });
    await get().fetchScopedFiles(true);
  },

  createPersonalFolderAction: async (name, parentFolderId) => {
    try {
      const res = await createPersonalFolder(name, parentFolderId);
      // Refresh the tree + child folders at the current level
      await get().loadPersonalFolderTree();
      return res.folder_id;
    } catch (e) {
      console.error('Failed to create personal folder:', e);
      throw e;
    }
  },

  renamePersonalFolderAction: async (folderId, name) => {
    await renamePersonalFolder(folderId, name);
    await get().loadPersonalFolderTree();
  },

  deletePersonalFolderAction: async (folderId) => {
    const res = await deletePersonalFolder(folderId);
    // Before refreshing the tree, use the old tree to determine whether we are currently on a deleted branch; reload after deletion.
    const { selectedScope, personalFolderTree } = get();
    let onDeletedPath = false;
    if (selectedScope.kind === 'personal' && selectedScope.folderId) {
      let cur: string | null = selectedScope.folderId;
      while (cur) {
        if (cur === folderId) { onDeletedPath = true; break; }
        const node: PersonalFolderNode | null = findFolderById(personalFolderTree, cur);
        cur = node?.parent_folder_id ?? null;
      }
    }
    await get().loadPersonalFolderTree();
    if (onDeletedPath) {
      await get().enterPersonalFolder(null);
    } else if (selectedScope.kind === 'personal') {
      await get().fetchScopedFiles(true);
    }
    return res.artifacts_affected;
  },

  moveArtifactsToPersonalFolderAction: async (artifactIds, folderId) => {
    const results = await Promise.allSettled(
      artifactIds.map((id) => moveArtifactToPersonalFolder(id, folderId)),
    );
    const moved = results.filter((r) => r.status === 'fulfilled').length;
    results.forEach((r, idx) => {
      if (r.status === 'rejected') {
        console.error('move_to_personal_folder failed for', artifactIds[idx], r.reason);
      }
    });
    if (moved > 0) {
      await get().fetchScopedFiles(true);
    }
    return moved;
  },

  copyArtifactsToPersonalFolderAction: async (artifactIds, folderId) => {
    const results = await Promise.allSettled(
      artifactIds.map((id) => copyArtifactToPersonalFolder(id, folderId)),
    );
    const copied = results.filter((r) => r.status === 'fulfilled').length;
    results.forEach((r, idx) => {
      if (r.status === 'rejected') {
        console.error('copy_to_personal_folder failed for', artifactIds[idx], r.reason);
      }
    });
    // The copy lands in the current view (a new copy in the same space), so the current scope must be refreshed
    if (copied > 0) {
      await get().fetchScopedFiles(true);
    }
    return copied;
  },

  uploadPersonalFile: async (file) => {
    const { selectedScope } = get();
    const folderId = selectedScope.kind === 'personal' ? (selectedScope.folderId ?? null) : null;
    await uploadFile(file, undefined, folderId);
    await get().fetchScopedFiles(true);
  },

  fetchFavorites: async (reset = false) => {
    const { searchKeyword, favPage, favorites } = get();
    const currentPage = reset ? 1 : favPage;
    set({ loading: true });
    try {
      const [res, automationItems] = await Promise.all([
        getFavoriteChats({
          keyword: searchKeyword || undefined,
          page: currentPage,
          page_size: PAGE_SIZE,
        }),
        loadAutomationFavoriteItems(searchKeyword || undefined),
      ]);
      const items = res.items || [];
      const existingBackendItems = reset
        ? []
        : favorites.filter((item) => !isAutomationFavoriteItem(item));
      set({
        favorites: dedupeAndSortFavorites([...automationItems, ...existingBackendItems, ...items]),
        favTotal: res.total + automationItems.length,
        favPage: currentPage,
        favHasMore: res.has_more,
        loading: false,
      });
    } catch (e) {
      console.error('Failed to fetch favorites:', e);
      set({ loading: false });
    }
  },

  deleteResource: async (id) => {
    try {
      const { selectedScope } = get();
      if (selectedScope.kind === 'team') {
        await deleteTeamFile(selectedScope.teamId, id);
      } else {
        await deleteArtifact(id);
      }
      set((state) => ({
        resources: state.resources.filter((r) => r.id !== id),
        total: Math.max(0, state.total - 1),
      }));
    } catch (e) {
      console.error('Failed to delete artifact:', e);
    }
  },

  unfavoriteChat: async (chatId) => {
    try {
      if (isAutomationFavoriteChatId(chatId)) {
        const taskId = chatId.slice(AUTOMATION_FAVORITE_CHAT_PREFIX.length);
        useAutomationChatStore.getState().setSidebarFavorite(taskId, false);
        return;
      }
      await updateSession(chatId, { favorite: false });
    } catch (e) {
      console.error('Failed to unfavorite chat:', e);
      throw e;
    }
  },

  removeFavorite: (chatId) => {
    set((state) => {
      const exists = state.favorites.some((item) => item.source_chat_id === chatId);
      return {
        favorites: state.favorites.filter((item) => item.source_chat_id !== chatId),
        favTotal: exists ? Math.max(0, state.favTotal - 1) : state.favTotal,
      };
    });
  },

  loadMore: async () => {
    const { tab, page, hasMore, favPage, favHasMore, loading, fetchResources, fetchFavorites } = get();
    if (loading) return; // prevent concurrent fetches from rapid scroll
    if (tab === 'favorites') {
      if (!favHasMore) return;
      set({ favPage: favPage + 1 });
      await fetchFavorites();
    } else if (tab === 'assets') {
      if (!hasMore) return;
      set({ page: page + 1 });
      await fetchResources();
    }
  },

  // ── Notification actions ──────────────────────────────────────

  fetchNotifications: async () => {
    set({ notifLoading: true });
    try {
      const notifications = await getAutomationNotifications();
      const unread = notifications.filter((n) => !n.read).length;
      set({ notifications, notifUnreadCount: unread, notifLoading: false });
    } catch (e) {
      console.error('Failed to fetch notifications:', e);
      set({ notifLoading: false });
    }
  },

  markNotificationRead: async (id: string) => {
    try {
      await markNotificationsRead([id]);
      set((s) => {
        const updated = s.notifications.map((n) => (n.id === id ? { ...n, read: true } : n));
        return { notifications: updated, notifUnreadCount: updated.filter((n) => !n.read).length };
      });
    } catch (e) {
      console.error('Failed to mark notification read:', e);
    }
  },

  markAllNotificationsRead: async () => {
    const { notifications } = get();
    const unreadIds = notifications.filter((n) => !n.read).map((n) => n.id);
    if (unreadIds.length === 0) return;
    try {
      await markNotificationsRead(unreadIds);
      set((s) => ({
        notifications: s.notifications.map((n) => ({ ...n, read: true })),
        notifUnreadCount: 0,
      }));
    } catch (e) {
      console.error('Failed to mark all notifications read:', e);
    }
  },

  markSelectedNotificationsRead: async () => {
    const { notifSelectedIds, notifications } = get();
    const ids = notifications
      .filter((n) => notifSelectedIds.has(n.id) && !n.read)
      .map((n) => n.id);
    if (ids.length === 0) return;
    try {
      await markNotificationsRead(ids);
      const idsSet = new Set(ids);
      set((s) => {
        const updated = s.notifications.map((n) =>
          idsSet.has(n.id) ? { ...n, read: true } : n,
        );
        return {
          notifications: updated,
          notifUnreadCount: updated.filter((n) => !n.read).length,
          notifSelectedIds: new Set<string>(),
        };
      });
    } catch (e) {
      console.error('Failed to mark selected notifications read:', e);
    }
  },

  deleteNotification: async (id: string) => {
    try {
      await deleteNotifications([id]);
      set((s) => {
        const updated = s.notifications.filter((n) => n.id !== id);
        const sel = new Set(s.notifSelectedIds);
        sel.delete(id);
        return {
          notifications: updated,
          notifUnreadCount: updated.filter((n) => !n.read).length,
          notifSelectedIds: sel,
        };
      });
    } catch (e) {
      console.error('Failed to delete notification:', e);
    }
  },

  deleteSelectedNotifications: async () => {
    const { notifSelectedIds } = get();
    const ids = Array.from(notifSelectedIds);
    if (ids.length === 0) return;
    try {
      await deleteNotifications(ids);
      set((s) => {
        const updated = s.notifications.filter((n) => !notifSelectedIds.has(n.id));
        return {
          notifications: updated,
          notifUnreadCount: updated.filter((n) => !n.read).length,
          notifSelectedIds: new Set<string>(),
        };
      });
    } catch (e) {
      console.error('Failed to delete selected notifications:', e);
    }
  },

  toggleNotifSelected: (id: string) => {
    set((s) => {
      const next = new Set(s.notifSelectedIds);
      if (next.has(id)) next.delete(id); else next.add(id);
      return { notifSelectedIds: next };
    });
  },

  toggleNotifSelectAll: () => {
    set((s) => {
      const allIds = s.notifications.map((n) => n.id);
      const allSelected = allIds.length > 0 && allIds.every((id) => s.notifSelectedIds.has(id));
      return { notifSelectedIds: allSelected ? new Set<string>() : new Set(allIds) };
    });
  },

  clearNotifSelection: () => {
    if (get().notifSelectedIds.size === 0) return;
    set({ notifSelectedIds: new Set<string>() });
  },

  setNotifUnreadCount: (n: number) => set({ notifUnreadCount: n }),
}));
