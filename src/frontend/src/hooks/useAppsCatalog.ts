import { useEffect, useState } from 'react';
import { CONFIG_STORAGE_KEY, configFetch } from '../utils/adminApi';
import { BUILTIN_APPS, type AppItem } from '../stores/pageConfigStore';
import { applicationText } from '../workbench/externalApi';
import type { CatalogItem } from '../workbench/catalog';

/** Permission selectors use the protected administrative catalog, not the user's projection.
 * Existing permission-only entries such as prompt_hub retain their original IDs and behavior.
 */
export function useAppsCatalog(): AppItem[] {
  const [appsCatalog, setAppsCatalog] = useState<AppItem[]>(BUILTIN_APPS);
  useEffect(() => {
    const controller = new AbortController();
    configFetch(localStorage.getItem(CONFIG_STORAGE_KEY) || '', '/v1/workbench/admin/catalog', {
      signal: controller.signal,
    }).then(body => {
      const builtinIds = new Set(BUILTIN_APPS.map(a => a.id));
      const ids = new Set(builtinIds);
      const externals = (body.data.items as CatalogItem[]).filter(item => {
        if (!['external:', 'plugin:'].some(prefix => item.applicationKey.startsWith(prefix)) && !item.legacy) return false;
        const id = item.applicationKey.startsWith('plugin:') ? item.applicationKey : item.declaration.id;
        if (ids.has(id)) return false;
        ids.add(id); return true;
      }).map(item => ({
        id:item.applicationKey.startsWith('plugin:') ? item.applicationKey : item.declaration.id, enabled:item.enabled, name:applicationText(item.declaration.title),
        description:applicationText(item.declaration.description), icon:'',
        url:item.declaration.target.kind === 'external' ? item.declaration.target.url : '',
      }));
      if (!controller.signal.aborted) setAppsCatalog([...BUILTIN_APPS, ...externals]);
    }).catch(() => { /* Preserve known builtins when the administrator request fails. */ });
    return () => controller.abort();
  }, []);
  return appsCatalog;
}
