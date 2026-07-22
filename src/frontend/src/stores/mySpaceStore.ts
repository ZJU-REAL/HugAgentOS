import { create } from 'zustand';

import type {
  AutomationNotification,
  AutomationTask,
  MySpaceTab,
  PersonalFolderNode,
  ResourceItem,
} from '../types';
import {
  copyArtifactToPersonalFolder,
  createPersonalFolder,
  deleteArtifact,
  deleteNotifications,
  deletePersonalFolder,
  getArtifacts,
  getAutomationNotifications,
  getFavoriteChats,
  listPersonalFolderTree,
  listSidebarAutomations,
  markNotificationsRead,
  moveArtifactToPersonalFolder,
  renamePersonalFolder,
  updateSession,
  uploadFile,
} from '../api';
import { t } from '../i18n';
import { ROOT_FOLDER_SENTINEL } from '../utils/constants';
import { childrenOfFolder, findFolderById } from '../utils/folderTree';
import { useAutomationChatStore } from './automationChatStore';

export type AssetFilter = 'document' | 'image';
type SourceFilter = 'all' | 'user_upload' | 'ai_generated';
type PersonalScope = { kind: 'personal'; folderId: string | null };

const PAGE_SIZE = 20;
const AUTOMATION_FAVORITE_CHAT_PREFIX = 'automation:';
const AUTOMATION_FAVORITE_ITEM_PREFIX = 'favorite-automation:';

function isAutomationFavoriteChatId(chatId: string): boolean {
  return chatId.startsWith(AUTOMATION_FAVORITE_CHAT_PREFIX);
}

function isAutomationFavoriteItem(item: ResourceItem): boolean {
  return typeof item.source_chat_id === 'string'
    && isAutomationFavoriteChatId(item.source_chat_id);
}

function toTime(value?: string): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function taskTitle(task: AutomationTask): string {
  const promptTitle = task.prompt?.trim();
  return task.name?.trim()
    || task.plan_title?.trim()
    || (promptTitle ? promptTitle.slice(0, 48) : '')
    || t('自动化任务');
}

function taskPreview(task: AutomationTask): string {
  return task.description?.trim()
    || task.prompt?.trim()
    || task.plan_title?.trim()
    || t('已执行 {n} 次', { n: task.run_count });
}

function matchesKeyword(item: ResourceItem, keyword?: string): boolean {
  const query = keyword?.trim().toLowerCase();
  if (!query) return true;
  return [item.name, item.source_chat_title, item.content_preview]
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    .some((value) => value.toLowerCase().includes(query));
}

function dedupeAndSort(items: ResourceItem[]): ResourceItem[] {
  const deduped = new Map<string, ResourceItem>();
  items.forEach((item) => deduped.set(item.id, item));
  return Array.from(deduped.values()).sort(
    (left, right) => toTime(right.created_at) - toTime(left.created_at),
  );
}

async function loadAutomationFavorites(keyword?: string): Promise<ResourceItem[]> {
  const automationStore = useAutomationChatStore.getState();
  const favoriteIds = Object.entries(automationStore.sidebarPrefs)
    .filter(([, preference]) => preference.favorite)
    .map(([taskId]) => taskId);
  if (favoriteIds.length === 0) return [];

  let tasksById = new Map(
    automationStore.sidebarTasks.map((task) => [task.task_id, task] as const),
  );
  if (favoriteIds.some((taskId) => !tasksById.has(taskId))) {
    try {
      const remoteTasks = await listSidebarAutomations();
      automationStore.setSidebarTasks(remoteTasks);
      tasksById = new Map(remoteTasks.map((task) => [task.task_id, task] as const));
    } catch (error) {
      console.error('Failed to load automation sidebar tasks for favorites:', error);
    }
  }

  return favoriteIds
    .map((taskId) => tasksById.get(taskId))
    .filter((task): task is AutomationTask => !!task)
    .map((task) => {
      const title = taskTitle(task);
      return {
        id: `${AUTOMATION_FAVORITE_ITEM_PREFIX}${task.task_id}`,
        type: 'favorite' as const,
        name: title,
        source_chat_id: `${AUTOMATION_FAVORITE_CHAT_PREFIX}${task.task_id}`,
        source_chat_title: title,
        content_preview: taskPreview(task),
        created_at: task.last_run_at || task.updated_at || task.created_at,
      };
    })
    .filter((item) => matchesKeyword(item, keyword));
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
  selectedScope: PersonalScope;
  personalFolderTree: PersonalFolderNode[];
  personalChildFolders: PersonalFolderNode[];
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
  loadPersonalFolderTree: () => Promise<void>;
  enterPersonalFolder: (folderId: string | null) => Promise<void>;
  createPersonalFolderAction: (name: string, parentFolderId: string | null) => Promise<string | null>;
  renamePersonalFolderAction: (folderId: string, name: string) => Promise<void>;
  deletePersonalFolderAction: (folderId: string) => Promise<number>;
  moveArtifactsToPersonalFolderAction: (artifactIds: string[], folderId: string | null) => Promise<number>;
  copyArtifactsToPersonalFolderAction: (artifactIds: string[], folderId: string | null) => Promise<number>;
  uploadPersonalFile: (file: File) => Promise<void>;
  fetchNotifications: () => Promise<void>;
  markNotificationRead: (id: string) => Promise<void>;
  markAllNotificationsRead: () => Promise<void>;
  markSelectedNotificationsRead: () => Promise<void>;
  deleteNotification: (id: string) => Promise<void>;
  deleteSelectedNotifications: () => Promise<void>;
  toggleNotifSelected: (id: string) => void;
  toggleNotifSelectAll: () => void;
  clearNotifSelection: () => void;
  setNotifUnreadCount: (count: number) => void;
}

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
  selectedScope: { kind: 'personal', folderId: null },
  personalFolderTree: [],
  personalChildFolders: [],
  notifications: [],
  notifLoading: false,
  notifUnreadCount: 0,
  notifSelectedIds: new Set<string>(),

  setTab: (tab) => {
    const previous = get().tab;
    set({ tab, page: 1, favPage: 1, resources: [], favorites: [], hasMore: false, favHasMore: false });
    if (previous === 'notifications' && tab !== 'notifications') {
      set({ notifSelectedIds: new Set<string>() });
    }
    if (tab === 'favorites') void get().fetchFavorites(true);
    if (tab === 'assets') void get().fetchResources(true);
    if (tab === 'notifications') void get().fetchNotifications();
  },
  setSearchKeyword: (searchKeyword) => set({ searchKeyword }),
  setAssetFilter: (assetFilter) => {
    if (get().assetFilter === assetFilter) return;
    set({ assetFilter, page: 1, resources: [], hasMore: false });
    void get().fetchResources(true);
  },
  setSourceFilter: (sourceFilter) => {
    if (get().sourceFilter === sourceFilter) return;
    set({ sourceFilter, page: 1, resources: [], hasMore: false });
    void get().fetchResources(true);
  },

  fetchResources: async (reset = false) => {
    const { selectedScope, searchKeyword, page, resources, assetFilter, sourceFilter } = get();
    const currentPage = reset ? 1 : page;
    set({ loading: true });
    try {
      const result = await getArtifacts({
        type: assetFilter,
        source_kind: sourceFilter === 'all' ? undefined : sourceFilter,
        keyword: searchKeyword || undefined,
        scope: 'personal',
        folder_id: selectedScope.folderId ?? ROOT_FOLDER_SENTINEL,
        page: currentPage,
        page_size: PAGE_SIZE,
      });
      const items = result.items || [];
      set({
        resources: reset ? items : [...resources, ...items],
        total: result.total,
        page: currentPage,
        hasMore: result.has_more,
        loading: false,
      });
    } catch (error) {
      console.error('Failed to fetch personal files:', error);
      set({ loading: false });
    }
  },

  loadPersonalFolderTree: async () => {
    try {
      const tree = await listPersonalFolderTree();
      set({ personalFolderTree: tree });
      set({ personalChildFolders: childrenOfFolder(tree, get().selectedScope.folderId) });
    } catch (error) {
      console.error('Failed to load personal folder tree:', error);
    }
  },

  enterPersonalFolder: async (folderId) => {
    if (get().selectedScope.folderId === folderId) return;
    set({
      selectedScope: { kind: 'personal', folderId },
      personalChildFolders: childrenOfFolder(get().personalFolderTree, folderId),
      resources: [],
      page: 1,
      hasMore: false,
    });
    await get().fetchResources(true);
  },

  createPersonalFolderAction: async (name, parentFolderId) => {
    const result = await createPersonalFolder(name, parentFolderId);
    await get().loadPersonalFolderTree();
    return result.folder_id;
  },
  renamePersonalFolderAction: async (folderId, name) => {
    await renamePersonalFolder(folderId, name);
    await get().loadPersonalFolderTree();
  },
  deletePersonalFolderAction: async (folderId) => {
    const result = await deletePersonalFolder(folderId);
    const { selectedScope, personalFolderTree } = get();
    let onDeletedPath = false;
    let current = selectedScope.folderId;
    while (current) {
      if (current === folderId) {
        onDeletedPath = true;
        break;
      }
      current = findFolderById(personalFolderTree, current)?.parent_folder_id ?? null;
    }
    await get().loadPersonalFolderTree();
    if (onDeletedPath) await get().enterPersonalFolder(null);
    else await get().fetchResources(true);
    return result.artifacts_affected;
  },
  moveArtifactsToPersonalFolderAction: async (artifactIds, folderId) => {
    const results = await Promise.allSettled(
      artifactIds.map((artifactId) => moveArtifactToPersonalFolder(artifactId, folderId)),
    );
    const moved = results.filter((result) => result.status === 'fulfilled').length;
    if (moved > 0) await get().fetchResources(true);
    return moved;
  },
  copyArtifactsToPersonalFolderAction: async (artifactIds, folderId) => {
    const results = await Promise.allSettled(
      artifactIds.map((artifactId) => copyArtifactToPersonalFolder(artifactId, folderId)),
    );
    const copied = results.filter((result) => result.status === 'fulfilled').length;
    if (copied > 0) await get().fetchResources(true);
    return copied;
  },
  uploadPersonalFile: async (file) => {
    await uploadFile(file, undefined, get().selectedScope.folderId);
    await get().fetchResources(true);
  },

  fetchFavorites: async (reset = false) => {
    const { searchKeyword, favPage, favorites } = get();
    const currentPage = reset ? 1 : favPage;
    set({ loading: true });
    try {
      const [result, automationItems] = await Promise.all([
        getFavoriteChats({ keyword: searchKeyword || undefined, page: currentPage, page_size: PAGE_SIZE }),
        loadAutomationFavorites(searchKeyword || undefined),
      ]);
      const existing = reset ? [] : favorites.filter((item) => !isAutomationFavoriteItem(item));
      set({
        favorites: dedupeAndSort([...automationItems, ...existing, ...(result.items || [])]),
        favTotal: result.total + automationItems.length,
        favPage: currentPage,
        favHasMore: result.has_more,
        loading: false,
      });
    } catch (error) {
      console.error('Failed to fetch favorites:', error);
      set({ loading: false });
    }
  },
  deleteResource: async (id) => {
    try {
      await deleteArtifact(id);
      set((state) => ({
        resources: state.resources.filter((resource) => resource.id !== id),
        total: Math.max(0, state.total - 1),
      }));
    } catch (error) {
      console.error('Failed to delete artifact:', error);
    }
  },
  unfavoriteChat: async (chatId) => {
    if (isAutomationFavoriteChatId(chatId)) {
      useAutomationChatStore.getState().setSidebarFavorite(
        chatId.slice(AUTOMATION_FAVORITE_CHAT_PREFIX.length),
        false,
      );
      return;
    }
    await updateSession(chatId, { favorite: false });
  },
  removeFavorite: (chatId) => set((state) => {
    const exists = state.favorites.some((item) => item.source_chat_id === chatId);
    return {
      favorites: state.favorites.filter((item) => item.source_chat_id !== chatId),
      favTotal: exists ? Math.max(0, state.favTotal - 1) : state.favTotal,
    };
  }),
  loadMore: async () => {
    const state = get();
    if (state.loading) return;
    if (state.tab === 'favorites' && state.favHasMore) {
      set({ favPage: state.favPage + 1 });
      await state.fetchFavorites();
    } else if (state.tab === 'assets' && state.hasMore) {
      set({ page: state.page + 1 });
      await state.fetchResources();
    }
  },

  fetchNotifications: async () => {
    set({ notifLoading: true });
    try {
      const notifications = await getAutomationNotifications();
      set({
        notifications,
        notifUnreadCount: notifications.filter((item) => !item.read).length,
        notifLoading: false,
      });
    } catch (error) {
      console.error('Failed to fetch notifications:', error);
      set({ notifLoading: false });
    }
  },
  markNotificationRead: async (id) => {
    await markNotificationsRead([id]);
    set((state) => {
      const notifications = state.notifications.map((item) => (
        item.id === id ? { ...item, read: true } : item
      ));
      return { notifications, notifUnreadCount: notifications.filter((item) => !item.read).length };
    });
  },
  markAllNotificationsRead: async () => {
    const ids = get().notifications.filter((item) => !item.read).map((item) => item.id);
    if (ids.length === 0) return;
    await markNotificationsRead(ids);
    set((state) => ({
      notifications: state.notifications.map((item) => ({ ...item, read: true })),
      notifUnreadCount: 0,
    }));
  },
  markSelectedNotificationsRead: async () => {
    const { notifSelectedIds, notifications } = get();
    const ids = notifications
      .filter((item) => notifSelectedIds.has(item.id) && !item.read)
      .map((item) => item.id);
    if (ids.length === 0) return;
    await markNotificationsRead(ids);
    const selected = new Set(ids);
    set((state) => {
      const updated = state.notifications.map((item) => (
        selected.has(item.id) ? { ...item, read: true } : item
      ));
      return {
        notifications: updated,
        notifUnreadCount: updated.filter((item) => !item.read).length,
        notifSelectedIds: new Set<string>(),
      };
    });
  },
  deleteNotification: async (id) => {
    await deleteNotifications([id]);
    set((state) => {
      const notifications = state.notifications.filter((item) => item.id !== id);
      const selected = new Set(state.notifSelectedIds);
      selected.delete(id);
      return {
        notifications,
        notifUnreadCount: notifications.filter((item) => !item.read).length,
        notifSelectedIds: selected,
      };
    });
  },
  deleteSelectedNotifications: async () => {
    const ids = Array.from(get().notifSelectedIds);
    if (ids.length === 0) return;
    await deleteNotifications(ids);
    const selected = new Set(ids);
    set((state) => {
      const notifications = state.notifications.filter((item) => !selected.has(item.id));
      return {
        notifications,
        notifUnreadCount: notifications.filter((item) => !item.read).length,
        notifSelectedIds: new Set<string>(),
      };
    });
  },
  toggleNotifSelected: (id) => set((state) => {
    const selected = new Set(state.notifSelectedIds);
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    return { notifSelectedIds: selected };
  }),
  toggleNotifSelectAll: () => set((state) => {
    const ids = state.notifications.map((item) => item.id);
    const allSelected = ids.length > 0 && ids.every((id) => state.notifSelectedIds.has(id));
    return { notifSelectedIds: allSelected ? new Set<string>() : new Set(ids) };
  }),
  clearNotifSelection: () => set({ notifSelectedIds: new Set<string>() }),
  setNotifUnreadCount: (notifUnreadCount) => set({ notifUnreadCount }),
}));
