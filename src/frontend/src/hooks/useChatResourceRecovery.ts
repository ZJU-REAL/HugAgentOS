import { useEffect } from 'react';
import { authFetch, chatTargetHeaders, getApiUrl, isHybridDual, LOCAL_TARGET_HEADER } from '../api';
import { useCanvasStore } from '../stores/canvasStore';
import { usePluginUiStore } from '../stores/pluginUiStore';
import { resourceBinding, type ResourceBinding } from '../plugin-ui/module/resource';

/** Recover only live, authorized resources; never replay a historical open tool. */
export function useChatResourceRecovery(chatId: string | null, userId: string | null) {
  const contributions = usePluginUiStore(state => state.items);
  const loaded = usePluginUiStore(state => state.loaded);
  useEffect(() => {
    if (!chatId || !userId) return;
    if (!loaded) { void usePluginUiStore.getState().fetchContributions(); return; }
    const controller = new AbortController();
    void (async () => {
      try {
        const path = getApiUrl() + '/v1/chats/' + encodeURIComponent(chatId) + '/plugin-resources';
        const targets = isHybridDual() ? ['cloud', 'local'] : [undefined];
        const recovered = await Promise.all(targets.map(async target => {
          try {
            const response = await authFetch(path, { signal: controller.signal,
              headers: target ? { [LOCAL_TARGET_HEADER]: target } : chatTargetHeaders(chatId) });
            if (!response.ok) return [];
            const body = await response.json() as { data?: { items?: ResourceBinding[] } };
            return body.data?.items ?? [];
          } catch { return []; }
        }));
        if (controller.signal.aborted) return;
        for (const resource of recovered.flat()) {
          const found = usePluginUiStore.getState().findModule(resource.slug, resource.module_id);
          if (!found || found.contribution.surface !== 'canvas' || resource.chat_id !== chatId) continue;
          const output = { resource };
          if (!resourceBinding(output, found.contribution)) continue;
          useCanvasStore.getState().openPluginView({ slug: resource.slug, canvasId: resource.module_id,
            chatId, resource, output, status: 'success' });
        }
      } catch { /* Resource recovery is optional; an unavailable runtime cannot be attached. */ }
    })();
    return () => controller.abort();
  }, [chatId, userId, loaded, contributions]);
}
