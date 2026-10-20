import { BUILTIN_APPS } from './stores/pageConfigStore';
import { useApplicationDirectory, type CatalogItem } from './workbench/catalog';
import { applicationText } from './workbench/externalApi';
import {sourceLabel} from './workbench/sourceIdentity';
import { t } from './i18n';

export interface ApplicationEntry {
  key: string; title: string; description: string; group: string; source: string;
  icon?: CatalogItem['declaration']['icon']; builtinIcon?: string;
  item: CatalogItem;
}
export function useApplicationEntries() {
  const catalog = useApplicationDirectory();
  const values = catalog.items.filter(item => item.declaration.target.kind !== 'builtin'
    && item.declaration.navigation?.showInAppCenter !== false && (!item.legacy || item.enabled));
  const entries: ApplicationEntry[] = values.map(item => {
    const meta = item.applicationKey.startsWith('builtin:') ? BUILTIN_APPS.find(app => app.id === item.declaration.id) : undefined;
    const source = item.applicationKey.startsWith('builtin:') ? '' :
      item.applicationKey.startsWith('plugin:') ? t('插件应用') : item.legacy ? t('旧版外部应用') : t('外部应用');
    return {key:item.applicationKey,title:meta ? t(meta.name) : t(applicationText(item.declaration.title)),
      description:t(meta?.description || applicationText(item.declaration.description)),group:item.declaration.navigation?.group || '',
      source:[source,item.origin ? t(sourceLabel(item.origin)) : ''].filter(Boolean).join(' · '),icon:item.declaration.icon,builtinIcon:meta?.icon,item};
  });
  return {catalog,entries};
}
