import type { PanelKey } from '../../types';
import { LAYOUT_ITEMS, type LayoutItemMeta } from './items';

const PRIMARY_MODULES = ['my_space', 'automation', 'ability_center'] as const;
const MORE_MODULES = ['projects', 'sites', 'app_center', 'lab'] as const;
export type ModuleId = typeof PRIMARY_MODULES[number] | typeof MORE_MODULES[number];
export interface ModuleEntry { id: ModuleId; meta: LayoutItemMeta }

/** Module placement belongs to the shell. Application catalogs and pins cannot change it. */
export function moduleNavigation(config: { sidebar: readonly string[]; menu: readonly string[] },
  context: { labEnabled: boolean; localOnly: boolean; automationAllowed: boolean }) {
  const configured = new Set([...config.sidebar, ...config.menu]);
  const visible = (id: ModuleId) => (!LAYOUT_ITEMS[id].requiresLab || context.labEnabled)
    && !(id === 'my_space' && context.localOnly)
    && !(id === 'automation' && !context.automationAllowed);
  const entries = (ids: readonly ModuleId[]) => ids.filter(visible).map(id => ({ id, meta: LAYOUT_ITEMS[id] }));
  const primary = entries(PRIMARY_MODULES);
  const more = entries(MORE_MODULES.filter(id => configured.has(id)));
  return { primary, more };
}

export function secondaryModule(panel: PanelKey, more: readonly ModuleEntry[]) {
  return more.some(entry => entry.meta.targetPanel === panel);
}
