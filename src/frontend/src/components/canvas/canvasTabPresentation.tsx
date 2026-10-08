import { ApartmentOutlined, GlobalOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { usePluginUiStore } from '../../stores/pluginUiStore';
import type { CanvasTab } from '../../stores/canvasStore';
import { getFileIconSrc } from '../../utils/fileIcon';
import { AgentIcon } from '../agent/AgentIcon';

export function tabTitle(tab: CanvasTab): string {
  if (tab.kind === 'file') return tab.artifact.name;
  if (tab.kind === 'subagent') return tab.target.agent.name;
  if (tab.kind === 'plugin') return tab.target.title || t('插件视图');
  return t('本体校验');
}
export function tabIcon(tab: CanvasTab) {
  if (tab.kind === 'subagent') return <AgentIcon agent={tab.target.agent} size={18} />;
  if (tab.kind === 'file') return <img src={getFileIconSrc(tab.artifact.name)} width="17" height="17" alt="" />;
  if (tab.kind === 'plugin') {
    const store = usePluginUiStore.getState();
    const module = store.findModule(tab.target.slug, tab.target.canvasId)?.contribution;
    const icon = store.findCanvas(tab.target.slug, tab.target.canvasId)?.contribution.icon || module?.icon;
    return icon ? <img src={icon} width="17" height="17" alt="" /> : module?.resource_binding ? <GlobalOutlined /> : <ApartmentOutlined />;
  }
  return <SafetyCertificateOutlined />;
}
