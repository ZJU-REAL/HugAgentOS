import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router';
import { usePanel } from '../routing/usePanel';
import { useAuthStore, useChatStore, useEditionStore, useModelCapabilitiesStore, useUIStore } from '../stores';
import { useAutomationChatStore } from '../stores/automationChatStore';
import { useAutomationStore } from '../stores/automationStore';
import { useCanvasStore } from '../stores/canvasStore';
import type { ChatMode } from '../stores/chatStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { useMySpaceStore } from '../stores/mySpaceStore';
import { usePageConfigStore } from '../stores/pageConfigStore';
import { useAutomationConversations } from './useAutomationConversations';
import { usePageConfigAll } from './usePageConfig';
export function useAppBootstrap(chatSurface: boolean) {
  const { pathname } = useLocation();
  const pageConfig = usePageConfigAll();
  const panel = usePanel();
  const { authUser, authChecking } = useAuthStore();
  const setSiderCollapsed = useUIStore(s => s.setSiderCollapsed);
  const openSearchModal = useUIStore(s => s.openSearchModal);
  const resetRightSidebar = useCanvasStore(s => s.resetSidebar);
  const currentChatId = useChatStore(s => s.currentChatId);
  // Mobile uses the full sidebar as an off-canvas drawer. Remember the desktop
  // rail state while entering mobile so resizing back does not unexpectedly
  // change the user's desktop preference.
  const wasMobileViewportRef = useRef(false);
  const desktopSiderCollapsedRef = useRef(false);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 960px)');
    const syncSidebarForViewport = () => {
      if (media.matches && !wasMobileViewportRef.current) {
        desktopSiderCollapsedRef.current = useUIStore.getState().siderCollapsed;
        setSiderCollapsed(true);
      } else if (!media.matches && wasMobileViewportRef.current) {
        setSiderCollapsed(desktopSiderCollapsedRef.current);
      }
      wasMobileViewportRef.current = media.matches;
    };
    syncSidebarForViewport();
    media.addEventListener('change', syncSidebarForViewport);
    return () => media.removeEventListener('change', syncSidebarForViewport);
  }, [setSiderCollapsed]);

  useEffect(() => {
    if (window.matchMedia('(max-width: 960px)').matches) setSiderCollapsed(true);
  }, [pathname, setSiderCollapsed]);

  const closeMobileSidebar = () => {
    if (window.matchMedia('(max-width: 960px)').matches) {
      setSiderCollapsed(true);
    }
  };

  const openMobileSidebar = () => {
    if (window.matchMedia('(max-width: 960px)').matches) {
      setSiderCollapsed(false);
    }
  };

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
    const VALID: readonly ChatMode[] = ['turbo', 'fast', 'low', 'medium', 'high', 'xhigh', 'max'];
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
    if (authChecking || !authUserId) return;
    void fetchCapabilities();
  }, [fetchCapabilities, authChecking, authUserId]);

  // Fetch edition capabilities at startup; CE has no extension entries.
  const fetchEdition = useEditionStore((s) => s.fetchEdition);
  useEffect(() => {
    void fetchEdition();
  }, [fetchEdition]);

  // ── Notification polling (60s) — updates the sidebar badge on My Space ──
  // Refresh all tasks so newly completed runs appear without reloading the page.
  const fetchNotifCount = useMySpaceStore((s) => s.fetchNotifications);
  const fetchTasks = useAutomationStore(s => s.fetchTasks);
  const localReady = useDeploymentModeStore(s => s.localReady);
  useAutomationConversations(authUserId, panel === 'automation', localReady);
  useEffect(() => {
    useAutomationStore.getState().reset();
    useAutomationChatStore.getState().exitAutomationChat();
  }, [authUserId]);
  useEffect(() => {
    if (!authUserId) return;
    // Initial fetch
    void fetchNotifCount();
    void fetchTasks();
    const timer = setInterval(() => {
      void fetchNotifCount();
      void fetchTasks();
    }, 60_000);
    return () => clearInterval(timer);
  }, [authUserId, fetchNotifCount, fetchTasks, localReady]);

  // Right-side content belongs to the current main panel/chat. Clear it when
  // context changes so a file or ontology result never leaks into another chat.
  useEffect(() => {
    resetRightSidebar();
  }, [panel, currentChatId, resetRightSidebar]);

  useEffect(() => {
    if (!chatSurface) useChatStore.getState().setLoopMode(false);
  }, [chatSurface]);

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

  return { closeMobileSidebar, openMobileSidebar, authUserId };
}
