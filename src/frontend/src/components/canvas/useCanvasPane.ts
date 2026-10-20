import { useContext, useMemo } from 'react';
import { derive, useCanvasStore, type CanvasState } from '../../stores/canvasStore';
import { CanvasPaneContext } from './CanvasPaneContext';

export function useCanvasPaneId() { return useContext(CanvasPaneContext); }

/** Project the shared tabs into the pane that owns this mounted content. */
export function useCanvasPane<T = CanvasState>(selector?: (state: CanvasState) => T): T {
  const pane = useCanvasPaneId();
  const state = useCanvasStore();
  const tabId = state.paneTabs[pane];
  const actions = useMemo(() => ({
    activateTab: (id: string) => useCanvasStore.getState().activateTab(id, pane),
    openPluginView: (target: Parameters<CanvasState['openPluginView']>[0]) => {
      const store = useCanvasStore.getState();
      store.focusPane(pane);
      store.openPluginView(target);
    },
    updateArtifact: (patch: Parameters<CanvasState['updateArtifact']>[0]) =>
      useCanvasStore.getState().updateArtifact(patch, tabId),
    updatePluginView: (patch: Parameters<CanvasState['updatePluginView']>[0]) =>
      useCanvasStore.getState().updatePluginView(patch, tabId),
  }), [pane, tabId]);
  const projected = useMemo(() => ({
    ...state, ...derive(state.tabs, tabId), activeTabId: tabId, ...actions,
    tabs: state.tabs.filter(tab => state.tabPanes[tab.id] === pane),
  }), [state, tabId, actions, pane]);
  return selector ? selector(projected) : projected as T;
}
