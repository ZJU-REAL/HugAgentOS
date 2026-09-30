import { create } from 'zustand';
import { t } from '../i18n';
import { navigateTo,pathForAutomationChat,pathForPanel } from '../routing/navigation';
import { userScopedKey,writeLocal } from '../storage';
import type { AutomationChatGroup,AutomationRun } from '../types';
import { parseServerTime } from '../utils/date';
import { useChatStore } from './chatStore';

const AUTOMATION_SIDEBAR_PREFS_KEY = 'hugagent_automation_sidebar_prefs_v1';

interface AutomationSidebarPref {
  pinned?: boolean;
  favorite?: boolean;
}

function loadSidebarPrefs(userId: string | null | undefined): Record<string, AutomationSidebarPref> {
  if (typeof window === 'undefined') return {};
  const key = userScopedKey(AUTOMATION_SIDEBAR_PREFS_KEY, userId);
  if (!key) return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed as Record<string, AutomationSidebarPref> : {};
  } catch {
    return {};
  }
}

function saveSidebarPrefs(userId: string | null | undefined, prefs: Record<string, AutomationSidebarPref>) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(AUTOMATION_SIDEBAR_PREFS_KEY, userId);
  if (!key) return;
  writeLocal(key, JSON.stringify(prefs));
}

interface AutomationChatState {
  /** Owner of the in-memory state. Null until hydrated by login. */
  currentUserId: string | null;
  /** Currently active automation chat group (null = normal chat mode).
   * The "last frame" render during the exit animation is backed by derived state inside the RunTimelinePanel component; the store keeps no snapshot. */
  activeGroup: AutomationChatGroup | null;
  /** Currently selected run ID within the group */
  selectedRunId: string | null;
  /** Local sidebar-only preferences for automation entries */
  sidebarPrefs: Record<string, AutomationSidebarPref>;

  enterAutomationChat: (
    taskId: string,
    taskName: string,
    runs: AutomationRun[],
    initialRunId?: string,
  ) => void;
  exitAutomationChat: () => void;
  selectRun: (runId: string) => void;
  setRuns: (runs: AutomationRun[]) => void;
  toggleSidebarPinned: (taskId: string) => void;
  toggleSidebarFavorite: (taskId: string) => void;
  setSidebarFavorite: (taskId: string, favorite: boolean) => void;
  /** Switch into the given user's context (idempotent). */
  hydrateForUser: (userId: string) => void;
  /** Drop in-memory state on logout; per-user keys remain on disk. */
  clearForLogout: () => void;
}

/** Seed a known run so history loading has a destination before metadata arrives. */
function prepareRunChat(taskId: string, taskName: string, run: AutomationRun) {
  const id = run.chat_id!;
  const chat = useChatStore.getState();
  chat.updateStore(prev => ({
    ...prev,
    chats: {
      ...prev.chats,
      [id]: prev.chats[id] || {
        id, title: taskName || t('定时任务'),
        createdAt: parseServerTime(run.started_at), updatedAt: parseServerTime(run.started_at),
        messages: [], automationRun: true, automationTaskId: taskId,
      },
    },
    order: prev.order.includes(id) ? prev.order : [id, ...prev.order],
  }));
  chat.addBackendSessionId(id);
  chat.adoptChatFromUrl(id);
}

export const useAutomationChatStore = create<AutomationChatState>((set, get) => ({
  currentUserId: null,
  activeGroup: null,
  selectedRunId: null,
  sidebarPrefs: {},

  enterAutomationChat: (taskId, taskName, runs, initialRunId) => {
    const completedRuns = runs.filter(run => run.status !== 'running' && run.chat_id);
    const target = completedRuns.find(run => run.run_id === initialRunId) || completedRuns[0];
    set({ activeGroup: { taskId, taskName, runs }, selectedRunId: target?.run_id || null });
    if (target?.chat_id) {
      prepareRunChat(taskId, taskName, target);
      navigateTo(pathForAutomationChat(taskId, target.chat_id));
    } else navigateTo(pathForPanel('automation', taskId));
  },

  exitAutomationChat: () => set({ activeGroup: null, selectedRunId: null }),

  selectRun: (runId) => {
    const group = get().activeGroup;
    const run = group?.runs.find(item => item.run_id === runId);
    if (!group || !run?.chat_id || run.status === 'running') return;
    set({ selectedRunId: runId });
    prepareRunChat(group.taskId, group.taskName, run);
    navigateTo(pathForAutomationChat(group.taskId, run.chat_id));
  },

  setRuns: (runs) => {
    const group = get().activeGroup;
    if (group) set({ activeGroup: { ...group, runs } });
  },

  toggleSidebarPinned: (taskId) => set((state) => {
    const prev = state.sidebarPrefs[taskId] || {};
    const nextPrefs = {
      ...state.sidebarPrefs,
      [taskId]: {
        ...prev,
        pinned: !prev.pinned,
      },
    };
    saveSidebarPrefs(get().currentUserId, nextPrefs);
    return { sidebarPrefs: nextPrefs };
  }),

  toggleSidebarFavorite: (taskId) => set((state) => {
    const prev = state.sidebarPrefs[taskId] || {};
    const nextPrefs = {
      ...state.sidebarPrefs,
      [taskId]: {
        ...prev,
        favorite: !prev.favorite,
      },
    };
    saveSidebarPrefs(get().currentUserId, nextPrefs);
    return { sidebarPrefs: nextPrefs };
  }),

  setSidebarFavorite: (taskId, favorite) => set((state) => {
    const prev = state.sidebarPrefs[taskId] || {};
    const nextPrefs = {
      ...state.sidebarPrefs,
      [taskId]: {
        ...prev,
        favorite,
      },
    };
    saveSidebarPrefs(get().currentUserId, nextPrefs);
    return { sidebarPrefs: nextPrefs };
  }),

  hydrateForUser: (userId) => {
    if (get().currentUserId === userId) return;
    set({
      currentUserId: userId,
      sidebarPrefs: loadSidebarPrefs(userId),
      // Reset transient run state so the previous user's selections don't
      // bleed through.
      activeGroup: null,
      selectedRunId: null,
        });
  },

  clearForLogout: () => set({
    currentUserId: null,
    activeGroup: null,
    selectedRunId: null,
      sidebarPrefs: {},
  }),
}));
