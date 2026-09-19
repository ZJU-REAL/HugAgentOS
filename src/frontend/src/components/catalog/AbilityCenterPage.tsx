import { DesktopAvailableCapabilities } from '../desktop/DesktopAvailableCapabilities';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useEffect, useState, type ReactNode } from 'react';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import type { DeviceCapabilityKind } from '../../api';
import type { AbilityTabKey } from '../../types';
import { AgentPanel } from '../agent/AgentPanel';
import { ABILITY_TABS } from './abilityTabs';
import { useAbilityTab } from '../../routing/subPages';
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
  const abilityTab = useAbilityTab();
  // 四个 pane 首次访问才挂载（各自会拉一份列表），挂载后常驻——这是本页的懒加载状态，
  // 不是「当前在哪个类别」的副本。
  const [visited, setVisited] = useState<Set<AbilityTabKey>>(() => new Set([abilityTab]));
  if (!visited.has(abilityTab)) setVisited(new Set(visited).add(abilityTab));
  const partial = useDeploymentModeStore((s) => s.partialCapabilities);
  const dual = useDeploymentModeStore((s) => s.provisionMode === 'dual');
  const load = useDesktopCapabilityStore((s) => s.load);
  useEffect(() => {
    if (!dual) return;
    const kind: DeviceCapabilityKind = abilityTab === 'skills' ? 'skill' : abilityTab === 'plugins' ? 'plugin' : abilityTab === 'mcp' ? 'mcp' : 'agent';
    // 回到窗口才重新拉一次来源标记。定时器在这里是多余的：用户看不见的时候刷新没有
    // 意义，看得见的时候 focus / visibilitychange 已经覆盖了每一次回到页面。
    const refresh = () => { if (document.visibilityState === 'visible') void load(kind, true); };
    refresh();
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [dual, abilityTab, load]);
  if (partial) return <DesktopAvailableCapabilities />;

  return (
    <div className="jx-abilityCenter">
      <div className="jx-abilityCenterBody">
        {ABILITY_TABS.map(({ key }) => (
          <div
            key={key}
            className={`jx-abilityCenterPane${abilityTab === key ? ' active' : ''}`}
            aria-hidden={abilityTab !== key}
          >
            {visited.has(key) ? PANES[key]() : null}
          </div>
        ))}
      </div>
    </div>
  );
}
