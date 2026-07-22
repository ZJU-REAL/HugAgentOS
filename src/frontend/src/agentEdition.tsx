import type { UserAgentItem } from './stores/agentStore';
import { t } from './i18n';

export function useEditionAgentPolicy() {
  return {
    canManage: (_agent: UserAgentItem) => false,
    includeInLibrary: (_agent: UserAgentItem) => false,
    creatorLabel: (_agent: UserAgentItem) => t('系统内置'),
  };
}

export function EditionAgentBadge({ agent: _agent }: { agent: UserAgentItem }) {
  return null;
}
