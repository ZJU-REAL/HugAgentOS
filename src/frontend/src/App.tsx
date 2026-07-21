import { useEffect, useRef, type ReactNode } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  Layout, Button, Typography, Tag, Modal,
} from 'antd';
import {
  CloseOutlined,
} from '@ant-design/icons';
import 'highlight.js/styles/github.css';
import { t } from './i18n';

/* styles loaded via styles/index.ts in main.tsx */
import type { PanelKey } from './types';
import { listSidebarAutomations, getPendingConfirm, listPendingConfirms, searchSessions } from './api';
import type { SearchResultItem } from './api';
import { TOPIC_TAG_COLORS } from './utils/constants';
import { SCROLL_FOLLOW_THRESHOLD, distanceFromBottom, scrollElementToBottom } from './utils/scroll';
import { EASE, SLIDE_EASE } from './utils/motionTokens';
import { CollapseHeight } from './components/common/CollapseHeight';
import { Sidebar, SearchModal } from './components/sidebar';
import { ChatArea, PromptHubPanel } from './components/chat';
import { ToolResultPanel } from './components/tool';
import { CatalogPanel, AbilityCenterPage, SkillsPage, McpPage } from './components/catalog';
import { AgentPanel } from './components/agent';
import { DocsPanel, AppCenterPanel } from './components/docs';
import LabPanel from './components/lab/LabPanel';
import { MySpacePanel } from './components/myspace';
import { ProjectsPanel, ProjectDetailPanel } from './components/projects';
import { useProjectStore } from './stores/projectStore';
import { CanvasPanel } from './components/canvas';
import { ImagePreview, AuthExpiredModal, AppLoadingSkeleton } from './components/common';
import { PasswordManagementPanel, SettingsPage } from './components/settings';
import { FirstRunSetup } from './components/onboarding';
import { CreateKBModal, ReindexModal } from './components/kb';
import { BatchConfirmModal } from './components/batch';
import {
  useUIStore, useChatStore, useCatalogStore, useCanvasStore, useAuthStore,
  useAutomationChatStore, useModelCapabilitiesStore, useEditionStore,
} from './stores';
import type { ChatMode } from './stores/chatStore';
import { RunTimelinePanel } from './components/automation/RunTimelinePanel';
import { useChatInit, useChatActions, useStreaming, useDelayedFlag } from './hooks';
import { usePageConfig, usePageConfigAll, usePageConfigPolling } from './hooks/usePageConfig';
import { usePageConfigStore } from './stores/pageConfigStore';
import { useMySpaceStore } from './stores/mySpaceStore';

const { Header, Content } = Layout;

function SlidePanel({ show, panelKey, children, x = 24, duration = 0.25 }: {
  show: boolean; panelKey: string; children: ReactNode; x?: number; duration?: number;
}) {
  return (
    <AnimatePresence>
      {show && (
        <motion.div
          key={panelKey}
          initial={{ opacity: 0, x }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x, transition: { duration: duration * 0.7, ease: EASE.exit } }}
          transition={{ duration, ease: SLIDE_EASE }}
          /* display:contents cannot be used — it generates no box, so opacity/transform
           * all stop working. This participates in the .jx-mainRow layout as a real flex
           * child; width is determined by the inner panel itself. */
          style={{ display: 'flex', flex: 'none', height: '100%', minWidth: 0 }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export default function App() {
  usePageConfigPolling();
  const pageConfig = usePageConfigAll();
  const panelTitles = pageConfig.navigation.panel_titles;
  const brandName = usePageConfig('branding.product_name', 'HugAgentOS');
  const recommendBannerText = usePageConfig('texts.recommend_banner_text', '');
  const { authUser, authChecking, setAuthUser } = useAuthStore();
  const {
    searchKeyword, setSearchResults, setSearchLoading,
    openSearchModal,
    detailModal, setDetailModal,
    recommendBarVisible, setRecommendBarVisible,
    promptHubOpen,
  } = useUIStore();
  const {
    store, currentChatId, setCurrentChatId,
    toolResultPanel, setToolResultPanel,
    backendSessionIds, loadedMsgIds,
  } = useChatStore();
  const { panel } = useCatalogStore();
  const setCatalogPanel = useCatalogStore((s) => s.setPanel);
  const setMySpaceTab = useMySpaceStore((s) => s.setTab);
  const canvasOpen = useCanvasStore((s) => s.isOpen);
  const closeCanvas = useCanvasStore((s) => s.closeCanvas);
  const automationActiveGroup = useAutomationChatStore((s) => s.activeGroup);
  const exitAutomationChat = useAutomationChatStore((s) => s.exitAutomationChat);
  const isCE = useEditionStore((s) => s.edition === 'ce');

  useEffect(() => {
    if (panel === 'share_records') {
      setMySpaceTab('shares');
      setCatalogPanel('my_space');
    }
  }, [panel, setCatalogPanel, setMySpaceTab]);

  // Dynamically apply page title + favicon from config
  useEffect(() => {
    const pt = pageConfig.branding.page_title;
    if (pt && typeof document !== 'undefined') document.title = pt;
  }, [pageConfig.branding.page_title]);

  useEffect(() => {
    const fav = pageConfig.branding.favicon_url;
    if (!fav || typeof document === 'undefined') return;
    let link = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    if (link.href !== fav) link.href = fav;
  }, [pageConfig.branding.favicon_url]);

  // Once pageConfig finishes its first load, sync chatStore.chatMode to the admin-side
  // "default chat mode". Runs only once, when loaded first flips, so remote config changes
  // during the subsequent 15s polling never override the user's manual switch.
  const pageConfigLoaded = usePageConfigStore((s) => s.loaded);
  const setChatMode = useChatStore((s) => s.setChatMode);
  const defaultChatModeApplied = useRef(false);
  useEffect(() => {
    if (!pageConfigLoaded || defaultChatModeApplied.current) return;
    defaultChatModeApplied.current = true;
    const VALID: readonly ChatMode[] = ['fast', 'medium', 'high', 'max'];
    const raw = pageConfig.defaults?.chat_mode as string | undefined;
    const next: ChatMode = (raw && (VALID as readonly string[]).includes(raw))
      ? (raw as ChatMode)
      : (pageConfig.defaults?.thinking_mode ? 'medium' : 'fast');
    setChatMode(next);
  }, [pageConfigLoaded, pageConfig.defaults?.chat_mode, pageConfig.defaults?.thinking_mode, setChatMode]);

  // Fetch main-model capabilities at startup (decides whether the dropdown shows "Thinking: high/max")
  const fetchCapabilities = useModelCapabilitiesStore((s) => s.fetchCapabilities);
  const authUserId = authUser?.user_id || '';
  useEffect(() => {
    if (authChecking) return;
    void fetchCapabilities();
  }, [fetchCapabilities, authChecking, authUserId]);

  // Fetch edition / license capability bits at startup (CE hides EE entries such as Teams)
  const fetchEdition = useEditionStore((s) => s.fetchEdition);
  useEffect(() => {
    void fetchEdition();
  }, [fetchEdition]);

  // ── Notification polling (60s) — updates the sidebar badge on My Space ──
  // Also refreshes the list of sidebar-activated automation tasks, so users don't have to
  // hit F5 to see newly completed tasks appear in the "Automation" group.
  const fetchNotifCount = useMySpaceStore((s) => s.fetchNotifications);
  const setSidebarTasks = useAutomationChatStore((s) => s.setSidebarTasks);
  useEffect(() => {
    if (!authUser) return;
    const refreshSidebarAutomations = async () => {
      try {
        const tasks = await listSidebarAutomations();
        setSidebarTasks(tasks);
      } catch { /* ignore — shares the heartbeat with notifications; retry next round on failure */ }
    };
    // Initial fetch
    void fetchNotifCount();
    void refreshSidebarAutomations();
    const timer = setInterval(() => {
      void fetchNotifCount();
      void refreshSidebarAutomations();
    }, 60_000);
    return () => clearInterval(timer);
  }, [authUser, fetchNotifCount, setSidebarTasks]);

  // Close canvas when panel or chat changes
  useEffect(() => {
    closeCanvas();
  }, [panel, currentChatId, closeCanvas]);

  // Sync the "composer context" when switching the main view panel: a site session's plugin
  // reference is kept only on the chat panel; switching to the project page/any other page
  // always clears it (fixes the site plugin-reference leak). Leaving the chat panel also
  // turns off autonomous-loop mode.
  useEffect(() => {
    useChatStore.getState().syncComposerForPanel(panel);
  }, [panel]);

  // Global ⌘K / Ctrl+K → open the search modal.
  // Defenses: skip while IME is composing, on key auto-repeat, when focus is inside
  // contenteditable / a code editor (editors like Monaco use ⌘K themselves), and while the
  // sidebar is inline-renaming (prevents ⌘K stealing focus so onBlur mistakenly saves a
  // half-deleted title).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key !== 'k' && e.key !== 'K') return;
      if (e.repeat) return;
      if (e.isComposing || e.keyCode === 229) return;

      const target = e.target as HTMLElement | null;
      // Embedded rich-text/code editors (Monaco, CodeMirror, TipTap, etc.) usually use contenteditable
      if (target?.isContentEditable) return;
      // Don't steal focus while the sidebar is renaming (the rename Input's onBlur commits the current edit, which may be an empty string)
      if (useUIStore.getState().editingChatId) return;

      e.preventDefault();
      openSearchModal();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openSearchModal]);

  const chat = store.chats[currentChatId];
  // Name of the project the current chat belongs to (for the "project name / title"
  // breadcrumb in the chat header). Prefer the projectName cached on the chat; sessions
  // fetched from the backend only carry projectId, so fall back to looking the name up
  // in the project list.
  const projectList = useProjectStore((s) => s.list);
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

  // ── Refs ──
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const chatListRef = useRef<HTMLDivElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const userScrolledUpRef = useRef(false);
  // The smooth animation fires a scroll event on every frame; the listener must be muted
  // during it, otherwise mid-animation states get misread as "user scrolled up".
  const isAutoScrollingRef = useRef(false);

  // ── Initialization hook (auth, sessions, catalog, etc.) ──
  const { effectiveApiUrl, refreshCatalog, searchTimerRef } = useChatInit();

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
  const { send: rawSend, abort, handleFileSelect, removeFile, regenerate, editAndResend, resumeRunIfAny, cancelAndResumeBatch, continueLoop } = useStreaming(
    effectiveApiUrl, generateSummary, generateClassification,
  );

  // ── Fetch the project list once after login: used to resolve names for the chat-header
  //    "project name / title" breadcrumb (sessions from the backend only carry projectId;
  //    the project list is needed to look up the name) ──
  useEffect(() => {
    if (!authUser) return;
    void useProjectStore.getState().fetchProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authUser?.user_id]);

  // ── Resume: when switching/refreshing into a chat, re-subscribe if the backend still has a run in progress ──
  useEffect(() => {
    if (!authUser || !currentChatId) return;
    // Leave a short window for the message list to load, avoiding a race (resumeRunIfAny re-checks sendingChatIds internally)
    const timer = window.setTimeout(() => { resumeRunIfAny(currentChatId); }, 300);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentChatId, authUser?.user_id]);

  // ── §13: when switching/refreshing into a chat, restore "pending-confirm" write-op bars from the backend registry ──
  // pendingConfirm lives only in the in-memory uiStore and is lost on refresh; the backend
  // _myspace_confirm registry is the authority on whether the chat still has pending items —
  // restore from it (or clear ones that are no longer valid).
  useEffect(() => {
    if (!authUser || !currentChatId) return;
    let cancelled = false;
    const chatId = currentChatId;
    getPendingConfirm(chatId)
      .then(({ confirms, designPick }) => {
        if (cancelled) return;
        useUIStore.getState().hydratePendingConfirmQueue(chatId, confirms);
        // Site-builder pick-one-of-three designs: the backend is the authority — restore the pick card if present, clear stale ones if not.
        useUIStore.getState().setPendingDesignPick(chatId, designPick);
      })
      .catch(() => { /* silent: if unavailable, keep the current state */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentChatId, authUser?.user_id]);

  // ── §13: light up sidebar blue dots in one pass after first paint/refresh (no need to open each chat) ──
  useEffect(() => {
    if (!authUser) return;
    listPendingConfirms()
      .then(({ confirms, designPicks }) => {
        const ui = useUIStore.getState();
        ui.hydratePendingConfirms(confirms);
        // design_pick uses its own single slot (the Sidebar blue dot reads it too); not mixed into the write-confirm queue
        for (const { chatId, info } of designPicks) ui.setPendingDesignPick(chatId, info);
      })
      .catch(() => { /* silent */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authUser?.user_id]);

  // A user-initiated send (Enter in the composer / clicking a follow-up question) is treated
  // as an explicit "take me to the bottom" intent: reset the scrolled-up flag so the
  // ResizeObserver below auto-scrolls to the bottom once the new message expands the list.
  const send = (text?: string) => {
    userScrolledUpRef.current = false;
    return rawSend(text);
  };

  // Cross-panel first message: the project-page composer stuffs the message into
  // chatStore.pendingFirstMessage; after jumping to the chat panel, this effect
  // auto-sends + clears it once currentChatId matches.
  const pendingFirstMessage = useChatStore((s) => s.pendingFirstMessage);
  const setPendingFirstMessage = useChatStore((s) => s.setPendingFirstMessage);
  useEffect(() => {
    if (!pendingFirstMessage) return;
    if (panel !== 'chat') return;
    if (pendingFirstMessage.chatId !== currentChatId) return;
    const content = pendingFirstMessage.content;
    // Clear pending first, then trigger send (avoids the effect re-firing in the same frame)
    setPendingFirstMessage(null);
    void send(content);
  }, [pendingFirstMessage, panel, currentChatId, setPendingFirstMessage, send]);

  // Track whether the user has scrolled up on purpose
  useEffect(() => {
    const content = document.querySelector<HTMLElement>('.jx-content');
    if (!content) return;
    const handleScroll = () => {
      if (isAutoScrollingRef.current) return;
      userScrolledUpRef.current = distanceFromBottom(content) > SCROLL_FOLLOW_THRESHOLD;
    };
    content.addEventListener('scroll', handleScroll, { passive: true });
    return () => content.removeEventListener('scroll', handleScroll);
  }, []);

  // Chat switch: reset follow state and smooth-scroll to the bottom (keeping the
  // "pulled down from the top" visual). Height growth from follow-up/action-bar animations
  // after reaching the bottom is covered by the ResizeObserver below.
  // hasMessages as a dependency: entering a chat whose messages haven't been fetched yet,
  // the first render has scrollHeight===clientHeight so the smooth scroll is a no-op;
  // once messages load asynchronously this effect runs again, ensuring we truly land at the bottom.
  useEffect(() => {
    userScrolledUpRef.current = false;
    const content = document.querySelector<HTMLElement>('.jx-content');
    if (!content) return;
    isAutoScrollingRef.current = true;
    const raf = requestAnimationFrame(() => scrollElementToBottom(content, true));
    const release = () => { isAutoScrollingRef.current = false; };
    // scrollend is a modern-browser event (Chrome 114+/Firefox 109+/Safari 17+);
    // for older browsers a single setTimeout serves as the safety net.
    content.addEventListener('scrollend', release, { once: true });
    const fallback = window.setTimeout(release, 1000);
    return () => {
      cancelAnimationFrame(raf);
      content.removeEventListener('scrollend', release);
      window.clearTimeout(fallback);
    };
  }, [currentChatId, hasMessages]);

  // Observe chat-list size changes: when streaming chunks or the framer-motion animations
  // of the follow-up/action bar grow the height, snap-align to the bottom as long as the
  // user hasn't scrolled up. Compared to multi-stage setTimeout fallbacks, this is driven
  // by "content actually changed" — no magic time numbers, and no pending setTimeouts
  // piling up while idle.
  // hasMessages as a dependency: the .jx-chatList that chatListRef points to only mounts
  // when messages exist; when the list goes from none to some we must re-observe the new node.
  useEffect(() => {
    if (panel !== 'chat' || !hasMessages) return;
    const content = document.querySelector<HTMLElement>('.jx-content');
    const list = chatListRef.current;
    if (!content || !list || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (userScrolledUpRef.current || isAutoScrollingRef.current) return;
      content.scrollTop = content.scrollHeight;
    });
    ro.observe(list);
    return () => ro.disconnect();
  }, [panel, currentChatId, hasMessages]);

  // Expanding a history plan card's step details, or expanding a tool-call card to view its
  // output, grows the DOM — the ResizeObserver above would then yank the viewport to the
  // bottom, pushing the content the user just expanded off screen. Here we intercept clicks
  // in the capture phase: whenever the user clicks the expand/collapse control of a plan
  // card or tool-call card, pre-mark userScrolledUpRef=true so the subsequent resize event
  // skips auto-scroll. Scrolling back to the bottom or sending another message naturally
  // resets this flag, leaving later streaming follow unaffected.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      if (target.closest('.jx-plan-stepHeader, .jx-plan-stepsToggle, .jx-tcr-header, .jx-trs-head')) {
        userScrolledUpRef.current = true;
      }
    };
    document.addEventListener('click', handler, { capture: true });
    return () => document.removeEventListener('click', handler, { capture: true } as EventListenerOptions);
  }, []);

  // ── Search debounce ──
  // Use searchSessions (which fully resolves mode fields like agentName/planChat via
  // toChatItem); otherwise SearchModal's _mode:* type filters would all fail on search hits.
  // The cancelled flag prevents a late fetch from backfilling stale results after the user
  // clears/closes the search.
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const kw = searchKeyword.trim();
    if (!kw) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }
    setSearchLoading(true);
    let cancelled = false;
    searchTimerRef.current = setTimeout(async () => {
      try {
        const { items } = await searchSessions(kw, 1, 50);
        if (cancelled) return;
        setSearchResults(items);
      } catch {
        if (!cancelled) setSearchResults([]);
      } finally {
        if (!cancelled) setSearchLoading(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [searchKeyword]);

  // ── Sidebar handlers ──
  const handleSelectChat = (id: string) => {
    // Automation virtual entries are handled by Sidebar via automationChatStore
    // — but if the user clicks a *normal* chat while in automation mode, exit first.
    if (!id.startsWith('automation:') && automationActiveGroup) {
      exitAutomationChat();
    }
    setPanelSafe('chat');
    setCurrentChatId(id);
    setToolResultPanel(null);
  };

  const handleSelectSearchResult = (item: SearchResultItem) => {
    if (automationActiveGroup) exitAutomationChat();
    useChatStore.getState().updateStore((prev) => {
      if (prev.chats[item.id]) return prev;
      return {
        chats: {
          ...prev.chats,
          [item.id]: {
            id: item.id,
            title: item.title || t('新对话'),
            createdAt: item.createdAt,
            updatedAt: item.updatedAt,
            messages: [],
            favorite: item.favorite,
            pinned: item.pinned,
            businessTopic: (item as any).businessTopic || '综合咨询',
          },
        },
        order: [item.id, ...prev.order.filter((x) => x !== item.id)],
      };
    });
    setPanelSafe('chat');
    setCurrentChatId(item.id);
    setToolResultPanel(null);
  };

  const handleSetPanel = (p: PanelKey) => setPanelSafe(p);

  const handleCapabilityClick = (capabilityId: string) => {
    if (capabilityId === 'knowledge') setPanelSafe('kb');
  };

  // ── Derived header text (for non-chat panels) ──
  const title = panelTitles[panel as string] || brandName;
  const panelSubtitles = pageConfig.navigation.panel_subtitles;
  const hint = panelSubtitles[panel as string] ?? '';

  // Whether to show the header: only for non-chat panels, or chat panels with messages
  const showHeader = panel !== 'chat'
    && panel !== 'settings'
    && panel !== 'skills'
    && panel !== 'mcp'
    && panel !== 'agents'
    && panel !== 'my_space'
    && panel !== 'ability_center'
    && panel !== 'app_center'
    && panel !== 'projects'
    && panel !== 'project_detail'
    && panel !== 'kb'
    && panel !== 'lab';
  const showChatHeader = panel === 'chat' && !isEmptyChat;
  const showAuthSkeleton = useDelayedFlag(authChecking);

  if (authChecking) {
    return showAuthSkeleton ? <AppLoadingSkeleton /> : null;
  }

  if (!authUser || window.location.pathname.startsWith('/mock-sso/login')) return null;

  if (authUser.must_change_password) {
    return (
      <Modal
        open
        title={t('修改默认密码')}
        footer={null}
        closable={false}
        maskClosable={false}
        keyboard={false}
        width={480}
      >
        <PasswordManagementPanel forced />
      </Modal>
    );
  }

  if (authUser.onboarding_required) {
    return (
      <FirstRunSetup
        user={authUser}
        onComplete={() => setAuthUser({ ...authUser, onboarding_required: false })}
      />
    );
  }

  return (
    <Layout style={{ height: '100%' }}>
      <Sidebar
        onNewChat={() => newChat(inputRef)}
        onDeleteChat={deleteChat}
        onTogglePinned={toggleChatPinned}
        onToggleFavorite={toggleChatFavorite}
        onStartRename={startRenameChat}
        onCommitRename={commitRenameChat}
        onExportChat={(id) => void exportChatRecord(id)}
        onSelectChat={handleSelectChat}
        onSetPanel={handleSetPanel}
      />
      {/* Global search modal: triggered by the search button / ⌘K / Ctrl+K */}
      <SearchModal
        onNewChat={() => newChat(inputRef)}
        onSelectChat={handleSelectChat}
        onSelectSearchResult={handleSelectSearchResult}
      />

      <Layout style={{ overflow: 'hidden', background: '#ffffff' }}>
        {/* Non-chat panels: standard header */}
        {showHeader && (
          <Header className="jx-topbar" style={{ paddingInline: 20, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0, flex: 1 }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <Typography.Title level={5} style={{ margin: 0, fontWeight: 900 }} ellipsis>{title}</Typography.Title>
                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 2 }} ellipsis>{hint}</Typography.Text>
              </div>
            </div>
          </Header>
        )}

        {/* Chat panel with messages: minimal header with title */}
        {showChatHeader && (
          <div className="jx-chatTopbar">
            {chat?.projectId && (
              <span
                className="jx-chatTopbarProject"
                title={`${t('项目：')}${chatProjectName || t('项目')}`}
                onClick={() => {
                  useProjectStore.getState().openProject(chat.projectId!);
                  setCatalogPanel('project_detail');
                }}
              >
                {chatProjectName || t('项目')}
                <span className="jx-chatTopbarProjectSep">/</span>
              </span>
            )}
            <span className="jx-chatTopbarTitle">{chat?.title || t('对话')}</span>
            {chat?.agentName && (
              <Tag className="jx-headerTopicTag" color="blue">{chat.agentName}</Tag>
            )}
            {(chat as any)?.planChat && (
              <Tag className="jx-headerTopicTag" color="blue">{t('计划模式')}</Tag>
            )}
            {chat?.businessTopic && (
              <Tag className="jx-headerTopicTag" color={TOPIC_TAG_COLORS[chat.businessTopic] || 'default'}>{chat.businessTopic}</Tag>
            )}
          </div>
        )}

        {/* Chat empty state: closable recommend banner (full-width); on close the height collapses so the content below moves up smoothly */}
        <CollapseHeight
          show={panel === 'chat' && isEmptyChat && recommendBarVisible}
          motionKey="recommend-banner"
          duration={0.2}
          style={{ flex: 'none' }}
        >
              <div className="jx-recommendBanner">
                <span className="jx-recommendBanner-icon">💡</span>
                <span className="jx-recommendBanner-text">
                  {recommendBannerText.trim() || t('推荐用法：优先使用知识库检索可提升可引用性与结果可靠性。')}
                  <a className="jx-recommendBanner-link" onClick={() => setPanelSafe('kb')}>{t('前往知识库 >')}</a>
                </span>
                <button className="jx-recommendBanner-close" onClick={() => setRecommendBarVisible(false)} aria-label={t('关闭')}>
                  <CloseOutlined style={{ fontSize: 16 }} />
                </button>
              </div>
        </CollapseHeight>


        <div className="jx-mainRow">
          <Content className="jx-content">
            {/* Unified panel-switch entrance (fade+rise, enter-only to stay responsive); key=panel:
              * switching chats within the chat panel does not replay it. One-way entrance
              * needs no motion — CSS primitives suffice. */}
            <div
              key={panel}
              className="jx-panel jx-anim-fadeInUp"
              style={{ '--fadeInUp-distance': '6px', animationDuration: '180ms' } as React.CSSProperties}
            >
              {panel === 'chat' && (
                <ChatArea
                  send={send}
                  abort={abort}
                  continueLoop={continueLoop}
                  exportChatRecord={exportChatRecord}
                  createChatShare={createChatShare}
                  onCapabilityClick={handleCapabilityClick}
                  handleFileSelect={handleFileSelect}
                  removeFile={removeFile}
                  regenerate={regenerate}
                  editAndResend={editAndResend}
                  inputRef={inputRef}
                  fileInputRef={fileInputRef}
                  chatListRef={chatListRef}
                  messagesEndRef={messagesEndRef}
                />
              )}
              {panel === 'ability_center' && <AbilityCenterPage />}
              {panel === 'skills' && <SkillsPage />}
              {panel === 'mcp' && <McpPage />}
              {panel === 'agents' && <AgentPanel />}
              {panel !== 'chat' && panel !== 'docs' && panel !== 'app_center' && panel !== 'lab' && panel !== 'settings' && panel !== 'skills' && panel !== 'mcp' && panel !== 'agents' && panel !== 'share_records' && panel !== 'my_space' && panel !== 'ability_center' && panel !== 'projects' && panel !== 'project_detail' && <CatalogPanel />}
              {panel === 'docs' && <DocsPanel />}
              {panel === 'app_center' && <AppCenterPanel />}
              {panel === 'lab' && <LabPanel />}
              {panel === 'settings' && <SettingsPage />}
              {panel === 'my_space' && <MySpacePanel />}
              {panel === 'projects' && <ProjectsPanel onOpenProject={(pid) => { useProjectStore.getState().openProject(pid); setCatalogPanel('project_detail'); }} />}
              {panel === 'project_detail' && useProjectStore.getState().currentProjectId && (
                <ProjectDetailPanel
                  projectId={useProjectStore.getState().currentProjectId!}
                  onBack={() => setCatalogPanel('projects')}
                  handleFileSelect={handleFileSelect}
                  removeFile={removeFile}
                />
              )}
            </div>
          </Content>

          <SlidePanel show={!!toolResultPanel && !promptHubOpen && !canvasOpen && panel === 'chat'} panelKey="tool-result-panel" x={20} duration={0.22}>
            <ToolResultPanel />
          </SlidePanel>
          <SlidePanel show={!isCE && promptHubOpen && !canvasOpen && (panel === 'chat' || panel === 'project_detail')} panelKey="prompt-hub">
            <PromptHubPanel />
          </SlidePanel>
          <SlidePanel show={canvasOpen} panelKey="canvas" x={30} duration={0.28}>
            <CanvasPanel />
          </SlidePanel>

          {/* Automation run timeline — persistent panel (not mutually exclusive with SlidePanels).
            * During exit store.activeGroup is already null; RunTimelinePanel falls back to a
            * snapshot internally to render the last frame. */}
          <SlidePanel show={!!automationActiveGroup && panel === 'chat'} panelKey="run-timeline" x={24} duration={0.24}>
            <RunTimelinePanel />
          </SlidePanel>
        </div>
      </Layout>

      {/* Global modals */}
      <Modal
        title={detailModal?.title}
        open={!!detailModal}
        onCancel={() => setDetailModal(null)}
        footer={<Button onClick={() => setDetailModal(null)}>{t('关闭')}</Button>}
        width={640}
        className="jx-detailModal"
        destroyOnHidden
      >
        {detailModal?.body}
      </Modal>

      <ImagePreview />
      <CreateKBModal onCreated={() => void refreshCatalog()} />
      <ReindexModal />
      <AuthExpiredModal />
      <BatchConfirmModal onCancelResume={cancelAndResumeBatch} />
    </Layout>
  );
}
