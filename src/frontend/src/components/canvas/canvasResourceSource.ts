/** Delayed module work stays attached to the resource and tab that owns it. */
import { useCanvasStore, type PluginPanelTarget } from '../../stores/canvasStore';
import type { ResourceBinding } from '../../plugin-ui/module/resource';

export function bindCanvasResourceSource(resource: ResourceBinding) {
  useCanvasStore.getState().updatePluginView({
    resource, slug: resource.slug, canvasId: resource.module_id,
  });
}

export function runForCurrentCanvas(
  tabId: string | null, target: PluginPanelTarget, action: () => void,
) {
  const state = useCanvasStore.getState();
  if (state.activeTabId === tabId && state.pluginTarget === target) action();
}
