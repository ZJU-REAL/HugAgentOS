import { useEffect, useState } from 'react';
import { API_BASE } from '../utils/adminApi';
import { BUILTIN_APPS, type AppItem } from '../stores/pageConfigStore';

/**
 * Loads the merged catalog of "built-in apps + backend external apps", shared by the
 * app-visibility-scope selectors across Config's permission panels (user management /
 * team management / role permissions). Fetches once on mount.
 */
export function useAppsCatalog(): AppItem[] {
  const [appsCatalog, setAppsCatalog] = useState<AppItem[]>([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/v1/content/docs`);
        const body = await res.json();
        const list = Array.isArray(body?.data?.app_config?.apps) ? body.data.app_config.apps : [];
        const builtinIds = new Set(BUILTIN_APPS.map((a) => a.id));
        const externals = (list as AppItem[]).filter((a) => !builtinIds.has(a.id));
        if (!cancelled) setAppsCatalog([...BUILTIN_APPS, ...externals]);
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, []);
  return appsCatalog;
}
