import { Layout, Tooltip } from 'antd';
import { t } from '../../i18n';
import { AutomationSidebar } from '../automation/AutomationSidebar';
import { MySpaceRail } from '../myspace/MySpaceRail';
import type { SidebarProps } from './sidebarTypes';
import type { PanelKey } from '../../types';
import type { ModuleEntry } from './moduleNavigation';
import { ModuleSecondaryNavigation } from './ModuleSecondaryNavigation';
import { LAYOUT_ITEMS } from './items';

/** Module navigation occupies its own column beside the classic conversation sidebar. */
export function ClassicModuleSidebar({ actions, panel, more, onSelect, collapsed, onCollapse }: { actions: SidebarProps; panel: PanelKey; more: ModuleEntry[]; onSelect: SidebarProps['onSetPanel']; collapsed: boolean; onCollapse: (collapsed: boolean) => void }) {
  const task = panel === 'automation';
  const title = LAYOUT_ITEMS[panel]?.label || t('导航');
  const expandLabel = task ? t('展开定时任务侧栏') : panel === 'my_space' ? t('展开我的空间侧栏') : t('展开侧边栏');
  const collapseLabel = task ? t('收起定时任务侧栏') : panel === 'my_space' ? t('收起我的空间侧栏') : t('收起侧边栏');
  return <Layout.Sider width={240} collapsedWidth={48} collapsed={collapsed} theme="light"
    className={'jx-classicModuleShell ' + (task ? 'jx-classicTaskShell' : 'jx-classicSpaceShell')} aria-label={title}>
    {collapsed ? <Tooltip title={expandLabel} placement="right">
      <button type="button" className="jx-miniRailBtn" aria-label={expandLabel}
        onClick={() => onCollapse(false)}><img src={LAYOUT_ITEMS[panel]?.icon} className="jx-miniRailIcon" alt="" /></button>
    </Tooltip> : <div className="jx-classicModuleInner">
      <header className="jx-moduleSidebarHeader">
        <strong>{title}</strong>
        <button type="button" className="jx-collapseBtn" aria-label={collapseLabel}
          onClick={() => onCollapse(true)}><img src="/home/expand.svg" alt="" /></button>
      </header>
      {task ? <AutomationSidebar actions={actions} /> : panel === 'my_space' ? <MySpaceRail /> : <ModuleSecondaryNavigation entries={more} panel={panel} onSelect={onSelect} />}
    </div>}
  </Layout.Sider>;
}
