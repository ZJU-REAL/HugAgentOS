import type { ReactNode } from 'react';
import { useCatalogStore } from '../../stores';
import type { AbilityTabKey } from '../../types';
import { AgentPanel } from '../agent/AgentPanel';
import { ABILITY_TABS } from './abilityTabs';
import { SkillsPage } from './SkillsPage';
import { McpPage } from './McpPage';
import { PluginsPage } from './PluginsPage';

/** 每个类别对应的 pane。写成 Record 而非数组，是为了拿到对 AbilityTabKey 的穷尽性检查——
 *  往 ABILITY_TABS 里加一个类别却忘了加 pane，会在编译期报错而不是渲染出一片空白。 */
const PANES: Record<AbilityTabKey, () => ReactNode> = {
  agents: () => <AgentPanel embedded />,
  skills: () => <SkillsPage embedded />,
  mcp: () => <McpPage embedded />,
  plugins: () => <PluginsPage />,
};

/**
 * 能力中心：智能体 / 技能 / 连接器 / 插件。
 *
 * 类别切换在**左侧边栏的二级导航**上（`LAYOUT_ITEMS.ability_center.children`，与这里共用
 * `ABILITY_TABS` 这张表），选中项存在 `catalogStore.abilityTab`，所以本页不画 Tab 栏。
 *
 * pane 采取「首次访问才挂载、之后常驻」：四个 pane 各自会在挂载时拉自己的列表（智能体、技能、
 * MCP、插件），一上来全挂等于把四份请求都打出去；只开过一个类别的用户不该为另外三个买单。
 * 挂载后不再卸载，切回来仍保留滚动位置与列表状态。
 */
export function AbilityCenterPage() {
  const abilityTab = useCatalogStore((s) => s.abilityTab);
  const visited = useCatalogStore((s) => s.visitedAbilityTabs);

  return (
    <div className="jx-abilityCenter">
      <div className="jx-abilityCenterBody">
        {ABILITY_TABS.map(({ key }) => (
          <div
            key={key}
            className={`jx-abilityCenterPane${abilityTab === key ? ' active' : ''}`}
            aria-hidden={abilityTab !== key}
          >
            {visited.includes(key) ? PANES[key]() : null}
          </div>
        ))}
      </div>
    </div>
  );
}
