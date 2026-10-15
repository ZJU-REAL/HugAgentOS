import { Layout, Tooltip } from 'antd';
import { useState } from 'react';
import { t } from '../../i18n';
import { AutomationSidebar } from '../automation/AutomationSidebar';
import { MySpaceRail } from '../myspace/MySpaceRail';
import type { SidebarProps } from './sidebarTypes';

/** Module navigation occupies its own column beside the classic conversation sidebar. */
export function ClassicModuleSidebar({ actions, panel }: { actions: SidebarProps; panel: 'automation' | 'my_space' }) {
  const [collapsed, setCollapsed] = useState(() => window.matchMedia('(max-width: 960px)').matches);
  const task = panel === 'automation';
  const title = task ? t('定时任务') : t('我的空间');
  const expandLabel = task ? t('展开定时任务侧栏') : t('展开我的空间侧栏');
  const collapseLabel = task ? t('收起定时任务侧栏') : t('收起我的空间侧栏');
  return <Layout.Sider width={240} collapsedWidth={48} collapsed={collapsed} theme="light"
    className={'jx-classicModuleShell ' + (task ? 'jx-classicTaskShell' : 'jx-classicSpaceShell')} aria-label={title}>
    {collapsed ? <Tooltip title={expandLabel} placement="right">
      <button type="button" className="jx-miniRailBtn" aria-label={expandLabel}
        onClick={() => setCollapsed(false)}><img src={task ? '/home/schedule.svg' : '/home/my-space.svg'} className="jx-miniRailIcon" alt="" /></button>
    </Tooltip> : <div className="jx-classicModuleInner">
      <header className="jx-moduleSidebarHeader">
        <strong>{title}</strong>
        <button type="button" className="jx-collapseBtn" aria-label={collapseLabel}
          onClick={() => setCollapsed(true)}><img src="/home/expand.svg" alt="" /></button>
      </header>
      {task ? <AutomationSidebar actions={actions} /> : <MySpaceRail />}
    </div>}
  </Layout.Sider>;
}
