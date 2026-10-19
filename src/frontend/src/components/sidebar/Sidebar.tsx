import { useModuleNavigation } from './useModuleNavigation';
import { ModuleButton } from './ModuleButton';
import { MoreModules } from './MoreModules';
import { ModuleSecondaryNavigation } from './ModuleSecondaryNavigation';
import { secondaryModule } from './moduleNavigation';
import { LAYOUT_ITEMS } from './items';
import { navigationKeyboard } from './navigationKeyboard';
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
  const { primary, more } = useModuleNavigation();
  const moreModule = secondaryModule(panel, more);
  const hasSecondary = moreModule || ['chat','personal_agent','ability_center','my_space','automation'].includes(panel);
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
  return <Layout.Sider width={344} collapsedWidth={64} collapsed={collapsed || !hasSecondary} theme="light" className="jx-sider jx-moduleShell">
    <nav onKeyDown={navigationKeyboard} className="jx-moduleRail" aria-label={t('导航')}>
      <Tooltip title={t('对话')} placement="right"><button className={`jx-moduleButton${chatModule ? ' active' : ''}`} aria-label={t('对话')} aria-pressed={chatModule} onClick={() => select('chat')}><img className="jx-moduleLogo" src={logo} alt="" /></button></Tooltip>
      {primary.map(entry => <ModuleButton key={entry.id} entry={entry} active={panel === entry.meta.targetPanel}
        className="jx-moduleButton" onSelect={select} count={entry.id === 'my_space' ? count : 0} />)}
      <MoreModules entries={more} panel={panel} onSelect={select} />
      <SidebarUtilities onSetPanel={select} />
    </nav>
    {!collapsed && hasSecondary && <aside className="jx-moduleSidebar" aria-label={title}>
      <header className="jx-moduleSidebarHeader"><strong>{title}</strong><button className="jx-searchBtn" aria-label={t('搜索对话')} onClick={openSearch}><img src="/home/search.svg" alt="" /></button><button className="jx-collapseBtn" aria-label={t('收起侧边栏')} onClick={() => setCollapsed(true)}><img src="/home/expand.svg" alt="" /></button></header>
      {chatModule && <ChatSidebar actions={actions} />}
      {panel === 'ability_center' && <nav className="jx-moduleCategories" aria-label={t('能力中心')}>{ABILITY_TABS.map(item => <button key={item.key} className={`jx-navItem${abilityTab === item.key ? ' active' : ''}`} aria-current={abilityTab === item.key ? 'page' : undefined} onClick={() => select('ability_center', abilitySlug(item.key))}><img className="jx-navItemIcon" src={item.icon} alt="" />{t(config.navigation.panel_titles[item.key] || item.label)}</button>)}</nav>}
      {panel === 'my_space' && <MySpaceRail />}
      {panel === 'automation' && <AutomationSidebar actions={actions} />}
      {moreModule && <ModuleSecondaryNavigation entries={more} panel={panel} onSelect={select} />}
    </aside>}
  </Layout.Sider>;
}
