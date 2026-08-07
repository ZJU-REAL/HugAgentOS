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
  { key: 'agents', label: t('智能体') },
  { key: 'skills', label: t('技能') },
  { key: 'mcp', label: t('连接器') },
  { key: 'plugins', label: t('插件') },
];
