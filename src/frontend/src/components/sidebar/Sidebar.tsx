import { useLocation } from 'react-router';
import { applicationRoute } from '../../workbench/workspaceNavigation';
import { useApplicationEntries } from '../../applicationEntries';
import { useApplicationOpen } from '../../applicationOpen';
import { ApplicationButton } from '../../workbench/ApplicationButton';
import { MoreApplications } from '../../workbench/MoreApplications';
import { useRailCapacity } from '../../workbench/useRailCapacity';
import { navigationKeyboard } from '../../workbench/navigationKeyboard';
import { useSidebarLayoutStore } from '../../stores/sidebarLayoutStore';
import { ClassicSidebar } from './ClassicSidebar';
import { Layout, Tooltip } from 'antd';
import { usePageConfig, usePageConfigAll } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { isConversationPath } from '../../routing/navigation';
import { abilitySlug, useAbilityTab } from '../../routing/subPages';
import { usePanel } from '../../routing/usePanel';
import { useChatStore, useMySpaceStore, useUIStore } from '../../stores';
import { isAutomationHistoryChat } from '../../utils/history';
import { AutomationSidebar } from '../automation/AutomationSidebar';
import { ABILITY_TABS } from '../catalog/abilityTabs';
import { MySpaceRail } from '../myspace/MySpaceRail';
import { ChatSidebar } from './ChatSidebar';
import type { SidebarProps } from './sidebarTypes';
import { SidebarUtilities } from './SidebarUtilities';
export function Sidebar(actions: SidebarProps) {
  const layout = useSidebarLayoutStore(s => s.layout);
  return layout === 'classic' ? <ClassicSidebar {...actions} /> : <ModernSidebar {...actions} />;
}
function ModernSidebar(actions: SidebarProps) {
  const panel = usePanel();
  const abilityTab = useAbilityTab();
  const collapsed = useUIStore(s => s.siderCollapsed);
  const setCollapsed = useUIStore(s => s.setSiderCollapsed);
  const openSearch = useUIStore(s => s.openSearchModal);
  const count = useMySpaceStore(s => s.notifUnreadCount);
  const brandName = usePageConfig('branding.product_name', 'HugAgentOS');
  const logo = usePageConfig('branding.logo_url', '/home/logo.svg');
  const config = usePageConfigAll();
  const { items: LAYOUT_ITEMS, railPinned, sidebar, menu, entries } = useApplicationEntries();
  const {ref,capacity} = useRailCapacity(railPinned);
  const location = useLocation();
  const currentKey = applicationRoute(location.pathname, location.search, location.hash)?.applicationKey || 'builtin:' + panel;
  const hasSecondary = ['chat','personal_agent','ability_center','my_space','automation'].includes(panel);
  const chatModule = panel === 'chat' || panel === 'personal_agent';
  const title = chatModule ? brandName : LAYOUT_ITEMS[panel]?.label || brandName;
  const select = (target: typeof panel, sub?: string) => {
    if (target === 'chat' && panel === 'chat') {
      actions.onNewChat();
    } else if (target === 'chat' && (panel === 'automation' || isAutomationHistoryChat(useChatStore.getState().store.chats[useChatStore.getState().currentChatId]))) {
      const chat = useChatStore.getState();
      const hiddenTaskChat = panel === 'automation' && isConversationPath() ? chat.currentChatId : undefined;
      const ordinary = chat.store.order.find(id => id !== hiddenTaskChat && chat.store.chats[id] && !isAutomationHistoryChat(chat.store.chats[id]));
      if (ordinary) actions.onSelectChat(ordinary); else actions.onNewChat();
    } else actions.onSetPanel(target, sub);
    if (!window.matchMedia('(max-width: 960px)').matches) setCollapsed(false);
  };
  const {open,feedback} = useApplicationOpen(select);
  const rail = railPinned.map(key => entries.find(entry => entry.key === key)).filter(entry => entry !== undefined);
  const visibleRail = rail.slice(0, capacity);
  const moreEntries = entries.filter(entry => !visibleRail.some(visible => visible.key === entry.key)
    && (!entry.key.startsWith('builtin:') || [...sidebar, ...menu].includes(entry.item.declaration.id)));
  return <Layout.Sider width={344} collapsedWidth={64} collapsed={collapsed || !hasSecondary} theme="light" className="jx-sider jx-moduleShell">
    <nav ref={ref} onKeyDown={navigationKeyboard} className="jx-moduleRail" aria-label={t('导航')}>
      <Tooltip title={t('对话')} placement="right"><button className={`jx-moduleButton${chatModule ? ' active' : ''}`} aria-label={t('对话')} aria-pressed={chatModule} onClick={() => select('chat')}><img className="jx-moduleLogo" src={logo} alt="" /></button></Tooltip>
      {visibleRail.map(entry => <ApplicationButton key={entry.key} entry={entry} current={entry.key === currentKey}
        className="jx-moduleButton" onOpen={open} count={entry.key === 'builtin:my_space' ? count : 0} />)}
      <MoreApplications entries={moreEntries} currentKey={currentKey} onOpen={open} />
      <SidebarUtilities onSetPanel={select} />
    </nav>
    {!collapsed && hasSecondary && <aside className="jx-moduleSidebar" aria-label={title}>
      <header className="jx-moduleSidebarHeader"><strong>{title}</strong><button className="jx-searchBtn" aria-label={t('搜索对话')} onClick={openSearch}><img src="/home/search.svg" alt="" /></button><button className="jx-collapseBtn" aria-label={t('收起侧边栏')} onClick={() => setCollapsed(true)}><img src="/home/expand.svg" alt="" /></button></header>
      {chatModule && <ChatSidebar actions={actions} />}
      {panel === 'ability_center' && <nav className="jx-moduleCategories" aria-label={t('能力中心')}>{ABILITY_TABS.map(item => <button key={item.key} className={`jx-navItem${abilityTab === item.key ? ' active' : ''}`} aria-current={abilityTab === item.key ? 'page' : undefined} onClick={() => select('ability_center', abilitySlug(item.key))}><img className="jx-navItemIcon" src={item.icon} alt="" />{t(config.navigation.panel_titles[item.key] || item.label)}</button>)}</nav>}
      {panel === 'my_space' && <MySpaceRail />}
      {panel === 'automation' && <AutomationSidebar actions={actions} />}

    </aside>}
    <span className="jx-applicationFeedback" role="status">{feedback}</span>
  </Layout.Sider>;
}
