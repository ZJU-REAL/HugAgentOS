import { usePluginUiStore } from './pluginUiStore';
import { resourceBinding, type ResourceBinding } from '../plugin-ui/module/resource';
import { create } from 'zustand';
import { artifactOrigin, type ArtifactLocation } from '../utils/artifactAccess';
import { loadJsonPref, removeLocal, saveJsonPref } from '../storage';

const PANEL_WIDTH_KEY = 'jx_canvas_panel_width';
const PANEL_MIN_WIDTH = 400;
/** Room the chat column keeps beside the panel. */
const PANEL_CHAT_RESERVE = 420;

/** Keep a dragged or restored panel width within what the layout can show. */
export function clampPanelWidth(width: number, available: number): number {
  return Math.round(Math.max(PANEL_MIN_WIDTH, Math.min(width, available - PANEL_CHAT_RESERVE)));
}

// Drag-resize calls setPanelWidth every frame; write once it settles.
let persistTimer: ReturnType<typeof setTimeout> | undefined;
let pendingWrite: (() => void) | undefined;
function persistPanelWidth(width: number | null) {
  clearTimeout(persistTimer);
  pendingWrite = () => {
    pendingWrite = undefined;
    if (width === null) removeLocal(PANEL_WIDTH_KEY);
    else saveJsonPref(PANEL_WIDTH_KEY, { width });
  };
  persistTimer = setTimeout(pendingWrite, 200);
}
// A reload right after a drag must not lose the debounced write.
if (typeof window !== 'undefined') window.addEventListener('pagehide', () => pendingWrite?.());

function loadPanelWidth(): number | null {
  const { width } = loadJsonPref<{ width?: unknown }>(PANEL_WIDTH_KEY, {});
  if (typeof width !== 'number' || !Number.isFinite(width)) return null;
  return clampPanelWidth(width, typeof window === 'undefined' ? Infinity : window.innerWidth);
}



export interface CanvasArtifact extends ArtifactLocation {
  space?: { id: string; path: string; writable: boolean; name?: string; kind?: 'personal' | 'team' };
  file_id: string;
  name: string;
  url: string;          // relative path, e.g. /files/xxx
  mime_type?: string;
  size?: number;
  chat_id?: string;     // for "save as" to associate with a conversation
}

export interface OntologyPanelTarget {
  chatId: string;
  messageUid: string;
}

/**
 * A canvas tab contributed by a plugin.
 *
 * The store knows only "some plugin's canvas view, fed this tool output" — the
 * shape it renders comes from that plugin's `plugin.json`, so a new plugin can
 * add a canvas without the store learning anything about it.
 */
export interface PluginPanelTarget {
  resource?: ResourceBinding;
  chatId?: string;
  /** Plugin that contributed the canvas view. */
  slug: string;
  /** `canvas_views[].id` inside that plugin's declaration. */
  canvasId: string;
  toolId?: string;
  /** Tool that produced this payload; gates tool-scoped node actions. */
  toolName?: string;
  /** Tab label; falls back to the contributed canvas title. */
  title?: string;
  status: 'loading' | 'success' | 'error';
  output?: unknown;
  error?: string;
}

export interface SubagentPanelTarget {
  chatId: string;
  messageUid: string;
  toolId: string;
  agent: { agent_id: string; name: string; avatar: string | null };
}

export interface CanvasSubagentTab {
  id: string;
  kind: 'subagent';
  target: SubagentPanelTarget;
}

export type RightSidebarView = 'file' | 'ontology' | 'plugin' | 'subagent' | 'custom' | 'empty';

/** A view registered by a feature module (see components/canvas/customCanvasRegistry.ts). */
export interface CustomPanelTarget {
  renderer: string;
  title: string;
}

export interface CanvasCustomTab {
  id: string;
  kind: 'custom';
  target: CustomPanelTarget;
}

/** 一个文件预览页签。openSeq 逐页签自增：同一文件重新打开 / 原地保存后用它做缓存击穿。 */
export interface CanvasFileTab {
  id: string;
  kind: 'file';
  artifact: CanvasArtifact;
  openSeq: number;
  /** 面板内有未保存的编辑（xlsx）；切走/关闭前需要确认 */
  dirty: boolean;
}

export interface CanvasOntologyTab {
  id: string;
  kind: 'ontology';
  target: OntologyPanelTarget;
}

export interface CanvasPluginTab {
  id: string;
  kind: 'plugin';
  target: PluginPanelTarget;
}

export type CanvasTab = CanvasFileTab | CanvasOntologyTab | CanvasPluginTab | CanvasSubagentTab | CanvasCustomTab;

const fileTabId = (file: CanvasArtifact) => `file:${file.origin || 'cloud'}:${file.file_id}`;
const ontologyTabId = (chatId: string) => `ontology:${chatId}`;
const pluginTabId = (target: PluginPanelTarget) =>
  target.resource
    ? `plugin:${target.resource.execution_scope}:${target.resource.install_id}:${target.resource.resource_id}`
    : `plugin:${target.slug}:${target.canvasId}:${target.chatId || 'global'}:${target.toolId || 'latest'}`;

/** 活动页签投影出的视图字段：老调用方（App / chatStream / 各面板）继续读这些即可。 */
export interface DerivedView {
  activeView: RightSidebarView;
  artifact: CanvasArtifact | null;
  ontologyTarget: OntologyPanelTarget | null;
  pluginTarget: PluginPanelTarget | null;
  subagentTarget: SubagentPanelTarget | null;
  customTarget: CustomPanelTarget | null;
  openSeq: number;
}

export function derive(tabs: CanvasTab[], activeTabId: string | null): DerivedView {
  const active = tabs.find((tab) => tab.id === activeTabId) || null;
  return {
    activeView: active ? active.kind : 'empty',
    subagentTarget: active?.kind === 'subagent' ? active.target : null,
    customTarget: active?.kind === 'custom' ? active.target : null,
    artifact: active && active.kind === 'file' ? active.artifact : null,
    ontologyTarget: active && active.kind === 'ontology' ? active.target : null,
    pluginTarget: active && active.kind === 'plugin' ? active.target : null,
    openSeq: active && active.kind === 'file' ? active.openSeq : 0,
  };
}

function replaceAt(tabs: CanvasTab[], index: number, tab: CanvasTab): CanvasTab[] {
  return tabs.map((item, i) => (i === index ? tab : item));
}

/** 页签内容被卸载后「未保存」标记就不再成立（重新打开是干净副本）。 */
function clearDirty(tabs: CanvasTab[], tabId: string | null): CanvasTab[] {
  if (!tabId) return tabs;
  return tabs.map((tab) => (
    tab.id === tabId && tab.kind === 'file' && tab.dirty ? { ...tab, dirty: false } : tab
  ));
}

export type CanvasPaneId = 'primary' | 'secondary';

export interface CanvasState extends DerivedView {
  isSplit: boolean;
  activePane: CanvasPaneId;
  paneTabs: Record<CanvasPaneId, string | null>;
  tabPanes: Record<string, CanvasPaneId>;
  focusPane: (pane: CanvasPaneId) => void;
  toggleSplit: () => void;
  isOpen: boolean;
  isFullscreen: boolean;
  /** 浏览器式页签：文件 / 本体校验 / 产业链图谱共用一条页签栏 */
  tabs: CanvasTab[];
  activeTabId: string | null;
  /** 拖拽出来的面板宽度，跨页签共享（切页签不该把宽度弹回默认值） */
  panelWidth: number | null;
  openCanvas: (artifact: CanvasArtifact) => void;
  openOntology: (target: OntologyPanelTarget) => void;
  openSubagent: (target: SubagentPanelTarget) => void;
  /** One tab per renderer: reopening activates it and updates its title. */
  openCustom: (target: CustomPanelTarget) => void;
  openPluginView: (target: PluginPanelTarget) => void;
  updatePluginView: (patch: Partial<PluginPanelTarget>, tabId?: string | null) => void;
  activateTab: (tabId: string, pane?: CanvasPaneId) => void;
  closeTab: (tabId: string) => void;
  reloadTab: (tabId: string) => void;
  setTabDirty: (tabId: string, dirty: boolean) => void;
  setPanelWidth: (width: number | null) => void;
  openSidebar: () => void;
  closeCanvas: () => void;
  setCanvasFullscreen: (isFullscreen: boolean) => void;
  toggleCanvasFullscreen: () => void;
  resetSidebar: () => void;
  /** Update artifact metadata without re-triggering content reload */
  updateArtifact: (patch: Partial<CanvasArtifact>, tabId?: string | null) => void;
}

/** One mounted owner per tab. Reopening a visible tab focuses its existing pane. */
function selectTab(state: CanvasState, tabs: CanvasTab[], id: string, pane = state.activePane) {
  const owner = state.tabPanes[id];
  const activePane = owner || pane;
  const oldId = state.paneTabs[activePane];
  const nextTabs = oldId !== id ? clearDirty(tabs, oldId) : tabs;
  return {
    isOpen: true, tabs: nextTabs, activeTabId: id, activePane,
    paneTabs: { ...state.paneTabs, [activePane]: id },
    tabPanes: { ...state.tabPanes, [id]: activePane },
    ...derive(nextTabs, id),
  };
}

export const useCanvasStore = create<CanvasState>((set) => ({
  isOpen: false,
  isFullscreen: false,
  isSplit: false,
  activePane: 'primary',
  paneTabs: { primary: null, secondary: null },
  tabPanes: {},
  tabs: [],
  activeTabId: null,
  panelWidth: loadPanelWidth(),
  ...derive([], null),
  openCanvas: (artifact) => set((state) => {
    artifact = { ...artifact, origin: artifactOrigin(artifact) };
    const id = fileTabId(artifact);
    const index = state.tabs.findIndex((tab) => tab.id === id);
    const previous = index >= 0 ? state.tabs[index] : null;
    // 同一文件重开 = 复用原页签并 +1 openSeq（刷新预览），而不是叠一个重复页签。
    const tab: CanvasFileTab = {
      id,
      kind: 'file',
      artifact,
      openSeq: (previous && previous.kind === 'file' ? previous.openSeq : 0) + 1,
      dirty: false,
    };
    const tabs = index >= 0 ? replaceAt(state.tabs, index, tab) : [...state.tabs, tab];
    return selectTab(state, tabs, id);
  }),
  openCustom: (target) => set((state) => {
    const id = `custom:${target.renderer}`;
    const index = state.tabs.findIndex((tab) => tab.id === id);
    const tab: CanvasCustomTab = { id, kind: 'custom', target };
    const tabs = index >= 0 ? replaceAt(state.tabs, index, tab) : [...state.tabs, tab];
    return selectTab(state, tabs, id);
  }),
  openSubagent: (target) => set((state) => {
    const id = `subagent:${JSON.stringify([target.chatId, target.messageUid, target.toolId])}`;
    const index = state.tabs.findIndex((tab) => tab.id === id);
    const tab: CanvasSubagentTab = { id, kind: 'subagent', target };
    const tabs = index >= 0 ? replaceAt(state.tabs, index, tab) : [...state.tabs, tab];
    return selectTab(state, tabs, id);
  }),
  openOntology: (target) => set((state) => {
    // 每个会话一个本体校验页签：同会话的新一轮校验刷新它，不再堆页签。
    const id = ontologyTabId(target.chatId);
    const index = state.tabs.findIndex((tab) => tab.id === id);
    const tab: CanvasOntologyTab = { id, kind: 'ontology', target };
    const tabs = index >= 0 ? replaceAt(state.tabs, index, tab) : [...state.tabs, tab];
    return selectTab(state, tabs, id);
  }),
  openPluginView: (input) => set((state) => {
    const module = usePluginUiStore.getState().findModule(input.slug, input.canvasId)?.contribution;
    const resource = resourceBinding(input.output, module);
    const target = resource ? { ...input, resource } : input;
    const id = pluginTabId(target);
    const exact = state.tabs.findIndex((tab) => tab.id === id);
    // 一个画布在 tool_call 时以 loading 建页签、tool_result 时补数据，两次事件带的
    // tool id 可能被规整得不一致；此时同会话里同一个画布仍在 loading 的页签就是同
    // 一次运行，认领它，避免叠出一个永远停在「生成中」的僵尸页签。
    const index = exact >= 0
      ? exact
      : state.tabs.findIndex((tab) => tab.kind === 'plugin'
        && tab.target.slug === target.slug
        && tab.target.canvasId === target.canvasId
        && tab.target.chatId === target.chatId
        && tab.target.status === 'loading');
    const previous = index >= 0 ? state.tabs[index] : null;
    const tab: CanvasPluginTab = {
      id,
      kind: 'plugin',
      target: previous && previous.kind === 'plugin'
        ? { ...previous.target, ...target }
        : target,
    };
    const tabs = index >= 0 ? replaceAt(state.tabs, index, tab) : [...state.tabs, tab];
    if (previous && previous.id !== id) {
      const tabPanes = { ...state.tabPanes, [id]: state.tabPanes[previous.id] || state.activePane };
      delete tabPanes[previous.id];
      const paneTabs = {
        primary: state.paneTabs.primary === previous.id ? id : state.paneTabs.primary,
        secondary: state.paneTabs.secondary === previous.id ? id : state.paneTabs.secondary,
      };
      return selectTab({ ...state, tabPanes, paneTabs }, tabs, id);
    }
    return selectTab(state, tabs, id);
  }),
  updatePluginView: (patch, tabId) => set((state) => {
    const index = state.tabs.findIndex((tab) => tabId ? tab.id === tabId : patch.resource
      ? tab.kind === 'plugin' && tab.target.resource?.resource_id === patch.resource.resource_id
      : tab.id === (tabId ?? state.activeTabId));
    const active = index >= 0 ? state.tabs[index] : null;
    if (!active || active.kind !== 'plugin') return {};
    const combined = { ...active.target, ...patch };
    const module = usePluginUiStore.getState().findModule(combined.slug, combined.canvasId)?.contribution;
    const resource = resourceBinding(combined.output, module);
    const target = resource ? { ...combined, resource } : combined;
    const id = pluginTabId(target);
    const tabs = replaceAt(state.tabs, index, { ...active, id, target });
    const activeTabId = state.activeTabId === active.id ? id : state.activeTabId;
    const paneTabs = {
      primary: state.paneTabs.primary === active.id ? id : state.paneTabs.primary,
      secondary: state.paneTabs.secondary === active.id ? id : state.paneTabs.secondary,
    };
    const tabPanes = { ...state.tabPanes, [id]: state.tabPanes[active.id] || state.activePane };
    if (active.id !== id) delete tabPanes[active.id];
    return { tabs, activeTabId, paneTabs, tabPanes, ...derive(tabs, activeTabId) };
  }),
  focusPane: (pane) => set(state => {
    if (pane === state.activePane || (pane === 'secondary' && !state.isSplit)) return {};
    const activeTabId = state.paneTabs[pane];
    return { activePane: pane, activeTabId, ...derive(state.tabs, activeTabId) };
  }),
  toggleSplit: () => set(state => {
    if (!state.isSplit) {
      const secondary = state.tabs.find(tab => tab.id !== state.activeTabId)?.id || null;
      return { isSplit: true, paneTabs: { primary: state.activeTabId, secondary },
        tabPanes: { ...state.tabPanes, ...(secondary ? { [secondary]: 'secondary' as const } : {}) } };
    }
    const activeTabId = state.activeTabId || state.paneTabs.primary || state.paneTabs.secondary;
    const tabs = state.tabs.map(tab => tab.kind === 'file' && tab.id !== activeTabId
      ? { ...tab, dirty: false } : tab);
    return { isSplit: false, activePane: 'primary', activeTabId,
      paneTabs: { primary: activeTabId, secondary: null },
      tabPanes: Object.fromEntries(tabs.map(tab => [tab.id, 'primary' as const])), tabs, ...derive(tabs, activeTabId) };
  }),
  activateTab: (tabId, pane) => set(state => {
    if (!state.tabs.some(tab => tab.id === tabId)) return {};
    return selectTab(state, state.tabs, tabId, pane);
  }),
  closeTab: (tabId) => set(state => {
    const index = state.tabs.findIndex(tab => tab.id === tabId);
    if (index < 0) return {};
    const tabs = state.tabs.filter(tab => tab.id !== tabId);
    const paneTabs = { ...state.paneTabs };
    for (const pane of ['primary', 'secondary'] as const) {
      if (paneTabs[pane] !== tabId) continue;
      const ownerIndex = state.tabs.filter(tab => state.tabPanes[tab.id] === pane).findIndex(tab => tab.id === tabId);
      const available = tabs.filter(tab => state.tabPanes[tab.id] === pane);
      paneTabs[pane] = available[Math.min(ownerIndex, available.length - 1)]?.id || null;
    }
    const activeTabId = paneTabs[state.activePane];
    const tabPanes = { ...state.tabPanes };
    delete tabPanes[tabId];
    return {
      tabs, paneTabs, tabPanes, activeTabId, ...derive(tabs, activeTabId),
      ...(!tabs.length ? { isOpen: false, isFullscreen: false, isSplit: false,
        activePane: 'primary' as const, paneTabs: { primary: null, secondary: null } } : {}),
    };
  }),
  reloadTab: (tabId) => set(state => {
    const tabs = state.tabs.map(tab => tab.id === tabId && tab.kind === 'file'
      ? { ...tab, openSeq: tab.openSeq + 1, dirty: false } : tab);
    return { tabs, ...derive(tabs, state.activeTabId) };
  }),
  setTabDirty: (tabId, dirty) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === tabId);
    const tab = index >= 0 ? state.tabs[index] : null;
    if (!tab || tab.kind !== 'file' || tab.dirty === dirty) return {};
    return { tabs: replaceAt(state.tabs, index, { ...tab, dirty }) };
  }),
  setPanelWidth: (panelWidth) => {
    persistPanelWidth(panelWidth);
    set({ panelWidth });
  },
  openSidebar: () => set({ isOpen: true }),
  // Keep the selected content while collapsed so the top-right toggle can
  // restore the same view. Chat/panel changes call resetSidebar explicitly.
  closeCanvas: () => set((state) => ({
    isOpen: false,
    isFullscreen: false,
    tabs: clearDirty(clearDirty(state.tabs, state.paneTabs.primary), state.paneTabs.secondary),
  })),
  setCanvasFullscreen: (isFullscreen) => set({ isFullscreen }),
  toggleCanvasFullscreen: () => set((state) => ({
    isFullscreen: !state.isFullscreen,
  })),
  resetSidebar: () => set({
    isSplit: false, activePane: 'primary', paneTabs: { primary: null, secondary: null }, tabPanes: {},
    isOpen: false,
    isFullscreen: false,
    tabs: [],
    activeTabId: null,
    ...derive([], null),
  }),
  updateArtifact: (patch, tabId) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === (tabId ?? state.activeTabId));
    const active = index >= 0 ? state.tabs[index] : null;
    if (!active || active.kind !== 'file') return {};
    const tabs = replaceAt(state.tabs, index, {
      ...active,
      artifact: { ...active.artifact, ...patch },
    });
    return { tabs, ...derive(tabs, state.activeTabId) };
  }),
}));
