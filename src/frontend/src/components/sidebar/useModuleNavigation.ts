import { useAuthStore } from '../../stores/authStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useSidebarLayoutStore } from '../../stores/sidebarLayoutStore';
import { usePageConfig } from '../../hooks/usePageConfig';
import { DEFAULT_LAYOUT_NAVIGATION, type LayoutNavigation } from '../../utils/pageConfigDefaults';
import { moduleNavigation } from './moduleNavigation';

export function useModuleNavigation() {
  const user = useAuthStore(s => s.authUser);
  const localOnly = useDeploymentModeStore(s => s.provisionMode === 'local_only');
  const layout = useSidebarLayoutStore(s => s.layout);
  const config = usePageConfig<LayoutNavigation>(`navigation.layouts.${layout}`, DEFAULT_LAYOUT_NAVIGATION);
  return moduleNavigation(config, {
    capabilities: { lab_enabled: user?.lab_enabled !== false, sites_enabled: user?.sites_enabled !== false }, localOnly,
    automationAllowed: !Array.isArray(user?.allowed_apps) || user.allowed_apps.includes('automation'),
  });
}
