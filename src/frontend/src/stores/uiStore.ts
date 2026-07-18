import type { ReactNode } from 'react';
import { create } from 'zustand';
import type { UpdateEntry, CapItem, UpdateCategory, FileConfirmInfo, DesignPickInfo } from '../types';
import type { SearchResultItem } from '../api';

export type HistoryTimeFilter = 'all' | 'today' | '7d' | '30d';
export type DocsSubTab = 'updates' | 'capabilities';
export type UpdateFilter = '全部' | UpdateCategory;

const DISPATCH_PROCESS_STORAGE_KEY = 'hugagent_dispatch_process_visible';

function loadDispatchProcessVisible(): boolean {
  if (typeof window === 'undefined') return false;
  const raw = window.localStorage.getItem(DISPATCH_PROCESS_STORAGE_KEY);
  return raw == null ? false : raw !== 'false';
}

interface UIState {
  siderCollapsed: boolean;

  // ── Search modal (replaces the old sidebar-embedded search) ──
  // The only search entry point: the search button / ⌘K triggers openSearchModal; closing resets it.
  searchModalOpen: boolean;
  searchKeyword: string;
  searchResults: SearchResultItem[];
  searchLoading: boolean;

  // The old "history section" filter dropdown has been removed; these two states are kept for SearchModal's internal use.
  historyTimeFilter: HistoryTimeFilter;
  historyTopicFilter: string;
  editingChatId: string | null;
  editingTitle: string;

  // ── Image preview ──
  previewImage: { url: string; name: string } | null;

  // ── Detail modal ──
  detailModal: { title: string; body: ReactNode } | null;

  // ── Recommend banner ──
  recommendBarVisible: boolean;

  // ── Docs panel ──
  activeDocsSubTab: DocsSubTab;
  activeUpdateFilter: UpdateFilter;
  featureUpdates: UpdateEntry[];
  capabilitiesList: CapItem[];

  // ── Prompt Hub ──
  promptHubOpen: boolean;
  dispatchProcessVisible: boolean;

  // ── §13 My Space write confirmation ──
  // Stores one **FIFO queue** per chatId: a single round of parallel tool calls can concurrently register N distinct
  // pending confirmations, which must all be queued and popped one by one — click one, the next appears — never overwritten
  // by later arrivals like the old single-slot model (overwriting would leave the un-popped N-1 tool coroutines stuck forever).
  pendingConfirm: Record<string, FileConfirmInfo[]>;

  // ── Site-building design three-way choice ──
  // Stores a **single value** per chatId (one site build pops only one picker; the backend already dedupes the same question).
  pendingDesignPick: Record<string, DesignPickInfo | undefined>;

  // ── Actions ──
  setSiderCollapsed: (v: boolean) => void;
  toggleSider: () => void;

  openSearchModal: () => void;
  closeSearchModal: () => void;
  setSearchKeyword: (keyword: string) => void;
  setSearchResults: (results: SearchResultItem[]) => void;
  setSearchLoading: (v: boolean) => void;

  setHistoryTimeFilter: (filter: HistoryTimeFilter) => void;
  setHistoryTopicFilter: (topic: string) => void;
  setEditingChatId: (id: string | null) => void;
  setEditingTitle: (title: string) => void;

  setRecommendBarVisible: (v: boolean) => void;

  setPreviewImage: (image: { url: string; name: string } | null) => void;
  setDetailModal: (modal: { title: string; body: ReactNode } | null) => void;

  setActiveDocsSubTab: (tab: DocsSubTab) => void;
  setActiveUpdateFilter: (filter: UpdateFilter) => void;
  setFeatureUpdates: (updates: UpdateEntry[]) => void;
  setCapabilitiesList: (items: CapItem[]) => void;

  setPromptHubOpen: (v: boolean) => void;
  setDispatchProcessVisible: (v: boolean) => void;

  // Enqueue an item (deduped by confirmId; ignored if already in the queue).
  enqueuePendingConfirm: (chatId: string, info: FileConfirmInfo) => void;
  // Dequeue an item (user has decided / the item timed out); delete the chat key when the queue is empty.
  resolvePendingConfirm: (chatId: string, confirmId: string) => void;
  // Clear the entire queue for a chat (new send / reset).
  clearPendingConfirm: (chatId: string) => void;
  // Replace a chat's entire queue with the backend's authoritative list (restore on refresh/switch-back, order-preserving).
  hydratePendingConfirmQueue: (chatId: string, infos: FileConfirmInfo[]) => void;
  // Sidebar blue dot: only ensures a chat with pending confirmations has a non-empty queue, without clobbering an existing fuller queue.
  hydratePendingConfirms: (list: Array<{ chatId: string; info: FileConfirmInfo }>) => void;

  // Site-building design three-way choice: set/clear the pending picker for the current chat (passing null clears and deletes the key).
  setPendingDesignPick: (chatId: string, info: DesignPickInfo | null) => void;
}

export const useUIStore = create<UIState>((set) => ({
  siderCollapsed: false,

  searchModalOpen: false,
  searchKeyword: '',
  searchResults: [],
  searchLoading: false,

  historyTimeFilter: 'all',
  historyTopicFilter: 'all',
  editingChatId: null,
  editingTitle: '',

  recommendBarVisible: true,

  previewImage: null,
  detailModal: null,

  activeDocsSubTab: 'updates',
  activeUpdateFilter: '全部',
  featureUpdates: [],
  capabilitiesList: [],

  promptHubOpen: false,
  dispatchProcessVisible: loadDispatchProcessVisible(),

  pendingConfirm: {},
  pendingDesignPick: {},

  setSiderCollapsed: (v) => set({ siderCollapsed: v }),
  toggleSider: () => set((s) => ({ siderCollapsed: !s.siderCollapsed })),

  openSearchModal: () => set({ searchModalOpen: true }),
  // When closing the modal, reset keyword/results/both filters so the next open starts clean.
  closeSearchModal: () => set({
    searchModalOpen: false,
    searchKeyword: '',
    searchResults: [],
    searchLoading: false,
    historyTimeFilter: 'all',
    historyTopicFilter: 'all',
  }),
  setSearchKeyword: (keyword) => set({ searchKeyword: keyword }),
  setSearchResults: (results) => set({ searchResults: results }),
  setSearchLoading: (v) => set({ searchLoading: v }),

  setHistoryTimeFilter: (filter) => set({ historyTimeFilter: filter }),
  setHistoryTopicFilter: (topic) => set({ historyTopicFilter: topic }),
  setEditingChatId: (id) => set({ editingChatId: id }),
  setEditingTitle: (title) => set({ editingTitle: title }),

  setRecommendBarVisible: (v) => set({ recommendBarVisible: v }),

  setPreviewImage: (image) => set({ previewImage: image }),
  setDetailModal: (modal) => set({ detailModal: modal }),

  setActiveDocsSubTab: (tab) => set({ activeDocsSubTab: tab }),
  setActiveUpdateFilter: (filter) => set({ activeUpdateFilter: filter }),
  setFeatureUpdates: (updates) => set({ featureUpdates: updates }),
  setCapabilitiesList: (items) => set({ capabilitiesList: items }),

  setPromptHubOpen: (v) => set({ promptHubOpen: v }),
  enqueuePendingConfirm: (chatId, info) =>
    set((s) => {
      if (!chatId || !info?.confirmId) return s;
      const q = s.pendingConfirm[chatId] ?? [];
      // Dedupe: the same confirm can be delivered repeatedly from multiple sources (SSE events / chat-switch polling /
      // hydrate). If it's already in the queue, leave it untouched to avoid needless re-renders and duplicate items.
      if (q.some((x) => x.confirmId === info.confirmId)) return s;
      return { pendingConfirm: { ...s.pendingConfirm, [chatId]: [...q, info] } };
    }),
  resolvePendingConfirm: (chatId, confirmId) =>
    set((s) => {
      const q = s.pendingConfirm[chatId];
      if (!q || !q.some((x) => x.confirmId === confirmId)) return s;
      const rest = q.filter((x) => x.confirmId !== confirmId);
      const next = { ...s.pendingConfirm };
      // An empty queue must delete the key: places like Sidebar use `!!pendingConfirm[id]`, and an empty array is
      // truthy, so keeping it would wrongly light up the blue dot.
      if (rest.length) next[chatId] = rest;
      else delete next[chatId];
      return { pendingConfirm: next };
    }),
  clearPendingConfirm: (chatId) =>
    set((s) => {
      if (!s.pendingConfirm[chatId]) return s;
      const next = { ...s.pendingConfirm };
      delete next[chatId];
      return { pendingConfirm: next };
    }),
  hydratePendingConfirmQueue: (chatId, infos) =>
    set((s) => {
      const cur = s.pendingConfirm[chatId];
      const clean = (infos || []).filter((x) => x?.confirmId);
      // The backend is authoritative: order-preserving full replacement. Skip when references are equal (avoids re-rendering on every chat-switch refresh).
      if (
        cur && cur.length === clean.length &&
        cur.every((x, i) => x.confirmId === clean[i].confirmId)
      ) return s;
      const next = { ...s.pendingConfirm };
      if (clean.length) next[chatId] = clean;
      else delete next[chatId];
      return { pendingConfirm: next };
    }),
  hydratePendingConfirms: (list) =>
    set((s) => {
      const next = { ...s.pendingConfirm };
      let changed = false;
      for (const { chatId, info } of list) {
        // Only light up the blue dot: leave an existing (fuller) queue untouched; only insert a placeholder when empty.
        if (chatId && info?.confirmId && !(next[chatId]?.length)) {
          next[chatId] = [info];
          changed = true;
        }
      }
      return changed ? { pendingConfirm: next } : s;
    }),
  setPendingDesignPick: (chatId, info) =>
    set((s) => {
      if (!chatId) return s;
      const valid = !!(info && info.confirmId);
      // Delete the key on empty value (DesignPickerCard/Sidebar rely on key existence); clearing when already empty is a no-op.
      if (!valid && !s.pendingDesignPick[chatId]) return s;
      const next = { ...s.pendingDesignPick };
      if (valid) next[chatId] = info as DesignPickInfo;
      else delete next[chatId];
      return { pendingDesignPick: next };
    }),
  setDispatchProcessVisible: (v) => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(DISPATCH_PROCESS_STORAGE_KEY, String(v));
    }
    set({ dispatchProcessVisible: v });
  },
}));
