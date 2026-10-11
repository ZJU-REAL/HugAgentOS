import { Badge, Layout, Tooltip } from 'antd';
import { useState } from 'react';
import { usePageConfig, usePageConfigAll } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { abilitySlug, useAbilityTab } from '../../routing/subPages';
import { usePanel } from '../../routing/usePanel';
import { loadJsonPref, saveJsonPref } from '../../storage';
import { useAuthStore, useMySpaceStore, useUIStore } from '../../stores';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { DEFAULT_SIDEBAR_ITEMS } from '../../utils/pageConfigDefaults';
import { ChatSidebar } from './ChatSidebar';
import { AutomationSidebar } from '../automation/AutomationSidebar';
import { MySpaceRail } from '../myspace/MySpaceRail';
import { LAYOUT_ITEMS } from './items';
import type { SidebarProps } from './sidebarTypes';
import { SidebarUtilities } from './SidebarUtilities';
import '../../styles/classic-sidebar.css';

const COLLAPSED_KEY = 'hugagent_ui_nav_collapsed_v1';

export function ClassicSidebar(actions: SidebarProps) {
  const collapsed = useUIStore(s => s.siderCollapsed);
  const setCollapsed = useUIStore(s => s.setSiderCollapsed);
  const openSearch = useUIStore(s => s.openSearchModal);
  const authUser = useAuthStore(s => s.authUser);
  const mode = useDeploymentModeStore(s => s.provisionMode);
  const count = useMySpaceStore(s => s.notifUnreadCount);
  const panel = usePanel();
  const abilityTab = useAbilityTab();
  const config = usePageConfigAll();
  const name = usePageConfig('branding.product_name', 'HugAgentOS');
  const subtitle = usePageConfig('branding.product_subtitle', '');
  const logo = usePageConfig('branding.logo_url', '/home/logo.svg');
  const configured = usePageConfig<string[]>('navigation.sidebar_items', DEFAULT_SIDEBAR_ITEMS);
  const items = [...new Set(configured)].filter(key => LAYOUT_ITEMS[key]
    && key !== 'settings'
    && (!LAYOUT_ITEMS[key].requiresLab || authUser?.lab_enabled !== false)
    && !(key === 'my_space' && mode === 'local_only'));
  const [groups, setGroups] = useState(() => loadJsonPref<Record<string, boolean>>(COLLAPSED_KEY, {}));
  const select: SidebarProps['onSetPanel'] = (target, sub) => {
    actions.onSetPanel(target, sub);
  };
  const renderIcon = (key: string, className: string) => {
    const icon = <img src={LAYOUT_ITEMS[key].icon} alt="" className={className} />;
    return key === 'my_space' && count > 0 ? <Badge count={count} size="small">{icon}</Badge> : icon;
  };
  const navigation = <nav className="jx-classicNav" aria-label={t('导航')}>
    {items.map(key => {
      const meta = LAYOUT_ITEMS[key];
      const isCollapsed = !!groups[key];
      const active = meta.activePanels?.includes(panel) || panel === meta.targetPanel;
      return <div key={key}>
        <button type="button" className={`jx-navItem${active && (!meta.children?.length || isCollapsed) ? ' active' : ''}`}
          aria-expanded={meta.children?.length ? !isCollapsed : undefined}
          onClick={() => {
            if (!meta.children?.length) { select(meta.targetPanel); return; }
            const next = { ...groups, [key]: !isCollapsed };
            setGroups(next);
            saveJsonPref(COLLAPSED_KEY, next);
            if (isCollapsed) select(meta.targetPanel);
          }}>
          {renderIcon(key, 'jx-navItemIcon')}<span>{meta.label}</span>
        </button>
        {!!meta.children?.length && !isCollapsed && <div className="jx-classicSubList">
          {meta.children.map(child => <button type="button" key={child.key}
            className={`jx-classicSubItem${active && abilityTab === child.key ? ' active' : ''}`}
            aria-current={active && abilityTab === child.key ? 'page' : undefined}
            onClick={() => select(meta.targetPanel, abilitySlug(child.key))}>
            {t(config.navigation.panel_titles[child.key] || child.label)}
          </button>)}
        </div>}
      </div>;
    })}
  </nav>;
  return <Layout.Sider width={280} collapsedWidth={56} collapsed={collapsed} theme="light"
    className={`jx-sider jx-classicShell${collapsed ? ' jx-classicShell--collapsed' : ''}`}>
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
      {items.map(key => <Tooltip key={key} title={LAYOUT_ITEMS[key].label} placement="right">
        <button className="jx-miniRailBtn" aria-label={LAYOUT_ITEMS[key].label} onClick={() => select(LAYOUT_ITEMS[key].targetPanel)}>
          {renderIcon(key, 'jx-miniRailIcon')}
        </button>
      </Tooltip>)}
      <SidebarUtilities onSetPanel={select} classic />
    </div> : <aside className="jx-classicInner" aria-label={t('导航')}>
      <header className="jx-classicBrand">
        <button className="jx-classicHome" title={t('回到首页')} onClick={actions.onNewChat}>
          <img src={logo} alt="" className="jx-classicLogo" />
          <span><strong>{name}</strong>{subtitle && <small>{subtitle}</small>}</span>
        </button>
        <button className="jx-searchBtn" aria-label={t('搜索对话')} onClick={openSearch}><img src="/home/search.svg" alt="" /></button>
        <button className="jx-collapseBtn" aria-label={t('收起侧边栏')} onClick={() => setCollapsed(true)}><img src="/home/expand.svg" alt="" /></button>
      </header>
      <ChatSidebar actions={actions}>
        {navigation}
        {panel === 'my_space' && <MySpaceRail />}
        {panel === 'automation' && <AutomationSidebar actions={actions} />}
      </ChatSidebar>
      <SidebarUtilities onSetPanel={select} classic expanded />
    </aside>}
  </Layout.Sider>;
}
