import type { PanelKey } from '../../types';
import type { LayoutNavigation, ModuleKey } from '../../utils/pageConfigDefaults';
import { normalizeLayoutNavigation } from '../../utils/pageConfigDefaults';
import { LAYOUT_ITEMS, type LayoutItemMeta, type ModuleCapability } from './items';

export interface ModuleEntry { id: ModuleKey; meta: LayoutItemMeta }

/** Module placement comes from the page config of the active sidebar layout. Only shell modules
 *  can be placed: application catalogs, plugins and pins never reach this model. */
export function moduleNavigation(config: Partial<LayoutNavigation> | null | undefined,
  context: { capabilities: Record<ModuleCapability, boolean>; localOnly: boolean; automationAllowed: boolean }) {
  const layout = normalizeLayoutNavigation(config);
  const visible = (id: ModuleKey) => {
    const capability = LAYOUT_ITEMS[id].requiresCapability;
    return (!capability || context.capabilities[capability])
      && !(id === 'my_space' && context.localOnly)
      && !(id === 'automation' && !context.automationAllowed);
  };
  const entries = (ids: readonly ModuleKey[]) => ids.filter(visible).map(id => ({ id, meta: LAYOUT_ITEMS[id] }));
  return { primary: entries(layout.sidebar_items), more: entries(layout.more_items) };
}

export function secondaryModule(panel: PanelKey, more: readonly ModuleEntry[]) {
  return more.some(entry => entry.meta.targetPanel === panel);
}
