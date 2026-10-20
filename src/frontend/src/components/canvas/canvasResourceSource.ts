/** Delayed module work stays attached to the resource and tab that owns it. */
import { useCanvasStore, type PluginPanelTarget } from '../../stores/canvasStore';
import type { ResourceBinding } from '../../plugin-ui/module/resource';

export function bindCanvasResourceSource(resource: ResourceBinding, tabId?: string | null) {
  useCanvasStore.getState().updatePluginView({
    resource, slug: resource.slug, canvasId: resource.module_id,
  }, tabId);
}

export function runForCurrentCanvas(
  tabId: string | null, target: PluginPanelTarget, action: () => void,
) {
  const state = useCanvasStore.getState();
  const tab = state.tabs.find(tab => tab.id === tabId);
  if (Object.values(state.paneTabs).includes(tabId) && tab?.kind === 'plugin' && tab.target === target) action();
}
