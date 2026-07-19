import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  Layout, Input, Dropdown, Tooltip, Badge, Modal, message,
} from 'antd';
import type { MenuProps } from 'antd';
import { EASE, LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { t } from '../../i18n';
import {
  DeleteOutlined, EditOutlined,
  PushpinOutlined, PushpinFilled, StarOutlined, StarFilled,
  EllipsisOutlined, CaretDownOutlined, FolderOutlined,
  ExportOutlined, ExclamationCircleFilled,
} from '@ant-design/icons';
import { useUIStore, useChatStore, useAuthStore, useMySpaceStore, useAutomationChatStore, useAutomationStore } from '../../stores';
import { useCatalogStore } from '../../stores/catalogStore';
import { useProjectStore } from '../../stores/projectStore';
import { usePageConfig } from '../../hooks/usePageConfig';
import { LAYOUT_ITEMS } from './items';
import { DEFAULT_SIDEBAR_ITEMS, DEFAULT_MENU_ITEMS } from '../../utils/pageConfigDefaults';
import { buildSidebarChatItems } from '../../utils/history';
import { resolveAvatarUrl } from '../../utils/avatar';
import { getAutomationRuns } from '../../api';
import type { ChatItem, PanelKey } from '../../types';
import { HELP_DOCUMENTATION_URL, IS_COMMUNITY_EDITION_BUILD } from '../../edition';

// The sidebar has 3 groups: Projects (project row + nested chats under it) / Automation / History (pinned items sorted to the top of the group).
type HistoryGroupKey = 'projects' | 'automation' | 'history';

/** A project group in the sidebar's "Projects" section: project row + the list of chats belonging to it. */
interface SidebarProjectGroup {
  projectId: string;
  name: string;
  pinned: boolean;
  /** Project is known in the project list (false = only reconstructed as a fallback from leftover chat.projectId) */
  known: boolean;
  items: ChatItem[];
  lastActivity: number;
}

const { Sider } = Layout;

// Chat list item add/remove animation: enter 0.22s float-up expand / exit 0.18s height collapse (items below smoothly reposition via layout).
const HISTORY_ITEM_ENTER = { duration: 0.22, ease: EASE.brandOut };
const HISTORY_ITEM_EXIT = { duration: 0.18, ease: EASE.exit };

// Search has been moved down into SearchModal (mounted directly by App.tsx); the sidebar no longer holds search-related props.
interface SidebarProps {
  onNewChat: () => void;
  onDeleteChat: (id: string) => void;
  onTogglePinned: (id: string) => void;
  onToggleFavorite: (id: string) => void;
  onStartRename: (item: ChatItem) => void;
  onCommitRename: (id: string) => void;
  onExportChat: (id: string) => void;
  onSelectChat: (id: string) => void;
  onSetPanel: (p: PanelKey) => void;
}


export function Sidebar({
  onNewChat, onDeleteChat, onTogglePinned, onToggleFavorite,
  onStartRename, onCommitRename, onExportChat, onSelectChat,
  onSetPanel,
}: SidebarProps) {
  const {
    siderCollapsed,
    setSiderCollapsed,
    openSearchModal,
    editingChatId, setEditingChatId,
    editingTitle, setEditingTitle,
    pendingConfirm,
    pendingDesignPick,
  } = useUIStore();
  const { store, currentChatId, chatsLoading, sendingChatIds, updateStore, addBackendSessionId } = useChatStore();
  const { authUser, doLogout } = useAuthStore();
  // ── Page config (text and branding configurable via the admin console) ──
  const cfgProductName = usePageConfig('branding.product_name', 'HugAgentOS');
  const cfgProductSub = usePageConfig('branding.product_subtitle', 'HugAgentOS AI 智能助手');
  const cfgLogoUrl = usePageConfig('branding.logo_url', '/home/logo.svg');
  const cfgBtnNewChat = usePageConfig('texts.btn_new_chat', '新建对话');
  const cfgEmptyState = usePageConfig('texts.sidebar_empty_state', '暂无对话记录');
  const cfgLogoutTitle = usePageConfig('texts.dialog_logout_confirm_title', '确认退出登录？');
  const cfgLogoutContent = usePageConfig('texts.dialog_logout_confirm_content', '退出登录不会丢失任何数据，你仍可以登录此账号。');
  const cfgLogoutOk = usePageConfig('texts.dialog_logout_confirm_ok', '退出登录');
  const sidebarLayoutKeys = usePageConfig<string[]>('navigation.sidebar_items', DEFAULT_SIDEBAR_ITEMS);
  const menuLayoutKeys = usePageConfig<string[]>('navigation.menu_items', DEFAULT_MENU_ITEMS);
  const { panel } = useCatalogStore();
  const notifUnreadCount = useMySpaceStore((s) => s.notifUnreadCount);
  const sidebarTasks = useAutomationChatStore((s) => s.sidebarTasks);
  const sidebarPrefs = useAutomationChatStore((s) => s.sidebarPrefs);
  const automationActiveGroup = useAutomationChatStore((s) => s.activeGroup);
  const selectedRunId = useAutomationChatStore((s) => s.selectedRunId);
  const enterAutomationChat = useAutomationChatStore((s) => s.enterAutomationChat);
  const updateSidebarTask = useAutomationChatStore((s) => s.updateSidebarTask);
  const renameActiveGroup = useAutomationChatStore((s) => s.renameActiveGroup);
  const toggleSidebarPinned = useAutomationChatStore((s) => s.toggleSidebarPinned);
  const toggleSidebarFavorite = useAutomationChatStore((s) => s.toggleSidebarFavorite);
  const updateAutomationTask = useAutomationStore((s) => s.updateTask);
  const setAutomationSelectedTaskId = useAutomationStore((s) => s.setSelectedTaskId);
  // ── Projects section data: project list + the currently open project (for highlighting) ──
  const projects = useProjectStore((s) => s.list);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  // Fetch the project list once auth is ready; refetch when switching accounts (user_id changes).
  const authUserId = authUser?.user_id;
  useEffect(() => {
    if (!authUserId) return;
    void useProjectStore.getState().fetchProjects();
  }, [authUserId]);

  const labEnabled = authUser?.lab_enabled !== false;
  const visibleSidebarItems = useMemo(() => {
    return sidebarLayoutKeys
      .map((key) => ({ key, meta: LAYOUT_ITEMS[key] }))
      .filter((x): x is { key: string; meta: typeof LAYOUT_ITEMS[string] } =>
        !!x.meta && (!x.meta.requiresLab || labEnabled));
  }, [sidebarLayoutKeys, labEnabled]);
  const visibleMenuItems = useMemo(() => {
    return menuLayoutKeys
      .map((key) => ({ key, meta: LAYOUT_ITEMS[key] }))
      .filter((x): x is { key: string; meta: typeof LAYOUT_ITEMS[string] } =>
        !!x.meta && (!x.meta.requiresLab || labEnabled));
  }, [menuLayoutKeys, labEnabled]);

  const historyListRef = useRef<HTMLDivElement | null>(null);
  const showScrollbar = () => historyListRef.current?.classList.add('show-scrollbar');
  const hideScrollbar = () => historyListRef.current?.classList.remove('show-scrollbar');

  // Group collapse state: all expanded by default; in-memory, reset on refresh.
  const [collapsedGroups, setCollapsedGroups] = useState<Record<HistoryGroupKey, boolean>>({
    projects: false,
    automation: false,
    history: false,
  });
  const toggleGroupCollapsed = (key: HistoryGroupKey) => {
    setCollapsedGroups((prev) => ({ ...prev, [key]: !prev[key] }));
  };
  // Per-project chat list collapse state (expanded by default); in-memory, reset on refresh.
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const toggleProjectCollapsed = (projectId: string) => {
    setCollapsedProjects((prev) => ({ ...prev, [projectId]: !prev[projectId] }));
  };

  const historyList = useMemo(
    () => buildSidebarChatItems(store, sidebarTasks, sidebarPrefs),
    [store, sidebarTasks, sidebarPrefs],
  );

  const startRenameItem = (item: ChatItem) => {
    if (item.automationRun) {
      setEditingChatId(item.id);
      setEditingTitle(item.title || t('自动化任务'));
      return;
    }
    onStartRename(item);
  };

  const commitRenameItem = async (item: ChatItem) => {
    if (!item.automationRun || !item.automationTaskId) {
      onCommitRename(item.id);
      return;
    }

    const nextTitle = editingTitle.trim() || t('自动化任务');
    setEditingChatId(null);
    setEditingTitle('');

    if (nextTitle === item.title) return;

    try {
      const updated = await updateAutomationTask(item.automationTaskId, { name: nextTitle });
      updateSidebarTask(updated);
      renameActiveGroup(item.automationTaskId, updated.name || nextTitle);
      message.success(t('自动化任务已重命名'));
    } catch (e) {
      message.error((e as Error)?.message || t('重命名失败'));
    }
  };

  const exportAutomationItem = async (item: ChatItem) => {
    if (!item.automationTaskId) return;
    try {
      const runs = await getAutomationRuns(item.automationTaskId, 50);
      const preferredRun = automationActiveGroup?.taskId === item.automationTaskId && selectedRunId
        ? runs.find((run) => run.run_id === selectedRunId && run.status !== 'running' && run.chat_id)
        : undefined;
      const fallbackRun = runs.find((run) => run.status !== 'running' && run.chat_id);
      const targetRun = preferredRun || fallbackRun;

      if (!targetRun?.chat_id) {
        message.warning(t('暂无可导出的执行记录'));
        return;
      }

      updateStore((prev) => {
        if (prev.chats[targetRun.chat_id!]) return prev;
        return {
          chats: {
            ...prev.chats,
            [targetRun.chat_id!]: {
              id: targetRun.chat_id!,
              title: item.title || t('自动化任务'),
              createdAt: item.createdAt,
              updatedAt: item.updatedAt,
              messages: [],
              automationRun: true,
              automationTaskId: item.automationTaskId,
            },
          },
          order: prev.order.includes(targetRun.chat_id!) ? prev.order : [targetRun.chat_id!, ...prev.order],
        };
      });
      addBackendSessionId(targetRun.chat_id);
      onExportChat(targetRun.chat_id);
    } catch (e) {
      message.error((e as Error)?.message || t('导出失败'));
    }
  };

  // Sort: pinned items first, the rest by updatedAt descending. Automation items use the same rule within their own group.
  const sortedHistoryList = useMemo(() => {
    return [...historyList].sort((a, b) => {
      const pinDiff = Number(!!b.pinned) - Number(!!a.pinned);
      if (pinDiff !== 0) return pinDiff;
      return (b.updatedAt || 0) - (a.updatedAt || 0);
    });
  }, [historyList]);

  const knownProjectIds = useMemo(
    () => new Set(projects.map((p) => p.project_id)),
    [projects],
  );

  /** Whether a chat is a "project orphan": its bound project is neither in the project list nor has a locally cached project name
   *  (typical case: the project was deleted, and the chat fetched back from the backend only has a project_id). Orphans fall back to History,
   *  avoiding a nameless fallback project group in the sidebar. */
  const isProjectOrphan = useCallback(
    (item: ChatItem) =>
      !!item.projectId && !knownProjectIds.has(item.projectId) && !item.projectName,
    [knownProjectIds],
  );

  // Grouping: Automation (shown only when there are tasks) + History (title always shown).
  // Chats belonging to a project don't go into the History group — they're nested under their respective projects in the "Projects" section above;
  // project orphans (see isProjectOrphan) are the exception and fall back to History.
  // Pinned items are no longer a separate group; per the sort above they appear at the top of the History group + rendered with 📌.
  const groupedHistoryList = useMemo(() => {
    const automationItems = sortedHistoryList.filter((item) => item.automationRun);
    const historyItems = sortedHistoryList.filter(
      (item) => !item.automationRun && (!item.projectId || isProjectOrphan(item)));
    const result: Array<{ key: HistoryGroupKey; label: string; items: ChatItem[] }> = [];
    if (automationItems.length > 0) {
      result.push({ key: 'automation', label: t('自动化'), items: automationItems });
    }
    result.push({ key: 'history', label: t('历史对话'), items: historyItems });
    return result;
  }, [sortedHistoryList, isProjectOrphan]);

  // "Projects" section: one group per project in the project list, with its owned chats attached (reusing the pinned+updated-time sort).
  // When the project list doesn't yet contain a projectId (not fetched / lost access) but a chat has cached a project name, use that name
  // to build a fallback group, so these chats don't vanish from the sidebar; orphans without even a name fall back to History.
  const projectGroups = useMemo<SidebarProjectGroup[]>(() => {
    const chatsByProject = new Map<string, ChatItem[]>();
    for (const item of sortedHistoryList) {
      if (item.automationRun || !item.projectId || isProjectOrphan(item)) continue;
      const arr = chatsByProject.get(item.projectId);
      if (arr) arr.push(item); else chatsByProject.set(item.projectId, [item]);
    }
    const groups: SidebarProjectGroup[] = projects.map((p) => {
      const items = chatsByProject.get(p.project_id) || [];
      return {
        projectId: p.project_id,
        name: p.name,
        pinned: !!p.pinned,
        known: true,
        items,
        lastActivity: Math.max(
          p.last_activity_at ? new Date(p.last_activity_at).getTime() : 0,
          ...items.map((i) => i.updatedAt || 0),
        ),
      };
    });
    for (const [pid, items] of chatsByProject) {
      if (knownProjectIds.has(pid)) continue;
      groups.push({
        projectId: pid,
        name: items.find((i) => i.projectName)?.projectName || t('项目'),
        pinned: false,
        known: false,
        items,
        lastActivity: Math.max(...items.map((i) => i.updatedAt || 0)),
      });
    }
    groups.sort((a, b) =>
      (Number(b.pinned) - Number(a.pinned)) || (b.lastActivity - a.lastActivity));
    return groups;
  }, [sortedHistoryList, projects, knownProjectIds, isProjectOrphan]);

  const historySkeletonGroups = [
    { key: 'history', label: t('历史对话'), rows: 8 },
  ];

  // A single chat item (shared between the projects section's nested list and the automation/history groups, ensuring identical interaction).
  // listLen controls the layout animation toggle (disable layout for very long lists to reduce reflow overhead).
  const renderChatItem = (item: ChatItem, listLen: number) => {
    const isAutomation = !!item.automationRun;
    const isActive = isAutomation
      ? (panel === 'chat' && automationActiveGroup?.taskId === item.automationTaskId)
      : (panel === 'chat' && item.id === currentChatId);
    const isEditing = editingChatId === item.id;

    const handleClick = async () => {
      if (editingChatId && editingChatId !== item.id) { setEditingChatId(null); setEditingTitle(''); }
      if (isAutomation && item.automationTaskId) {
        // Fetch runs then enter automation chat mode
        try {
          const runs = await getAutomationRuns(item.automationTaskId, 50);
          enterAutomationChat(item.automationTaskId, item.title, runs);
        } catch { /* ignore */ }
      } else {
        onSelectChat(item.id);
      }
    };

    return (
      <motion.div
        key={item.id}
        layout={listLen <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
        initial={{ opacity: 0, height: 0, minHeight: 0 }}
        animate={{ opacity: 1, height: 36, minHeight: 36 }}
        exit={{ opacity: 0, height: 0, minHeight: 0, transition: HISTORY_ITEM_EXIT }}
        transition={HISTORY_ITEM_ENTER}
        style={{ overflow: 'hidden' }}
        className={`jx-historyItem${isActive ? ' active' : ''}${isEditing ? ' editing' : ''}`}
        onClick={handleClick}>
        {isEditing ? (
          <Input size="small" value={editingTitle} autoFocus
            onChange={(e) => setEditingTitle(e.target.value)}
            onPressEnter={() => void commitRenameItem(item)}
            onBlur={() => void commitRenameItem(item)}
            onClick={(e) => e.stopPropagation()}
            maxLength={30} className="jx-historyEditInput" />
        ) : (
          <div className="jx-historyMain">
            {item.pinned ? (
              <Tooltip title={t('已置顶')}>
                <span className="jx-historyPinIcon" onClick={(e) => e.stopPropagation()}>
                  <PushpinFilled />
                </span>
              </Tooltip>
            ) : null}
            {isAutomation ? (
              <Tooltip title={t('自动化任务')}>
                <span className="jx-historyTypeIcon jx-historyTypeIcon--automation" style={{ fontSize: 13, color: '#faad14', flexShrink: 0 }}>&#9889;</span>
              </Tooltip>
            ) : item.agentName ? (
              <Tooltip title={item.agentName}>
                <img src="/home/new-icons/agent.svg" alt={t('子智能体')} className="jx-historyTypeIcon jx-historyTypeIcon--agent" style={{ width: 14, height: 14 }} />
              </Tooltip>
            ) : item.planChat ? (
              <Tooltip title={t('计划模式')}>
                <img src="/home/new-icons/plan.svg" alt={t('计划模式')} className="jx-historyTypeIcon jx-historyTypeIcon--plan" style={{ width: 14, height: 14 }} />
              </Tooltip>
            ) : null}
            <span className="jx-historyTitle">
              {item.title || t('对话')}
            </span>
            {sendingChatIds.has(item.id) && (
              <Tooltip title={t('运行中')}>
                <span className="jx-historyRunningDot" />
              </Tooltip>
            )}
            {!sendingChatIds.has(item.id) && ((pendingConfirm[item.id]?.length ?? 0) > 0 || !!pendingDesignPick[item.id]) && (
              <Tooltip title={t('有待确认的操作')}>
                <span className="jx-historyConfirmDot" />
              </Tooltip>
            )}
          </div>
        )}
        <div className="jx-historyActions">
          <Dropdown menu={{
            items: isAutomation
              ? [
                  { key: 'pin', label: item.pinned ? t('取消置顶') : t('置顶'), icon: item.pinned ? <PushpinFilled /> : <PushpinOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); if (item.automationTaskId) toggleSidebarPinned(item.automationTaskId); } },
                  { key: 'fav', label: item.favorite ? t('取消收藏') : t('收藏'), icon: item.favorite ? <StarFilled /> : <StarOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); if (item.automationTaskId) toggleSidebarFavorite(item.automationTaskId); } },
                  { key: 'rename', label: t('重命名'), icon: <EditOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); startRenameItem(item); } },
                  { key: 'export', label: t('导出'), icon: <ExportOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); void exportAutomationItem(item); } },
                ]
              : [
                  { key: 'pin', label: item.pinned ? t('取消置顶') : t('置顶'), icon: item.pinned ? <PushpinFilled /> : <PushpinOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onTogglePinned(item.id); } },
                  { key: 'fav', label: item.favorite ? t('取消收藏') : t('收藏'), icon: item.favorite ? <StarFilled /> : <StarOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onToggleFavorite(item.id); } },
                  { key: 'rename', label: t('重命名'), icon: <EditOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); startRenameItem(item); } },
                  { key: 'export', label: t('导出'), icon: <ExportOutlined />, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onExportChat(item.id); } },
                  { type: 'divider' as const },
                  { key: 'delete', label: t('删除'), icon: <DeleteOutlined />, danger: true, onClick: ({ domEvent }) => { domEvent.stopPropagation(); onDeleteChat(item.id); } },
                ],
          }} trigger={['click']} placement="bottomRight" overlayClassName="jx-chatItemMenu">
            <button aria-label={t('更多操作')} className="jx-historyMoreBtn" onClick={(e) => e.stopPropagation()}><EllipsisOutlined /></button>
          </Dropdown>
        </div>
      </motion.div>
    );
  };

  // Footer dropdown menus (shared between expanded + mini layouts)
  const footerSettingsMenu: MenuProps = {
    items: [
      ...visibleMenuItems.map(({ key, meta }) => ({
        key,
        label: meta.label,
        icon: key === 'my_space' && notifUnreadCount > 0 ? (
          <Badge count={notifUnreadCount} size="small" offset={[-2, 2]}>
            <img src={meta.icon} alt="" style={{ width: 16, height: 16 }} />
          </Badge>
        ) : (
          <img src={meta.icon} alt="" style={{ width: 16, height: 16 }} />
        ),
        onClick: () => {
          if (key === 'lab') setAutomationSelectedTaskId(null);
          onSetPanel(meta.targetPanel);
        },
      })),
      ...(!IS_COMMUNITY_EDITION_BUILD && (authUser?.can_system_config || authUser?.can_content_manage)
        ? [{ type: 'divider' as const }]
        : []),
      ...(!IS_COMMUNITY_EDITION_BUILD && authUser?.can_system_config ? [{
        key: 'system_config',
        label: t('系统配置'),
        icon: <img src="/home/settings.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => { window.location.href = '/config'; },
      }] : []),
      ...(!IS_COMMUNITY_EDITION_BUILD && authUser?.can_content_manage ? [{
        key: 'content_manage',
        label: t('内容管理'),
        icon: <img src="/home/knowledge.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => { window.location.href = '/admin'; },
      }] : []),
      { type: 'divider' as const },
      {
        key: 'logout',
        label: t('退出登录'),
        icon: <img src="/home/logout.svg" alt="" style={{ width: 16, height: 16 }} />,
        danger: true,
        onClick: () => {
          Modal.confirm({
            title: cfgLogoutTitle,
            icon: <ExclamationCircleFilled style={{ color: '#F8AB42' }} />,
            content: cfgLogoutContent,
            okText: cfgLogoutOk,
            cancelText: t('取消'),
            okButtonProps: { danger: true },
            onOk: () => void doLogout(),
          });
        },
      },
    ],
  };
  const helpMenu: MenuProps = {
    items: [
      ...(!IS_COMMUNITY_EDITION_BUILD ? [{
        key: 'docs',
        label: t('更新记录'),
        icon: <img src="/home/updates.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => onSetPanel('docs'),
      }] : []),
      {
        key: IS_COMMUNITY_EDITION_BUILD ? 'official_docs' : 'manual',
        label: t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '操作手册'),
        icon: <img src="/home/knowledge.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => window.open(HELP_DOCUMENTATION_URL, '_blank', 'noopener,noreferrer'),
      },
    ],
  };

  // ──────────────────────────── Mini rail (collapsed) ────────────────────────────
  if (siderCollapsed) {
    return (
      <Sider
        width={280}
        className="jx-sider jx-sider--mini"
        theme="light"
        collapsed
        collapsedWidth={56}
        style={{ overflow: 'hidden' }}
      >
        <div className="jx-miniRail">
          <div className="jx-miniRailTop">
            <Tooltip title={t('展开侧边栏并返回首页')} placement="right">
              <button
                type="button"
                className="jx-miniRailLogo"
                onClick={() => { setSiderCollapsed(false); onNewChat(); }}
                aria-label={t('展开侧边栏并返回首页')}
              >
                <img src={cfgLogoUrl} alt="" className="jx-miniRailLogoImg" />
              </button>
            </Tooltip>
            <Tooltip title={t('展开侧边栏')} placement="right">
              <button
                type="button"
                className="jx-miniRailBtn"
                onClick={() => setSiderCollapsed(false)}
                aria-label={t('展开侧边栏')}
              >
                <img src="/home/collapse.svg" alt="" className="jx-miniRailIcon" />
              </button>
            </Tooltip>
          </div>
          <div className="jx-miniRailDivider" />
          <div className="jx-miniRailGroup">
            <Tooltip title={cfgBtnNewChat} placement="right">
              <button
                type="button"
                className="jx-miniRailBtn jx-miniRailBtn--primary"
                onClick={onNewChat}
                aria-label={cfgBtnNewChat}
              >
                <img src="/home/new-chat.svg" alt="" className="jx-miniRailIcon jx-miniRailIcon--primary" />
              </button>
            </Tooltip>
            <Tooltip title={t('搜索对话 (⌘K)')} placement="right">
              <button
                type="button"
                className="jx-miniRailBtn"
                aria-label={t('搜索对话')}
                onClick={openSearchModal}
              >
                <img src="/home/search.svg" alt="" className="jx-miniRailIcon" />
              </button>
            </Tooltip>
          </div>
          {visibleSidebarItems.length > 0 && (
            <>
              <div className="jx-miniRailDivider" />
              <div className="jx-miniRailGroup jx-miniRailNav">
                {visibleSidebarItems.map(({ key, meta }) => {
                  const active = meta.activePanels?.includes(panel);
                  const iconEl = (
                    <img src={meta.icon} alt="" className="jx-miniRailIcon" />
                  );
                  return (
                    <Tooltip key={key} title={meta.label} placement="right">
                      <button
                        type="button"
                        className={`jx-miniRailBtn${active ? ' active' : ''}`}
                        aria-label={meta.label}
                        onClick={() => {
                          if (key === 'lab') setAutomationSelectedTaskId(null);
                          onSetPanel(meta.targetPanel);
                        }}
                      >
                        {key === 'my_space' && notifUnreadCount > 0 ? (
                          <Badge count={notifUnreadCount} size="small" offset={[-2, 2]}>{iconEl}</Badge>
                        ) : (
                          iconEl
                        )}
                      </button>
                    </Tooltip>
                  );
                })}
              </div>
            </>
          )}
          <div className="jx-miniRailSpacer" />
          <div className="jx-miniRailFooter">
            <Dropdown menu={helpMenu} trigger={['click']} placement="topRight" overlayClassName="jx-settingsMenu">
              <Tooltip title={t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '帮助 / 更新记录')} placement="right">
                <button type="button" className="jx-miniRailBtn" aria-label={t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '帮助')}>
                  <img src="/home/help.svg" alt="" className="jx-miniRailIcon" style={{ opacity: 0.55 }} />
                </button>
              </Tooltip>
            </Dropdown>
            <Dropdown menu={footerSettingsMenu} trigger={['click']} placement="topLeft" overlayClassName="jx-settingsMenu">
              <Tooltip title={authUser?.nickname || authUser?.real_name || authUser?.username || t('用户')} placement="right">
                <button type="button" className="jx-miniRailBtn jx-miniRailAvatarBtn" aria-label={t('用户菜单')}>
                  <img src={resolveAvatarUrl(authUser?.avatar_url)} alt="" className="jx-miniRailAvatar" />
                </button>
              </Tooltip>
            </Dropdown>
          </div>
        </div>
      </Sider>
    );
  }

  return (
    <Sider width={280} className="jx-sider" theme="light" collapsed={siderCollapsed} collapsedWidth={56}
      style={{ overflow: 'hidden' }}>
      <div className="jx-siderInner">
        {/* Logo row + collapse button */}
        <div className="jx-brandRow">
          <button type="button" className="jx-brandHomeBtn" onClick={onNewChat} title={t('回到首页')}>
            <div className="jx-logo"><img src={cfgLogoUrl} alt="" className="jx-logoImg" /></div>
            <div className="jx-brand-text">
              <div className="jx-brand-title">{cfgProductName}</div>
              {cfgProductSub && <div className="jx-brand-sub">{cfgProductSub}</div>}
            </div>
          </button>
          <button className="jx-searchBtn" onClick={openSearchModal} title={t('搜索对话 (⌘K)')}>
            <img src="/home/search.svg" alt="" style={{ width: 20, height: 20 }} />
          </button>
          <button className="jx-collapseBtn" onClick={() => setSiderCollapsed(true)} title={t('收起侧边栏')}>
            <img src="/home/expand.svg" alt="" style={{ width: 20, height: 20 }} />
          </button>
        </div>

        {/* New Chat button */}
        <button className="jx-newChatBtn" onClick={onNewChat}>
          <img src="/home/new-chat.svg" alt="" className="jx-newChatIcon" />
          <span>{cfgBtnNewChat}</span>
        </button>

        {/* Primary nav menu */}
        {visibleSidebarItems.length > 0 && (
          <div className="jx-navMenu">
            {visibleSidebarItems.map(({ key, meta }) => (
              <button
                key={key}
                className={`jx-navItem${meta.activePanels?.includes(panel) ? ' active' : ''}`}
                onClick={() => {
                  if (key === 'lab') setAutomationSelectedTaskId(null);
                  onSetPanel(meta.targetPanel);
                }}>
                {key === 'my_space' && notifUnreadCount > 0 ? (
                  <Badge count={notifUnreadCount} size="small" offset={[-2, 2]}>
                    <img src={meta.icon} alt="" className="jx-navItemIcon" />
                  </Badge>
                ) : (
                  <img src={meta.icon} alt="" className="jx-navItemIcon" />
                )}
                <span>{meta.label}</span>
              </button>
            ))}
          </div>
        )}

        {/* History list — the history section title and filter dropdown have been moved down into SearchModal */}
          <div className="jx-historyListWrap" ref={historyListRef} onMouseEnter={showScrollbar} onMouseLeave={hideScrollbar}>
            {chatsLoading ? (
              <div className="jx-historySkeletonList" aria-hidden="true">
                {historySkeletonGroups.map((group) => (
                  <div key={group.key} className="jx-historyGroup">
                    <div className="jx-historyGroupTitle">{group.label}</div>
                    <div className="jx-historyGroupList">
                      {Array.from({ length: group.rows }).map((_, index) => (
                        <div key={`${group.key}-${index}`} className="jx-historyItem jx-historyItemSkeleton">
                          <div className="jx-historyMain">
                            <div className="jx-skeletonBlock jx-historySkLine" />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ) : groupedHistoryList.length === 0 ? (
              <div className="jx-historyEmpty">{cfgEmptyState}</div>
            ) : (<>
              {/* ── Projects section: project row (click to open the project page) + the chats nested under it ── */}
              {projectGroups.length > 0 && (
                <div
                  className={`jx-historyGroup${collapsedGroups.projects ? ' jx-historyGroup--collapsed' : ''}`}
                  aria-label={t('项目')}
                >
                  <button
                    type="button"
                    className="jx-historyGroupHeader"
                    onClick={() => toggleGroupCollapsed('projects')}
                    aria-expanded={!collapsedGroups.projects}
                  >
                    <span className="jx-historyGroupTitle">{t('项目')}</span>
                    <CaretDownOutlined className="jx-historyGroupChevron" />
                  </button>
                  <div className={`jx-expandWrap jx-historyGroupExpand${collapsedGroups.projects ? '' : ' jx-expandWrap--open'}`}>
                    <div className="jx-historyGroupList">
                      {projectGroups.map((pg) => {
                        const projCollapsed = !!collapsedProjects[pg.projectId];
                        const projActive = panel === 'project_detail' && currentProjectId === pg.projectId;
                        return (
                          <div key={pg.projectId} className="jx-projectGroup">
                            <div
                              className={`jx-projectRow${projActive ? ' active' : ''}`}
                              role="button"
                              tabIndex={0}
                              title={pg.name}
                              onClick={() => {
                                if (!pg.known) return; // fallback groups (project no longer visible) can't open the detail page
                                void useProjectStore.getState().openProject(pg.projectId);
                                onSetPanel('project_detail');
                              }}
                              onKeyDown={(e) => {
                                if (e.key !== 'Enter' && e.key !== ' ') return;
                                e.preventDefault();
                                if (!pg.known) return;
                                void useProjectStore.getState().openProject(pg.projectId);
                                onSetPanel('project_detail');
                              }}
                            >
                              <FolderOutlined className="jx-projectRowIcon" />
                              <span className="jx-projectRowName">{pg.name}</span>
                              {pg.pinned && (
                                <Tooltip title={t('已置顶')}>
                                  <span className="jx-historyPinIcon" onClick={(e) => e.stopPropagation()}>
                                    <PushpinFilled />
                                  </span>
                                </Tooltip>
                              )}
                              {pg.items.length > 0 && (
                                <button
                                  type="button"
                                  className={`jx-projectRowChevron${projCollapsed ? ' collapsed' : ''}`}
                                  onClick={(e) => { e.stopPropagation(); toggleProjectCollapsed(pg.projectId); }}
                                  aria-label={projCollapsed ? t('展开项目会话') : t('收起项目会话')}
                                  aria-expanded={!projCollapsed}
                                >
                                  <CaretDownOutlined />
                                </button>
                              )}
                            </div>
                            {pg.items.length > 0 && (
                              <div className={`jx-expandWrap jx-historyGroupExpand${projCollapsed ? '' : ' jx-expandWrap--open'}`}>
                                <div className={`jx-historyGroupList jx-projectChatList${projCollapsed ? '' : ' jx-projectChatList--open'}`}>
                                  <AnimatePresence initial={false} mode="popLayout">
                                    {pg.items.map((item) => renderChatItem(item, pg.items.length))}
                                  </AnimatePresence>
                                </div>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}
              {groupedHistoryList.map((group) => {
                const isCollapsed = collapsedGroups[group.key];
                return (
                <div
                  key={group.key}
                  className={`jx-historyGroup${isCollapsed ? ' jx-historyGroup--collapsed' : ''}`}
                  aria-label={group.label}
                >
                  <button
                    type="button"
                    className="jx-historyGroupHeader"
                    onClick={() => toggleGroupCollapsed(group.key)}
                    aria-expanded={!isCollapsed}
                  >
                    <span className="jx-historyGroupTitle">{group.label}</span>
                    <CaretDownOutlined className="jx-historyGroupChevron" />
                  </button>
                  {/* Collapse animation: reuses the global jx-expandWrap (grid-rows 0fr→1fr);
                      once collapsed, CSS visibility removes tab focus as a fallback (see sidebar.css). */}
                  <div className={`jx-expandWrap jx-historyGroupExpand${isCollapsed ? '' : ' jx-expandWrap--open'}`}>
                    <div className="jx-historyGroupList">
                    <AnimatePresence initial={false} mode="popLayout">
                    {group.items.map((item) => renderChatItem(item, group.items.length))}
                    </AnimatePresence>
                    </div>
                  </div>
                </div>
                );
              })}
            </>)}
          </div>

        {/* Footer: user info + help button */}
        <div className="jx-sideFooter">
          <Dropdown menu={footerSettingsMenu} trigger={['click']} placement="topLeft" overlayClassName="jx-settingsMenu">
            <button className="jx-userInfoBtn">
              <img src={resolveAvatarUrl(authUser?.avatar_url)} alt="" className="jx-userAvatar" />
              <span className="jx-userName">{authUser?.nickname || authUser?.real_name || authUser?.username || t('用户')}</span>
            </button>
          </Dropdown>
          <Dropdown menu={helpMenu} trigger={['click']} placement="topRight" overlayClassName="jx-settingsMenu">
            <button className="jx-helpBtn" title={t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '帮助')}>
              <img src="/home/help.svg" alt="" style={{ width: 16, height: 16, opacity: 0.45 }} />
            </button>
          </Dropdown>
        </div>
      </div>
    </Sider>
  );
}
