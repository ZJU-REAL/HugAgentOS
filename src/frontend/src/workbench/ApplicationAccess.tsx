import type { ReactNode } from 'react';
import { usePersonalAgentAvailable } from '../personalAgentEdition';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { reloadDirectory, useApplicationDirectory } from './catalog';
import { t } from '../i18n';
import { HostButton, HostNotice } from './ui';

/** URL navigation is not authorization. Actual backend resource gates remain authoritative. */
export function ApplicationAccess({id, children}: {id: string; children: ReactNode}) {
  const catalog = useApplicationDirectory();
  const personalAgentAvailable = usePersonalAgentAvailable();
  const auth = useAuthStore(s => s.authUser);
  const localOnly = useDeploymentModeStore(s => s.provisionMode === 'local_only');
  // This existing module has its own server-derived capability, not allowed_apps.
  // It retains its original ChatSidebar entry and is not a new catalog/rail icon.
  if (id === 'personal_agent') {
    return personalAgentAvailable ? children : <Denied />;
  }
  const item = catalog.items.find(i => i.applicationKey === 'builtin:' + id);
  const authoritative = catalog.items.length > 0 && !catalog.error;
  const denied = ((id === 'automation' && Array.isArray(auth?.allowed_apps) && !auth.allowed_apps.includes(id))
    || (['lab', 'sites'].includes(id) && auth?.lab_enabled === false)
    );
  if (denied || (authoritative && !item)) {
    return <Denied />;
  }
  if ((id === 'my_space' && localOnly) || item?.availability.available === false) {
    return <HostNotice tone="warning" placement="page">{t('当前环境不支持此应用')}</HostNotice>;
  }
  if (catalog.error && id === 'automation') return <HostNotice tone="error" placement="page"
    actions={<HostButton tone="primary" onClick={() => void reloadDirectory()}>{t('重试')}</HostButton>}>{t(catalog.error)}</HostNotice>;
  return children;
}
function Denied() {
  return <HostNotice tone="error" placement="page">{t('应用不存在或你没有访问权限')}</HostNotice>;
}
