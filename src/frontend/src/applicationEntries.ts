import { useApplicationNavigation } from './applicationNavigation';
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
  const navigation = useApplicationNavigation();
  const catalog = useApplicationDirectory();
  const visibleBuiltin = new Set([...navigation.sidebar, ...navigation.menu, 'chat', 'settings', 'docs', 'plan_mode']);
  const values = catalog.items.filter(item => !item.applicationKey.startsWith('builtin:') || visibleBuiltin.has(item.declaration.id));
  // Existing navigation remains available while the authoritative catalog loads.
  if (!catalog.items.length && !catalog.error) {
    for (const id of [...new Set([...navigation.sidebar, ...navigation.menu])]) {
      values.push({applicationKey:'builtin:' + id, revision:1, enabled:true,
        declaration:{schemaVersion:'1.0.0',id,title:navigation.items[id].label,target:{kind:'builtin',componentId:navigation.items[id].targetPanel}},
        availability:{available:true,reason:null},navigation:{defaultPinned:false,forced:false}});
    }
  }
  const entries: ApplicationEntry[] = values.map(item => {
    const meta = item.applicationKey.startsWith('builtin:') ? navigation.items[item.declaration.id] : undefined;
    const source = item.applicationKey.startsWith('builtin:') ? '' :
      item.applicationKey.startsWith('plugin:') ? t('插件应用') : item.legacy ? t('旧版外部应用') : t('外部应用');
    return {key:item.applicationKey,title:(meta?.label || t(applicationText(item.declaration.title)))+(item.origin?' · '+t(sourceLabel(item.origin)):''),
      description:t(applicationText(item.declaration.description)),group:item.declaration.navigation?.group || '',
      source: item.applicationKey.startsWith('builtin:') ? source : source + ' · ' + item.applicationKey,icon:item.declaration.icon,builtinIcon:meta?.icon,item};
  });
  return {...navigation,catalog,entries};
}
