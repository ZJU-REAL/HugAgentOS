import type { PanelKey } from '../../types';
import { t } from '../../i18n';

export interface LayoutItemMeta {
  label: string;
  icon: string;
  targetPanel: PanelKey;
  activePanels?: PanelKey[];
  requiresLab?: boolean;
}

export const LAYOUT_ITEMS: Record<string, LayoutItemMeta> = {
  agents:         { label: t('子智能体'), icon: '/home/sub-agent.svg',     targetPanel: 'agents',         activePanels: ['agents'] },
  kb:             { label: t('知识库'),   icon: '/home/knowledge.svg',     targetPanel: 'kb',             activePanels: ['kb'] },
  app_center:     { label: t('应用中心'), icon: '/home/app-center.svg',    targetPanel: 'app_center',     activePanels: ['app_center'] },
  projects:       { label: t('项目'),     icon: '/home/projects.svg',      targetPanel: 'projects',       activePanels: ['projects', 'project_detail'] },
  my_space:       { label: t('我的空间'), icon: '/home/my-space.svg',      targetPanel: 'my_space',       activePanels: ['my_space'] },
  settings:       { label: t('设置'),     icon: '/home/settings.svg',      targetPanel: 'settings' },
  ability_center: { label: t('能力中心'), icon: '/home/capability.svg',    targetPanel: 'ability_center' },
  lab:            { label: t('实验室'),   icon: '/home/new-icons/lab.svg', targetPanel: 'lab',            requiresLab: true },
};

export const LAYOUT_ITEM_KEYS = Object.keys(LAYOUT_ITEMS);
