import { useState } from 'react';
import { Button } from 'antd';
import { ShopOutlined } from '@ant-design/icons';
import { getMarketplaceSkills, getMarketplaceSkillDetail, installMarketplaceSkill } from '../../../api';
import { useAuthStore } from '../../../stores';
import { SkillMarketplaceModal } from '../../catalog/SkillMarketplaceModal';
import { McpMarketplaceModal } from '../../catalog/McpMarketplaceModal';
import { PluginMarketplaceModal } from '../../catalog/PluginMarketplaceModal';
import { t } from '../../../i18n';

export type CapabilityField = 'skill_ids' | 'mcp_ids' | 'plugin_ids';
const skillFetchers = {
  loadList: () => getMarketplaceSkills(),
  loadDetail: (slug: string) => getMarketplaceSkillDetail(slug),
  install: (slug: string, secrets?: Record<string, string>) => installMarketplaceSkill(slug, secrets),
};
const excludedPlugins = ['browser-automation'];

export function PersonalCapabilityMarkets({ field, disabled, onImported }: {
  field: CapabilityField;
  disabled: boolean;
  onImported: (field: CapabilityField, id?: string) => void;
}) {
  const user = useAuthStore(state => state.authUser);
  const [market, setMarket] = useState<CapabilityField>();
  const scopeLabel = t('导入后加入待保存的选择；保存能力绑定后生效。');
  return <>
    {field === 'mcp_ids' && user?.can_add_mcp && <Button disabled={disabled} icon={<ShopOutlined />} onClick={() => setMarket('mcp_ids')}>{t('从工具市场导入')}</Button>}
    {field === 'skill_ids' && user?.can_add_skill && <Button disabled={disabled} icon={<ShopOutlined />} onClick={() => setMarket('skill_ids')}>{t('从技能市场导入')}</Button>}
    {field === 'plugin_ids' && user?.can_import_plugin && <Button disabled={disabled} icon={<ShopOutlined />} onClick={() => setMarket('plugin_ids')}>{t('从插件市场导入')}</Button>}
    {market === 'skill_ids' && <SkillMarketplaceModal open onClose={() => setMarket(undefined)}
      fetchers={skillFetchers} scopeLabel={scopeLabel}
      onInstalled={result => onImported('skill_ids', result.id)} />}
    {market === 'mcp_ids' && <McpMarketplaceModal open canInstall={user?.can_add_mcp === true}
      onClose={() => setMarket(undefined)} onInstalled={result => onImported('mcp_ids', result.server_id)} />}
    {market === 'plugin_ids' && <PluginMarketplaceModal open onClose={() => setMarket(undefined)}
      scopeLabel={scopeLabel} excludedSlugs={excludedPlugins}
      onInstalled={result => onImported('plugin_ids', result.install_id)} />}
  </>;
}
