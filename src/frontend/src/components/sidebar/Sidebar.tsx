import { EllipsisOutlined } from '@ant-design/icons';
import { Badge, Dropdown, Layout, Tooltip } from 'antd';
import { usePageConfig, usePageConfigAll } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { isConversationPath } from '../../routing/navigation';
import { abilitySlug, useAbilityTab } from '../../routing/subPages';
import { usePanel } from '../../routing/usePanel';
import { useAuthStore, useChatStore, useMySpaceStore, useUIStore } from '../../stores';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { isAutomationHistoryChat } from '../../utils/history';
import { DEFAULT_MENU_ITEMS, DEFAULT_SIDEBAR_ITEMS } from '../../utils/pageConfigDefaults';
import { AutomationSidebar } from '../automation/AutomationSidebar';
import { ABILITY_TABS } from '../catalog/abilityTabs';
import { MySpaceRail } from '../myspace/MySpaceRail';
import { ChatSidebar } from './ChatSidebar';
import { LAYOUT_ITEMS } from './items';
import type { SidebarProps } from './sidebarTypes';
import { SidebarUtilities } from './SidebarUtilities';
const PRIMARY = ['my_space', 'automation', 'ability_center'] as const;
export function Sidebar(actions: SidebarProps) {
  const panel = usePanel();
  const abilityTab = useAbilityTab();
  const collapsed = useUIStore(s => s.siderCollapsed);
  const setCollapsed = useUIStore(s => s.setSiderCollapsed);
  const openSearch = useUIStore(s => s.openSearchModal);
  const authUser = useAuthStore(s => s.authUser);
  const mode = useDeploymentModeStore(s => s.provisionMode);
  const count = useMySpaceStore(s => s.notifUnreadCount);
  const brandName = usePageConfig('branding.product_name', 'HugAgentOS');
  const logo = usePageConfig('branding.logo_url', '/home/logo.svg');
  const configuredSidebar = usePageConfig<string[]>('navigation.sidebar_items', DEFAULT_SIDEBAR_ITEMS);
  const configuredMenu = usePageConfig<string[]>('navigation.menu_items', DEFAULT_MENU_ITEMS);
  const config = usePageConfigAll();
  const visible = (key: string) => LAYOUT_ITEMS[key] && (!LAYOUT_ITEMS[key].requiresLab || authUser?.lab_enabled !== false) && !(key === 'my_space' && mode === 'local_only');
  const more = [...new Set([...configuredSidebar, ...configuredMenu])].filter(key => !PRIMARY.some(primary => primary === key) && key !== 'settings' && visible(key));
  const chatModule = panel === 'chat';
  const title = chatModule ? brandName : LAYOUT_ITEMS[panel === 'project_detail' ? 'projects' : panel]?.label || brandName;
  const select = (target: typeof panel, sub?: string) => {
    if (target === 'chat' && (panel === 'automation' || isAutomationHistoryChat(useChatStore.getState().store.chats[useChatStore.getState().currentChatId]))) {
      const chat = useChatStore.getState();
      const hiddenTaskChat = panel === 'automation' && isConversationPath() ? chat.currentChatId : undefined;
      const ordinary = chat.store.order.find(id => id !== hiddenTaskChat && chat.store.chats[id] && !isAutomationHistoryChat(chat.store.chats[id]));
      if (ordinary) actions.onSelectChat(ordinary); else actions.onNewChat();
    } else actions.onSetPanel(target, sub);
    if (!window.matchMedia('(max-width: 960px)').matches) setCollapsed(false);
  };
  return <Layout.Sider width={344} collapsedWidth={64} collapsed={collapsed} theme="light" className="jx-sider jx-moduleShell">
    <nav className="jx-moduleRail" aria-label={t('导航')}>
      <Tooltip title={t('对话')} placement="right"><button className={`jx-moduleButton${chatModule ? ' active' : ''}`} aria-label={t('对话')} aria-pressed={chatModule} onClick={() => select('chat')}><img className="jx-moduleLogo" src={logo} alt="" /></button></Tooltip>
      {PRIMARY.filter(visible).map(key => { const meta = LAYOUT_ITEMS[key]; return <Tooltip key={key} title={meta.label} placement="right"><button className={`jx-moduleButton${panel === meta.targetPanel ? ' active' : ''}`} aria-label={meta.label} aria-pressed={panel === meta.targetPanel} onClick={() => select(meta.targetPanel)}>{key === 'my_space' ? <Badge count={count} size="small"><img src={meta.icon} alt="" /></Badge> : <img src={meta.icon} alt="" />}</button></Tooltip>; })}
      {more.length > 0 && <Dropdown menu={{ items: more.map(key => ({ key, label: LAYOUT_ITEMS[key].label, icon: <img className="jx-moduleMenuIcon" src={LAYOUT_ITEMS[key].icon} alt="" />, onClick: () => select(LAYOUT_ITEMS[key].targetPanel) })) }} trigger={['click']} placement="bottomLeft"><button className={`jx-moduleButton${more.some(key => LAYOUT_ITEMS[key].activePanels?.includes(panel)) ? ' active' : ''}`} aria-label={t('更多')}><EllipsisOutlined /></button></Dropdown>}
      <SidebarUtilities onSetPanel={select} />
    </nav>
    {!collapsed && <aside className="jx-moduleSidebar" aria-label={title}>
      <header className="jx-moduleSidebarHeader"><strong>{title}</strong><button className="jx-searchBtn" aria-label={t('搜索对话')} onClick={openSearch}><img src="/home/search.svg" alt="" /></button><button className="jx-collapseBtn" aria-label={t('收起侧边栏')} onClick={() => setCollapsed(true)}><img src="/home/expand.svg" alt="" /></button></header>
      {chatModule && <ChatSidebar actions={actions} />}
      {panel === 'ability_center' && <nav className="jx-moduleCategories" aria-label={t('能力中心')}>{ABILITY_TABS.map(item => <button key={item.key} className={`jx-navItem${abilityTab === item.key ? ' active' : ''}`} aria-current={abilityTab === item.key ? 'page' : undefined} onClick={() => select('ability_center', abilitySlug(item.key))}><img className="jx-navItemIcon" src={item.icon} alt="" />{t(config.navigation.panel_titles[item.key] || item.label)}</button>)}</nav>}
      {panel === 'my_space' && <MySpaceRail />}
      {panel === 'automation' && <AutomationSidebar actions={actions} />}
      {!chatModule && !['ability_center', 'my_space', 'automation'].includes(panel) && <nav className="jx-moduleCategories">{more.map(key => <button key={key} className={`jx-navItem${(LAYOUT_ITEMS[key].activePanels?.includes(panel) || LAYOUT_ITEMS[key].targetPanel === panel) ? ' active' : ''}`} onClick={() => select(LAYOUT_ITEMS[key].targetPanel)}><img className="jx-navItemIcon" src={LAYOUT_ITEMS[key].icon} alt="" />{LAYOUT_ITEMS[key].label}</button>)}</nav>}
    </aside>}
  </Layout.Sider>;
}
