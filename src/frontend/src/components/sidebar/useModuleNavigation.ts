import { useAuthStore } from '../../stores/authStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { usePageConfig } from '../../hooks/usePageConfig';
import { DEFAULT_MENU_ITEMS, DEFAULT_SIDEBAR_ITEMS } from '../../utils/pageConfigDefaults';
import { moduleNavigation } from './moduleNavigation';

export function useModuleNavigation() {
  const user = useAuthStore(s => s.authUser);
  const localOnly = useDeploymentModeStore(s => s.provisionMode === 'local_only');
  const sidebar = usePageConfig<string[]>('navigation.sidebar_items', DEFAULT_SIDEBAR_ITEMS);
  const menu = usePageConfig<string[]>('navigation.menu_items', DEFAULT_MENU_ITEMS);
  return moduleNavigation({ sidebar, menu }, {
    labEnabled: user?.lab_enabled !== false, localOnly,
    automationAllowed: !Array.isArray(user?.allowed_apps) || user.allowed_apps.includes('automation'),
  });
}
