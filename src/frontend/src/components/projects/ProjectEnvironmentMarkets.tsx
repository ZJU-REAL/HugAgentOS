import { useMemo } from 'react';
import {
  getMarketplaceAgentDetail, getMarketplaceAgents, installMarketplaceAgent,
  getMarketplaceSkillDetail, getMarketplaceSkills, installMarketplaceSkill,
} from '../../api';
import { SkillMarketplaceModal } from '../catalog/SkillMarketplaceModal';
import { PluginMarketplaceModal } from '../catalog/PluginMarketplaceModal';
import { McpMarketplaceModal } from '../catalog/McpMarketplaceModal';
import { AgentMarketplaceModal } from '../agent/AgentMarketplaceModal';
import { useAuthStore } from '../../stores';
import type { EnvironmentField } from '../../api/projectEnvironment';
import { t } from '../../i18n';

export default function ProjectEnvironmentMarkets({ kind, onClose, onInstalled }: {
  kind: EnvironmentField | null;
  onClose: () => void;
  onInstalled: (field: EnvironmentField, id?: string) => void;
}) {
  const canAddMcp = useAuthStore(s => s.authUser?.can_add_mcp === true);
  const skills = useMemo(() => ({
    loadList: getMarketplaceSkills, loadDetail: getMarketplaceSkillDetail,
    install: installMarketplaceSkill,
  }), []);
  const agents = useMemo(() => ({
    loadList: getMarketplaceAgents, loadDetail: getMarketplaceAgentDetail,
    install: installMarketplaceAgent,
  }), []);
  const label = t('获取后加入此项目的待绑定资源，保存后生效');
  return <>
    {kind === 'skill_ids' && <SkillMarketplaceModal open onClose={onClose}
      fetchers={skills} scopeLabel={label}
      onInstalled={result => onInstalled('skill_ids', result.id)} />}
    {kind === 'plugin_ids' && <PluginMarketplaceModal open onClose={onClose}
      scopeLabel={label} onInstalled={result => onInstalled('plugin_ids', result.install_id)} />}
    {kind === 'agent_ids' && <AgentMarketplaceModal open onClose={onClose}
      fetchers={agents} scopeLabel={label} onInstalled={() => onInstalled('agent_ids')} />}
    {kind === 'mcp_ids' && <McpMarketplaceModal open onClose={onClose}
      canInstall={canAddMcp} onInstalled={() => onInstalled('mcp_ids')} />}
  </>;
}
