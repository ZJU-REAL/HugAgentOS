import { useState } from 'react';
import { useModuleNavigation } from './useModuleNavigation';
import { secondaryModule } from './moduleNavigation';
import { ClassicNavigation } from './ClassicNavigation';
import { ClassicModuleSidebar } from './ClassicModuleSidebar';
import { Layout, Tooltip } from 'antd';
import { usePageConfig } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { usePanel } from '../../routing/usePanel';
import { useMySpaceStore, useUIStore } from '../../stores';
import { ChatSidebar } from './ChatSidebar';
import type { SidebarProps } from './sidebarTypes';
import { SidebarUtilities } from './SidebarUtilities';
import '../../styles/classic-sidebar.css';

export function ClassicSidebar(actions: SidebarProps) {
  const [moduleCollapsed, setModuleCollapsed] = useState(() => window.matchMedia('(max-width: 960px)').matches);
  const count = useMySpaceStore(s => s.notifUnreadCount);
  const { primary, more } = useModuleNavigation();
  const panel = usePanel();
  const select: SidebarProps['onSetPanel'] = (target, sub) => {
    actions.onSetPanel(target, sub);
    if (!window.matchMedia('(max-width: 960px)').matches) {
      useUIStore.getState().setSiderCollapsed(false);
      setModuleCollapsed(false);
    }
  };
  const navigation = (expanded: boolean) => <ClassicNavigation primary={primary} more={more}
    panel={panel} expanded={expanded} count={count} onSelect={select} />;
  const collapsed = useUIStore(s => s.siderCollapsed);
  const setCollapsed = useUIStore(s => s.setSiderCollapsed);
  const openSearch = useUIStore(s => s.openSearchModal);
  const name = usePageConfig('branding.product_name', 'HugAgentOS');
  const subtitle = usePageConfig('branding.product_subtitle', '');
  const logo = usePageConfig('branding.logo_url', '/home/logo.svg');

  return <><Layout.Sider width={280} collapsedWidth={56} collapsed={collapsed} theme="light"
    className={'jx-sider jx-classicShell' + (collapsed ? ' jx-classicShell--collapsed' : '')}>
    {collapsed ? <div className="jx-classicMini">
      <Tooltip title={t('展开侧边栏并返回首页')} placement="right">
        <button className="jx-miniRailBtn" aria-label={t('展开侧边栏并返回首页')}
          onClick={() => { setCollapsed(false); actions.onNewChat(); }}><img src={logo} alt="" className="jx-classicLogo" /></button>
      </Tooltip>
      <Tooltip title={t('展开侧边栏')} placement="right">
        <button className="jx-miniRailBtn" aria-label={t('展开侧边栏')} onClick={() => setCollapsed(false)}><img src="/home/collapse.svg" alt="" className="jx-miniRailIcon" /></button>
      </Tooltip>
      <Tooltip title={t('新建对话')} placement="right">
        <button className="jx-miniRailBtn" aria-label={t('新建对话')} onClick={actions.onNewChat}><img src="/home/new-chat.svg" alt="" className="jx-miniRailIcon" /></button>
      </Tooltip>
      <Tooltip title={t('搜索对话')} placement="right">
        <button className="jx-miniRailBtn" aria-label={t('搜索对话')} onClick={openSearch}><img src="/home/search.svg" alt="" className="jx-miniRailIcon" /></button>
      </Tooltip>
      {navigation(false)}
      <SidebarUtilities onSetPanel={actions.onSetPanel} />
    </div> : <aside className="jx-classicInner" aria-label={t('导航')}>
      <header className="jx-classicBrand">
        <button className="jx-classicHome" title={t('回到首页')} onClick={actions.onNewChat}>
          <img src={logo} alt="" className="jx-classicLogo" />
          <span><strong>{name}</strong>{subtitle && <small>{subtitle}</small>}</span>
        </button>
        <button className="jx-searchBtn" aria-label={t('搜索对话')} onClick={openSearch}><img src="/home/search.svg" alt="" /></button>
        <button className="jx-collapseBtn" aria-label={t('收起侧边栏')} onClick={() => setCollapsed(true)}><img src="/home/expand.svg" alt="" /></button>
      </header>
      <ChatSidebar actions={actions} navigation={navigation(true)} />
      <SidebarUtilities onSetPanel={actions.onSetPanel} expanded />
    </aside>}
  </Layout.Sider>
    {(panel === 'automation' || panel === 'my_space' || secondaryModule(panel, more)) && <ClassicModuleSidebar key={panel} actions={actions} panel={panel} more={more} onSelect={select} collapsed={moduleCollapsed} onCollapse={setModuleCollapsed} />}
  </>;
}
