import { create } from 'zustand';



export interface CanvasArtifact {
  file_id: string;
  name: string;
  url: string;          // relative path, e.g. /files/xxx
  mime_type?: string;
  size?: number;
  chat_id?: string;     // for "save as" to associate with a conversation
}

export interface OntologyPanelTarget {
  chatId: string;
  messageTs: number;
}

/**
 * A canvas tab contributed by a plugin.
 *
 * The store knows only "some plugin's canvas view, fed this tool output" — the
 * shape it renders comes from that plugin's `plugin.json`, so a new plugin can
 * add a canvas without the store learning anything about it.
 */
export interface PluginPanelTarget {
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

export type RightSidebarView = 'file' | 'ontology' | 'plugin' | 'empty';

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

export type CanvasTab = CanvasFileTab | CanvasOntologyTab | CanvasPluginTab;

const fileTabId = (fileId: string) => `file:${fileId}`;
const ontologyTabId = (chatId: string) => `ontology:${chatId}`;
const pluginTabId = (target: PluginPanelTarget) =>
  `plugin:${target.slug}:${target.canvasId}:${target.chatId || 'global'}:${target.toolId || 'latest'}`;

/** 活动页签投影出的视图字段：老调用方（App / chatStream / 各面板）继续读这些即可。 */
interface DerivedView {
  activeView: RightSidebarView;
  artifact: CanvasArtifact | null;
  ontologyTarget: OntologyPanelTarget | null;
  pluginTarget: PluginPanelTarget | null;
  openSeq: number;
}

function derive(tabs: CanvasTab[], activeTabId: string | null): DerivedView {
  const active = tabs.find((tab) => tab.id === activeTabId) || null;
  return {
    activeView: active ? active.kind : 'empty',
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

interface CanvasState extends DerivedView {
  isOpen: boolean;
  isFullscreen: boolean;
  /** 浏览器式页签：文件 / 本体校验 / 产业链图谱共用一条页签栏 */
  tabs: CanvasTab[];
  activeTabId: string | null;
  /** 拖拽出来的面板宽度，跨页签共享（切页签不该把宽度弹回默认值） */
  panelWidth: number | null;
  openCanvas: (artifact: CanvasArtifact) => void;
  openOntology: (target: OntologyPanelTarget) => void;
  openPluginView: (target: PluginPanelTarget) => void;
  updatePluginView: (patch: Partial<PluginPanelTarget>) => void;
  activateTab: (tabId: string) => void;
  closeTab: (tabId: string) => void;
  setTabDirty: (tabId: string, dirty: boolean) => void;
  setPanelWidth: (width: number | null) => void;
  openSidebar: () => void;
  closeCanvas: () => void;
  setCanvasFullscreen: (isFullscreen: boolean) => void;
  toggleCanvasFullscreen: () => void;
  resetSidebar: () => void;
  /** Update artifact metadata without re-triggering content reload */
  updateArtifact: (patch: Partial<CanvasArtifact>) => void;
}

export const useCanvasStore = create<CanvasState>((set) => ({
  isOpen: false,
  isFullscreen: false,
  tabs: [],
  activeTabId: null,
  panelWidth: null,
  ...derive([], null),
  openCanvas: (artifact) => set((state) => {
    const id = fileTabId(artifact.file_id);
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
    return { isOpen: true, tabs, activeTabId: id, ...derive(tabs, id) };
  }),
  openOntology: (target) => set((state) => {
    // 每个会话一个本体校验页签：同会话的新一轮校验刷新它，不再堆页签。
    const id = ontologyTabId(target.chatId);
    const index = state.tabs.findIndex((tab) => tab.id === id);
    const tab: CanvasOntologyTab = { id, kind: 'ontology', target };
    const tabs = index >= 0 ? replaceAt(state.tabs, index, tab) : [...state.tabs, tab];
    return { isOpen: true, tabs, activeTabId: id, ...derive(tabs, id) };
  }),
  openPluginView: (target) => set((state) => {
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
    return { isOpen: true, tabs, activeTabId: id, ...derive(tabs, id) };
  }),
  updatePluginView: (patch) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === state.activeTabId);
    const active = index >= 0 ? state.tabs[index] : null;
    if (!active || active.kind !== 'plugin') return {};
    const tabs = replaceAt(state.tabs, index, {
      ...active,
      target: { ...active.target, ...patch },
    });
    return { tabs, ...derive(tabs, state.activeTabId) };
  }),
  activateTab: (tabId) => set((state) => {
    if (!state.tabs.some((tab) => tab.id === tabId)) return {};
    // 切走会卸载上一个页签的内容，它的「未保存」标记也随之失效（回来时是重新加载的
    // 干净副本）—— 就地清掉，免得下次操作弹一次假的确认框。
    const tabs = clearDirty(state.tabs, state.activeTabId);
    return { isOpen: true, tabs, activeTabId: tabId, ...derive(tabs, tabId) };
  }),
  closeTab: (tabId) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === tabId);
    if (index < 0) return {};
    const tabs = state.tabs.filter((tab) => tab.id !== tabId);
    if (!tabs.length) {
      return { tabs, activeTabId: null, isOpen: false, isFullscreen: false, ...derive(tabs, null) };
    }
    // 关掉当前页签后接管右邻页签（没有右邻则取左邻），与浏览器一致。
    const activeTabId = state.activeTabId === tabId
      ? tabs[Math.min(index, tabs.length - 1)].id
      : state.activeTabId;
    return { tabs, activeTabId, ...derive(tabs, activeTabId) };
  }),
  setTabDirty: (tabId, dirty) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === tabId);
    const tab = index >= 0 ? state.tabs[index] : null;
    if (!tab || tab.kind !== 'file' || tab.dirty === dirty) return {};
    return { tabs: replaceAt(state.tabs, index, { ...tab, dirty }) };
  }),
  setPanelWidth: (panelWidth) => set({ panelWidth }),
  openSidebar: () => set({ isOpen: true }),
  // Keep the selected content while collapsed so the top-right toggle can
  // restore the same view. Chat/panel changes call resetSidebar explicitly.
  closeCanvas: () => set((state) => ({
    isOpen: false,
    isFullscreen: false,
    tabs: clearDirty(state.tabs, state.activeTabId),
  })),
  setCanvasFullscreen: (isFullscreen) => set({ isFullscreen }),
  toggleCanvasFullscreen: () => set((state) => ({
    isFullscreen: !state.isFullscreen,
  })),
  resetSidebar: () => set({
    isOpen: false,
    isFullscreen: false,
    tabs: [],
    activeTabId: null,
    ...derive([], null),
  }),
  updateArtifact: (patch) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === state.activeTabId);
    const active = index >= 0 ? state.tabs[index] : null;
    if (!active || active.kind !== 'file') return {};
    const tabs = replaceAt(state.tabs, index, {
      ...active,
      artifact: { ...active.artifact, ...patch },
    });
    return { tabs, ...derive(tabs, state.activeTabId) };
  }),
}));
