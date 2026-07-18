import { useEffect, useMemo } from 'react';
import { usePageConfigStore } from '../stores/pageConfigStore';
import type { PageConfig } from '../utils/pageConfigDefaults';
import { t } from '../i18n';

const POLL_INTERVAL_MS = 15_000;
const BROADCAST_CHANNEL_NAME = 'jx_page_config';

/**
 * Read any field in the page config (dot-path).
 * e.g.: usePageConfig('branding.product_name') or usePageConfig('texts.btn_new_chat')
 *
 * T defaults to string; pass the generic explicitly to get a non-string value.
 *
 * String return values (DB value or fallback) are already uniformly translated via t() ——
 * callers just pass the raw Chinese text as fallback, don't wrap it in t() again.
 */
export function usePageConfig(path: string, fallback: string): string;
export function usePageConfig<T>(path: string, fallback: T): T;
export function usePageConfig<T = string>(path: string, fallback?: T): T {
  const config = usePageConfigStore((s) => s.config);
  const parts = useMemo(() => path.split('.'), [path]);
  let value: unknown = config;
  for (const p of parts) {
    if (value && typeof value === 'object' && p in (value as Record<string, unknown>)) {
      value = (value as Record<string, unknown>)[p];
    } else {
      return fallback as T;
    }
  }
  const resolved = (value as T) ?? (fallback as T);
  // Run the DB-configured Chinese text through the dictionary: default text can be translated in the English UI,
  // while admin-customized unknown text that isn't found is returned as-is (graceful fallback).
  if (typeof resolved === 'string') {
    return t(resolved) as unknown as T;
  }
  return resolved;
}

/**
 * Read a panel's title + subtitle, with fallback. One call replaces two usePageConfig calls.
 */
export function usePanelHeader(
  panel: string,
  fallback: { title: string; subtitle?: string },
): { title: string; subtitle: string } {
  const title = usePageConfig(`navigation.panel_titles.${panel}`, fallback.title);
  const subtitle = usePageConfig(`navigation.panel_subtitles.${panel}`, fallback.subtitle ?? '');
  return { title, subtitle };
}

/**
 * Get the full config object (use when a component needs multiple fields).
 */
export function usePageConfigAll(): PageConfig {
  return usePageConfigStore((s) => s.config);
}

/**
 * Get app config such as external sub-app URLs (enterprise profile, enterprise research...).
 */
export function useAppConfig() {
  return usePageConfigStore((s) => s.appConfig);
}

/**
 * Call once in the root component. Startup fetch + 15s polling + visibilitychange + BroadcastChannel cross-tab sync.
 */
export function usePageConfigPolling(): void {
  useEffect(() => {
    const store = usePageConfigStore.getState();

    // Initial fetch
    void store.fetchConfig();

    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const checkVersion = async () => {
      if (cancelled) return;
      const state = usePageConfigStore.getState();
      const latest = await state.fetchVersion();
      if (cancelled) return;
      if (latest && latest !== state.getVersionKey()) {
        await state.fetchConfig();
      }
    };

    timer = setInterval(checkVersion, POLL_INTERVAL_MS);

    const onVisibility = () => {
      if (document.visibilityState === 'visible') void checkVersion();
    };
    document.addEventListener('visibilitychange', onVisibility);

    // Cross-tab: broadcast on admin save, other tabs fetch immediately
    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(BROADCAST_CHANNEL_NAME);
      channel.onmessage = (ev) => {
        if (ev?.data?.type === 'updated') {
          void usePageConfigStore.getState().fetchConfig();
        }
      };
    } catch {
      channel = null;
    }

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
      if (channel) {
        channel.close();
      }
    };
  }, []);
}

/**
 * Call after an admin save to notify this tab + other tabs to sync immediately.
 */
export function broadcastPageConfigUpdate(): void {
  try {
    const channel = new BroadcastChannel(BROADCAST_CHANNEL_NAME);
    channel.postMessage({ type: 'updated' });
    channel.close();
  } catch {
    // no-op
  }
}
