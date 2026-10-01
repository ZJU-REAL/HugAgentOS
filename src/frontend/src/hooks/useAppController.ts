import {
  message
} from 'antd';
import 'highlight.js/styles/github.css';
import { useCallback, useEffect, useRef } from 'react';
import { listenForFolderProjects } from '../desktop/folderMenu';
import { t } from '../i18n';
import { isConversationPath, panelFromPath } from '../routing/navigation';
import { usePanel, useRouteProjectId } from '../routing/usePanel';
import { useProjectListBootstrap } from './useProjectListBootstrap';
import { useAppBootstrap } from './useAppBootstrap';
import { useChatRecovery } from './useChatRecovery';
import { useChatSearch } from './useChatSearch';
import { useChatViewport } from './useChatViewport';

/* styles loaded via styles/index.ts in main.tsx */
import type { SearchResultItem } from '../api';
import { useChatActions, useChatInit, useStreaming } from '../hooks';
import { usePageConfig, usePageConfigAll, usePageConfigPolling } from '../hooks/usePageConfig';
import {
  useAuthStore,
  useAutomationChatStore,
  useCanvasStore,
  useCatalogStore,
  useChatStore,
  useEditionStore,
  useUIStore
} from '../stores';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { useProjectStore } from '../stores/projectStore';
import type { PanelKey } from '../types';


export function useAppController() {
  usePageConfigPolling();
  const pageConfig = usePageConfigAll();
  const panelTitles = pageConfig.navigation.panel_titles;
  const brandName = usePageConfig('branding.product_name', 'HugAgentOS');
  const recommendBannerText = usePageConfig('texts.recommend_banner_text', '');
  const { authUser } = useAuthStore();
  const {
    detailModal, setDetailModal,
    recommendBarVisible, setRecommendBarVisible,
    promptHubOpen,
    siderCollapsed, setSiderCollapsed,
  } = useUIStore();
  const {
    store, currentChatId, setCurrentChatId,
    toolResultPanel, setToolResultPanel,
    backendSessionIds, loadedMsgIds,
  } = useChatStore();
  const panel = usePanel();
  const chatSurface = isConversationPath();
  const setCatalogPanel = useCatalogStore((s) => s.setPanel);
  const isDesktopShell = useDeploymentModeStore((s) => s.isDesktop);
  const desktopProvisionMode = useDeploymentModeStore((s) => s.provisionMode);
  const canvasOpen = useCanvasStore((s) => s.isOpen);
  const canvasPanelWidth = useCanvasStore((s) => s.panelWidth);
  const canvasFullscreen = useCanvasStore((s) => s.isFullscreen);
  const rightSidebarView = useCanvasStore((s) => s.activeView);
  const closeCanvas = useCanvasStore((s) => s.closeCanvas);
  const openRightSidebar = useCanvasStore((s) => s.openSidebar);
  const openOntologySidebar = useCanvasStore((s) => s.openOntology);
  const automationActiveGroup = useAutomationChatStore((s) => s.activeGroup);
  const exitAutomationChat = useAutomationChatStore((s) => s.exitAutomationChat);
  const isCE = useEditionStore((s) => s.edition === 'ce');

  const { closeMobileSidebar, openMobileSidebar, authUserId } = useAppBootstrap(chatSurface);

  const chat = store.chats[currentChatId];
  const latestOntologyMessage = [...(chat?.messages || [])]
    .reverse()
    .find((message) => Boolean(message.ontologyGovernance));
  const handleRightSidebarToggle = () => {
    if (canvasOpen) {
      closeCanvas();
      return;
    }
    if (rightSidebarView !== 'empty') {
      openRightSidebar();
      return;
    }
    if (latestOntologyMessage) {
      openOntologySidebar({ chatId: currentChatId, messageUid: latestOntologyMessage.uid });
      return;
    }
    openRightSidebar();
  };
  // Name of the project the current chat belongs to (for the "project name / title"
  // breadcrumb in the chat header). Prefer the projectName cached on the chat; sessions
  // fetched from the backend only carry projectId, so fall back to looking the name up
  // in the project list.
  const projectList = useProjectStore((s) => s.list);
  // 订阅而不是 getState()：刷新后项目 id 由 sessionStorage 恢复，读快照会漏掉这次更新。
  // 打开的是哪个项目由地址说了算，不从 store 再读一份
  const currentProjectId = useRouteProjectId();
  const chatProjectName = chat?.projectId
    ? (chat.projectName || projectList.find((p) => p.project_id === chat.projectId)?.name || '')
    : '';
  // Treat a chat as non-empty while its messages are still loading from the
  // backend (backendSessionIds has the ID but messages array is empty).
  // This prevents the homepage / recommend-banner from flashing when switching
  // between history items. Once the load completed (loadedMsgIds) an empty
  // chat is genuinely empty — show the normal empty state, not the skeleton.
  const isChatLoadingFromBackend = (!chat || chat.messages.length === 0)
    && backendSessionIds.has(currentChatId)
    && !loadedMsgIds.has(currentChatId);
  const isEmptyChat = (!chat || chat.messages.length === 0) && !isChatLoadingFromBackend;
  // ChatArea only mounts the scrollable list once a message exists; the scroll effects
  // below must re-run when this flips so they attach to the new DOM (e.g. entering an
  // automation run chat before its messages have loaded).
  const hasMessages = !!chat?.messages.length;

  const { inputRef, fileInputRef, chatListRef, messagesEndRef, handleContentRef, userScrolledUpRef } = useChatViewport(currentChatId, hasMessages, chatSurface);

  // ── Initialization hook (auth, sessions, catalog, etc.) ──
  const { effectiveApiUrl, refreshCatalog } = useChatInit();

  // ── Chat actions hook ──
  const {
    newChat, deleteChat,
    toggleChatPinned, toggleChatFavorite,
    startRenameChat, commitRenameChat,
    exportChatRecord,
    createChatShare,
    generateSummary, generateClassification,
    setPanelSafe,
  } = useChatActions(effectiveApiUrl);

  // ── Streaming hook ──
  const { send: rawSend, abort, activateQueuedMessage, discardQueuedMessage, handleFileSelect, removeFile, regenerate, editAndResend, resumeRunIfAny, cancelAndResumeBatch, continueLoop } = useStreaming(
    effectiveApiUrl, generateSummary, generateClassification,
  );

  // Codex-style stop shortcut: Escape only targets the chat currently visible
  // in this tab. Popup/menu handlers can preventDefault first and retain their
  // normal close behavior without accidentally cancelling the run.
  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
      if (!isConversationPath()) return;
      const state = useChatStore.getState();
      if (!state.sendingChatIds.has(state.currentChatId)) return;
      event.preventDefault();
      abort(state.currentChatId);
    };
    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [abort]);

  useProjectListBootstrap(authUser?.user_id);

  useChatRecovery(currentChatId, resumeRunIfAny);

  // A user-initiated send (Enter in the composer / clicking a follow-up question) is treated
  // as an explicit "take me to the bottom" intent: reset the scrolled-up flag so the
  // ResizeObserver below auto-scrolls to the bottom once the new message expands the list.
  const send = useCallback((text?: string) => {
    userScrolledUpRef.current = false;
    return rawSend(text);
  }, [rawSend, userScrolledUpRef]);

  // 编辑重发同样是显式"带我去底部"的意图（点击编辑区按钮会被下面的捕获监听
  // 预置为脱离跟随，这里在真正发送时复位，恢复流式跟随）。
  const editAndResendFollow = (messageIndex: number, newContent: string) => {
    userScrolledUpRef.current = false;
    return editAndResend(messageIndex, newContent);
  };

  // Cross-panel first message: the project-page composer stuffs the message into
  // chatStore.pendingFirstMessage; after jumping to the chat panel, this effect
  // auto-sends + clears it once currentChatId matches.
  const pendingFirstMessage = useChatStore((s) => s.pendingFirstMessage);
  const setPendingFirstMessage = useChatStore((s) => s.setPendingFirstMessage);
  useEffect(() => {
    if (!pendingFirstMessage) return;
    if (!chatSurface) return;
    if (pendingFirstMessage.chatId !== currentChatId) return;
    const content = pendingFirstMessage.content;
    // Clear pending first, then trigger send (avoids the effect re-firing in the same frame)
    setPendingFirstMessage(null);
    void send(content);
  }, [pendingFirstMessage, chatSurface, currentChatId, setPendingFirstMessage, send]);

  useChatSearch();

  // ── Sidebar handlers ──
  const handleSelectChat = (id: string) => {
    // Automation virtual entries are handled by Sidebar via automationChatStore
    // — but if the user clicks a *normal* chat while in automation mode, exit first.
    if (!id.startsWith('automation:') && automationActiveGroup) {
      exitAutomationChat();
    }
    setCurrentChatId(id);
    setToolResultPanel(null);
    closeMobileSidebar();
  };

  const handleSelectSearchResult = (item: SearchResultItem) => {
    if (automationActiveGroup) exitAutomationChat();
    useChatStore.getState().updateStore((prev) => {
      if (prev.chats[item.id]) return prev;
      return {
        chats: {
          ...prev.chats,
          [item.id]: {
            ...item,
            id: item.id,
            title: item.title || t('新对话'),
            createdAt: item.createdAt,
            updatedAt: item.updatedAt,
            messages: [],
            favorite: item.favorite,
            pinned: item.pinned,
            businessTopic: item.businessTopic || '综合咨询',
          },
        },
        order: [item.id, ...prev.order.filter((x) => x !== item.id)],
      };
    });
    setCurrentChatId(item.id);
    setToolResultPanel(null);
    closeMobileSidebar();
  };

  const handleSetPanel = (p: PanelKey, sub?: string) => {
    setPanelSafe(p, sub);
    closeMobileSidebar();
  };

  const handleNewChat = () => {
    newChat(inputRef);
    closeMobileSidebar();
  };

  const handleNewProjectChat = (projectId: string, projectName: string) => {
    newChat(inputRef);
    const chatId = useChatStore.getState().currentChatId;
    useChatStore.getState().bindChatProject(chatId, projectId, projectName);
    closeMobileSidebar();
  };

  const openFolderProjectRef = useRef(handleNewProjectChat);
  useEffect(() => { openFolderProjectRef.current = handleNewProjectChat; });
  useEffect(() => {
    if (!isDesktopShell || !authUserId ||
      !['dual', 'local_only'].includes(desktopProvisionMode)) return;
    const stop = listenForFolderProjects(window, (project) => {
      openFolderProjectRef.current(project.project_id, project.name);
      void useProjectStore.getState().fetchProjects();
    }, (error) => {
      message.error(t('新建本地项目失败') + '：' + (error instanceof Error ? error.message : String(error)));
    }, () => JSON.stringify([
      useChatStore.getState().currentChatId,
      panelFromPath(),
      useProjectStore.getState().currentProjectId,
    ]));
    const pending = sessionStorage.getItem('hugagent:pending-project-folder');
    if (pending) {
      sessionStorage.removeItem('hugagent:pending-project-folder');
      window.dispatchEvent(new CustomEvent('hugagent:open-project-folder', { detail: pending, cancelable: true }));
    }
    return stop;
  }, [isDesktopShell, desktopProvisionMode, authUserId]);

  const handleCapabilityClick = (capabilityId: string) => {
    // 知识库已并入「我的空间」的 Tab，首页快捷入口直接落到那个 Tab
    if (capabilityId === 'knowledge') setPanelSafe('my_space', 'kb');
  };

  // ── Derived header text (for non-chat panels) ──
  const title = panelTitles[panel as string] || brandName;
  const panelSubtitles = pageConfig.navigation.panel_subtitles;
  const hint = panelSubtitles[panel as string] ?? '';

  // 帮助页使用通栏标题，其余模块自带页头。
  // 写成正面枚举而不是逐个 `panel !== 'x'` 的否定链：新增面板默认不显示，不必回来补一行。
  const showHeader = panel === 'docs';
  const showChatHeader = chatSurface && !isEmptyChat;
  return {
    handleNewChat,
    handleNewProjectChat,
    deleteChat,
    toggleChatPinned,
    toggleChatFavorite,
    startRenameChat,
    commitRenameChat,
    exportChatRecord,
    handleSelectChat,
    handleSetPanel,
    siderCollapsed,
    setSiderCollapsed,
    handleSelectSearchResult,
    canvasFullscreen,
    canvasPanelWidth,
    canvasOpen,
    chatSurface,
    showChatHeader,
    openMobileSidebar,
    isEmptyChat,
    handleRightSidebarToggle,
    showHeader,
    title,
    hint,
    chat,
    chatProjectName,
    recommendBarVisible,
    recommendBannerText,
    handleCapabilityClick,
    setRecommendBarVisible,
    handleContentRef,
    panel,
    send,
    abort,
    activateQueuedMessage,
    discardQueuedMessage,
    continueLoop,
    createChatShare,
    handleFileSelect,
    removeFile,
    regenerate,
    editAndResendFollow,
    inputRef,
    fileInputRef,
    chatListRef,
    messagesEndRef,
    currentProjectId,
    setCatalogPanel,
    toolResultPanel,
    promptHubOpen,
    isCE,
    automationActiveGroup,
    rightSidebarView,
    detailModal,
    setDetailModal,
    refreshCatalog,
    cancelAndResumeBatch
  };
}
