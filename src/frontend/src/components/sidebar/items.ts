import type { AbilityTabKey, PanelKey } from '../../types';
import { ABILITY_TABS } from '../catalog/abilityTabs';
import { t } from '../../i18n';

/** 侧边栏二级导航项：目前只有「能力中心」用到，点了切页内展示的能力类别。
 *  只有一个消费者，所以 key 直接收窄到 AbilityTabKey、点击直接调 setAbilityTab——
 *  不为单一用例造一套通用的子导航协议。 */
export interface LayoutSubItemMeta {
  key: AbilityTabKey;
  label: string;
}

export interface LayoutItemMeta {
  label: string;
  icon: string;
  targetPanel: PanelKey;
  activePanels?: PanelKey[];
  requiresLab?: boolean;
  /** 有 children 的项在侧边栏里展开成「一级标题 + 二级列表」（参考设计里的「扩展」样式） */
  children?: readonly LayoutSubItemMeta[];
}

// 「知识库」不再是侧边栏一级入口——已并入「我的空间」的顶部 Tab（MySpacePanel TABS）；
// 「子智能体」也不再单列——已成为「能力中心」的二级导航项。两者的 PanelKey 仍保留可用，
// 但从这里移除后，即便后台页面配置里还存着旧 key，itemVisible() 也会因 meta 为空而过滤掉。
export const LAYOUT_ITEMS: Record<string, LayoutItemMeta> = {
  ability_center: {
    label: t('能力中心'),
    icon: '/home/capability.svg',
    targetPanel: 'ability_center',
    activePanels: ['ability_center'],
    // 类别列表的真源在 catalog/abilityTabs.ts，与页面里的 pane 顺序共用一张表
    children: ABILITY_TABS,
  },
  app_center:     { label: t('应用中心'), icon: '/home/app-center.svg', targetPanel: 'app_center', activePanels: ['app_center'] },
  automation:     { label: t('定时任务'), icon: '/home/schedule.svg',   targetPanel: 'automation', activePanels: ['automation'] },
  sites:          { label: t('站点'),     icon: '/home/sites.svg',      targetPanel: 'sites',      activePanels: ['sites'], requiresLab: true },
  projects:       { label: t('项目'),     icon: '/home/projects.svg',   targetPanel: 'projects',   activePanels: ['projects', 'project_detail'] },
  my_space:       { label: t('我的空间'), icon: '/home/my-space.svg',   targetPanel: 'my_space',   activePanels: ['my_space'] },
  settings:       { label: t('设置'),     icon: '/home/settings.svg',   targetPanel: 'settings' },
  lab:            { label: t('实验室'),   icon: '/home/new-icons/lab.svg', targetPanel: 'lab',     requiresLab: true },
};

export const LAYOUT_ITEM_KEYS = Object.keys(LAYOUT_ITEMS);
