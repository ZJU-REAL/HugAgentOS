import { orderedNavigation, useNavigationPreferences } from './workbench/preferences';
import { useApplicationDirectory } from './workbench/catalog';
import { LAYOUT_ITEMS } from './components/sidebar/items';
import { usePageConfig } from './hooks/usePageConfig';
import { useAuthStore } from './stores/authStore';
import { useDeploymentModeStore } from './stores/deploymentModeStore';
import { DEFAULT_MENU_ITEMS, DEFAULT_SIDEBAR_ITEMS } from './utils/pageConfigDefaults';
import { buildNavigationModel } from './workbench/navigation';

export function builtinNavigation(config: { sidebar: string[]; menu: string[] },
  context: { labEnabled: boolean; localOnly: boolean }) {
  return buildNavigationModel({
    items: LAYOUT_ITEMS,
    primary: ['my_space', 'automation', 'ability_center'],
    sidebar: config.sidebar,
    menu: config.menu,
    visible: (key, item) => key !== 'settings'
      && (!item.requiresLab || context.labEnabled)
      && !(key === 'my_space' && context.localOnly),
  });
}

export function useApplicationNavigation() {
  const authLabEnabled = useAuthStore(s => s.authUser?.lab_enabled !== false);
  const localOnly = useDeploymentModeStore(s => s.provisionMode === 'local_only');
  const sidebar = usePageConfig<string[]>('navigation.sidebar_items', DEFAULT_SIDEBAR_ITEMS);
  const menu = usePageConfig<string[]>('navigation.menu_items', DEFAULT_MENU_ITEMS);
  const catalog = useApplicationDirectory();
  const preferences = useNavigationPreferences();
  const allowed = useAuthStore(s => s.authUser?.allowed_apps ?? null);
  const authoritative = catalog.items.length > 0 && !catalog.error;
  const hasBuiltin = (key: string) => catalog.items.some(i => i.applicationKey === 'builtin:' + key);
  const labEnabled = authLabEnabled && (!authoritative || hasBuiltin('lab'));
  const visible = (key: string) => (!LAYOUT_ITEMS[key]?.requiresLab || labEnabled)
    && !(key === 'my_space' && localOnly) && (key !== 'automation' || !Array.isArray(allowed) || allowed.includes(key))
    && (!authoritative || hasBuiltin(key));
  const builtin = (keys: string[]) => keys.filter(k => k.startsWith('builtin:')).map(k => k.slice(8));
  const forced = builtin(catalog.policy.forced);
  const configured = catalog.policy.revision > 0 ? builtin(catalog.policy.defaultPinned) : sidebar;
  const nav = builtinNavigation({
    sidebar: [...new Set([...forced, ...configured])].filter(visible), menu: (catalog.policy.revision > 0 ? [...new Set([...sidebar, ...menu])] : menu).filter(visible),
  }, {labEnabled, localOnly});
  nav.primary = (catalog.policy.revision > 0
    ? [...new Set([...forced, ...configured])]
    : nav.primary).filter(key => visible(key) && nav.sidebar.includes(key));
  nav.more = [...new Set([...nav.sidebar, ...nav.menu])].filter(k => !nav.primary.includes(k));
  const defaults = catalog.policy.revision > 0 ? catalog.policy.defaultPinned : sidebar.map(key => 'builtin:' + key);
  const all = orderedNavigation(defaults, catalog.policy.forced, preferences.value);
  const customized = preferences.value.pinned !== null;
  if (customized) {
    nav.sidebar = builtin(all).filter(key => visible(key) && !!nav.items[key]);
    nav.primary = [...nav.sidebar];
    nav.menu = [...new Set([...sidebar, ...menu])].filter(key => visible(key) && !!nav.items[key]);
    nav.more = [...new Set([...builtin([...sidebar.map(key => 'builtin:' + key), ...menu.map(key => 'builtin:' + key)]), ...nav.menu])]
      .filter(key => visible(key) && !!nav.items[key] && !nav.primary.includes(key));
  }
  const permittedKeys = new Set(catalog.items.map(item => item.applicationKey));
  const retain = (key: string) => key.startsWith('builtin:')
    ? visible(key.slice(8)) && !!nav.items[key.slice(8)] && (key !== 'builtin:my_space' || !localOnly)
    : permittedKeys.has(key);
  return { ...nav, defaults, workspaceMore: catalog.items.filter(item => !item.applicationKey.startsWith('builtin:') && !all.includes(item.applicationKey) && !item.legacy), pinned: all.filter(retain),
    railPinned: customized || catalog.policy.revision > 0 ? all.filter(retain)
      : [...nav.primary.map(key => 'builtin:' + key), ...all.filter(key => !key.startsWith('builtin:') && retain(key))] };

}
