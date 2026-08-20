import type { AbilityTabKey } from '../../types';
import { t } from '../../i18n';

/**
 * 能力中心的四类能力，**唯一真源**。
 *
 * 两处消费同一张表，避免类别列表在导航与页面之间分叉：
 * - `components/sidebar/items.ts` → `LAYOUT_ITEMS.ability_center.children`（侧边栏二级导航 / 用户菜单子菜单）
 * - `components/catalog/AbilityCenterPage.tsx` → 四个 pane 的渲染顺序
 *
 * 只放 key + 文案，不放 JSX：`items.ts` 属于侧边栏基础设施，不应该反向依赖能力中心的组件。
 * pane 的映射留在 `AbilityCenterPage` 里，用 `Record<AbilityTabKey, …>` 拿到穷尽性检查。
 */
export const ABILITY_TABS: ReadonlyArray<{ key: AbilityTabKey; label: string }> = [
  { key: 'agents', label: t('子智能体') },
  { key: 'skills', label: t('技能库') },
  { key: 'mcp', label: t('连接器') },
  { key: 'plugins', label: t('插件库') },
];

/**
 * 类别默认名——同时供两处使用，保证「左侧导航」与「右侧页面标题」不再各写一套
 * （曾经是 智能体/子智能体、技能/技能库、插件/插件库 三对不一致）：
 * - 侧边栏二级导航直接用 `ABILITY_TABS[].label`；
 * - 各能力页把它作为 `usePanelHeader(panel, { title })` 的 fallback。
 *
 * 管理后台改了页面标题时，侧边栏会通过 `navigation.panel_titles.<key>` 一起跟着变
 * （见 Sidebar 里的 abilityTabLabel）。
 */
export const ABILITY_TAB_TITLE: Record<AbilityTabKey, string> = {
  agents: '子智能体',
  skills: '技能库',
  mcp: '连接器',
  plugins: '插件库',
};
