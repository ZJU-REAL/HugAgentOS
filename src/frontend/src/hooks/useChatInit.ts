import { useEffect,useRef } from 'react';
import { authFetch,chatTargetHeaders,checkSession,isHybridDual } from '../api';
import { usePanel } from '../routing/usePanel';
import { newDraftChatId } from '../storage';
import { isLocalDraftChat,useAuthStore,useAutomationChatStore,useCatalogStore,useChatStore,useSettingsStore,useSidebarOrderStore,useUIStore } from '../stores';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import type { ChatItem,UpdateEntry } from '../types';
import { preservedChatsOnRebuild } from '../utils/sessionRebuild';
import { inflightMsgLoads,MSG_LOAD_MAX_RETRIES,msgLoadRetryCounts,reloadChatHistory } from './chatHistoryLoader';
import { isLocalSidebarChat,mergeLocalSessions,sessionToChatItem } from './chatSessionMapping';
import { useChatBatchHydration } from './useChatBatchHydration';

export { ensureFullMessages,loadOlderMessages,MESSAGE_PAGE_SIZE,reloadChatHistory } from './chatHistoryLoader';
export { parseHistoryMessage } from './chatHistoryMessage';
const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

export function useChatInit() {
  const { authUser, authExpiredUrl, authChecking, initAuth } = useAuthStore();
  const { loadMemorySettings, loadOntologySettings } = useSettingsStore();
  const { setFeatureUpdates } = useUIStore();
  const {
    updateStore, adoptChatFromUrl, setChatsLoading, setToolDisplayNames,
    addBackendSessionId, clearBackendSessionIds,
    clearLoadedMsgIds,
    currentChatId, sessionLoadEpoch, bumpSessionLoadEpoch,
    hydrateForUser,
  } = useChatStore();
  const fetchCatalog = useCatalogStore((state) => state.fetchCatalog);
  const panel = usePanel();

  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auth initialization
  useEffect(() => { initAuth(); }, []);

  // Load memory settings when auth is ready
  useEffect(() => {
    if (!authUser) return;
    loadMemorySettings();
    loadOntologySettings();
  }, [authUser]);

  // Proactive session heartbeat
  useEffect(() => {
    if (!authUser) return;
    let lastCheck = Date.now();
    const SESSION_CHECK_INTERVAL = 30_000;
    let checking = false;

    const onInteraction = async () => {
      if (authExpiredUrl) return;
      const now = Date.now();
      if (now - lastCheck < SESSION_CHECK_INTERVAL) return;
      if (checking) return;
      checking = true;
      lastCheck = now;
      try {
        // Refresh capability bits while renewing: after an admin changes user/team/role permissions, an already-logged-in user
        // syncs within ~30s without re-logging in (only written back when there's an actual change, to avoid needless re-renders).
        const fresh = await checkSession();
        const cur = useAuthStore.getState().authUser;
        if (fresh && cur && JSON.stringify(fresh) !== JSON.stringify(cur)) {
          useAuthStore.getState().setAuthUser(fresh);
        }
      } catch { /* session invalidation is handled by the global 401 handler */ } finally { checking = false; }
    };

    document.addEventListener('click', onInteraction, { capture: true });
    document.addEventListener('keydown', onInteraction, { capture: true });
    return () => {
      document.removeEventListener('click', onInteraction, { capture: true });
      document.removeEventListener('keydown', onInteraction, { capture: true });
    };
  }, [authUser, authExpiredUrl]);

  // Fetch docs content
  useEffect(() => {
    if (authChecking || !authUser) return;
    if (panel !== 'docs') return;
    authFetch(`${effectiveApiUrl}/v1/content/docs`)
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data?.data?.updates)) setFeatureUpdates(data.data.updates as UpdateEntry[]);
      })
      .catch(() => {});
  }, [panel, authChecking, authUser, setFeatureUpdates]);

  // 能力目录只有 catalogStore.fetchCatalog 一个取法；这里曾经另写过一份等价实现，
  // 两份分别演化就会出现「同一个开关两个页面显示不一样」。
  useEffect(() => {
    if (authChecking || !authUser) return;
    void fetchCatalog();
  }, [effectiveApiUrl, authUser, authChecking, fetchCatalog]);

  // Fetch tool display names
  useEffect(() => {
    if (authChecking || !authUser) return;
    authFetch(`${effectiveApiUrl}/v1/config/tool-names`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && typeof data.tools === 'object') {
          setToolDisplayNames({ ...data.tools, ...(data.servers || {}) });
        }
      })
      .catch(() => {});
  }, [effectiveApiUrl, authChecking, authUser]);

  // Load chat sessions from backend
  // Use authUser?.user_id (not the full authUser object) so that updating only
  // the avatar URL does not trigger a re-fetch and panel navigation.
  const authUserId = authUser?.user_id ?? null;
  const localReady = useDeploymentModeStore((s) => s.localReady);
  useEffect(() => {
    if (authChecking || !authUserId) return;
    if (!effectiveApiUrl) return;
    // Switch the chat store into this user's context BEFORE we read its
    // snapshot — without this, the snapshot would be either empty (initial
    // boot) or, worse, the previous user's data still in memory after a
    // login swap.
    hydrateForUser(authUserId);
    useAutomationChatStore.getState().hydrateForUser(authUserId);
    // 侧边栏手动拖拽顺序：本地秒开 + 异步拉服务端顺序（见 sidebarOrderStore）
    useSidebarOrderStore.getState().hydrateForUser(authUserId);
    const localSnapshot = useChatStore.getState().store;
    clearBackendSessionIds();
    clearLoadedMsgIds();

    let cancelled = false;

    const fetchSessions = async () => {
      setChatsLoading(true);
      try {
        const r = await authFetch(`${effectiveApiUrl}/v1/chats?page_size=100`);
        if (!r.ok || cancelled) return;
        const payload = await r.json();
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const items: any[] = payload?.data?.items || [];
        // 云端会话先上屏；双模式下本机会话由下面的就绪效应并入，不让侧边栏等本机启动。
        const chats: Record<string, ChatItem> = {};
        const order: string[] = [];

        for (const s of items) {
          const id: string = s.chat_id;
          chats[id] = sessionToChatItem(s, localSnapshot.chats[id]);
          order.push(id);
          addBackendSessionId(id);
        }

        if (!cancelled) {
          // Capture previously selected chat before updating store
          const prevChatId = useChatStore.getState().currentChatId;

          updateStore((prev) => {
            // Session fetches are asynchronous. Preserve the freshest explicit composer choice
            // from the live store as well as the startup snapshot, so a click made while this
            // request was in flight cannot be overwritten by the server response.
            const mergedServerChats: Record<string, ChatItem> = {};
            for (const [id, serverChat] of Object.entries(chats)) {
              const active = prev.chats[id]?.planModeActive;
              const batchActive = prev.chats[id]?.batchModeActive;
              mergedServerChats[id] = {
                ...serverChat,
                ...(typeof active === 'boolean' ? { planModeActive: active } : {}),
                ...(typeof batchActive === 'boolean' ? { batchModeActive: batchActive } : {}),
              };
            }
            // 保留判定看**来源**（见 utils/sessionRebuild）：本机会话不在云端名单里
            // 是天经地义的，不能当成它已过期。
            const kept = preservedChatsOnRebuild(
              new Set(Object.keys(mergedServerChats)),
              prev,
              localSnapshot,
              isLocalSidebarChat,
            );
            const preserved = kept.chats;
            const preservedOrder = kept.order;
            // A pending server list must not erase a newly selected execution target.
            const currentId = useChatStore.getState().currentChatId;
            const draft = prev.chats[currentId];
            if (draft && !mergedServerChats[currentId] && !preserved[currentId]) {
              preserved[currentId] = draft;
              if (draft.messages.length > 0) preservedOrder.unshift(currentId);
            }
            return {
              chats: { ...mergedServerChats, ...preserved },
              order: [...order, ...preservedOrder],
            };
          });

          const allChats = { ...chats };
          // 恢复目标就是地址栏指的那段会话（prevChatId 已由 hydrateForUser 从地址读出）。
          // 后端已有 → 恢复历史；后端没有（正在流式输出首条消息、或本地会话还没并进来）
          // → 保留同一 id，空会话渲染出来就是空首页。
          //
          // 用 adopt 而不是 setCurrentChatId：这里的输入本来就来自地址栏，再走一遍
          // 写地址的动作，会在会话列表尚未合并完时把「还没进历史」误判成草稿，
          // 把用户刚点开的那条链接退回首页。
          const targetChatId = prevChatId || newDraftChatId(authUserId);
          adoptChatFromUrl(targetChatId);
          // Bump epoch so the lazy-load messages effect re-fires even when
          // currentChatId hasn't changed (e.g. the same chat id is restored from the URL).
          bumpSessionLoadEpoch();

          // Pre-load messages for the target chat BEFORE clearing chatsLoading,
          // so the user never sees the empty home page flash.
          if (
            allChats[targetChatId]
            && !useChatStore.getState().loadedMsgIds.has(targetChatId)
            && !inflightMsgLoads.has(targetChatId)
          ) {
            inflightMsgLoads.add(targetChatId);
            // Only mark the chat "loaded" when this quick single-page fetch
            // fully covered it. On failure (non-2xx, network error) or when
            // more pages exist, re-bump the epoch so the lazy-load effect
            // actually retries — otherwise the chat is stuck on the skeleton
            // until the next full refresh.
            // 只铺第一屏就算"预载完成"——更早的内容由 ChatArea 滚到顶时续拉。
            let preloadComplete = false;
            try {
              preloadComplete = await reloadChatHistory(targetChatId);
            } catch { /* ignore — lazy-load retries via the epoch bump below */ }
            inflightMsgLoads.delete(targetChatId);
            if (!cancelled) {
              if (!preloadComplete) {
                // The lazy-load effect already ran (and skipped) while this
                // preload held the in-flight lock; bump the epoch so it
                // re-fires now that the lock is released.
                bumpSessionLoadEpoch();
              }
            }
          }
        }
      } catch { /* Lazy history loading remains available after a session-list failure. */ } finally {
        if (!cancelled) setChatsLoading(false);
      }
    };

    fetchSessions();
    // 本机那一面和云端并行拉、各自到达即上屏，不让侧边栏等本机启动。本机执行面
    // 这一刻可能还在装（客户端更新后必然如此）——就绪后下面的效应会再并一次。
    if (isHybridDual()) {
      mergeLocalSessions(effectiveApiUrl, () => cancelled)
        .catch(() => { /* 本机执行面暂不可达：就绪效应会重来 */ });
    }

    // 侧边栏「运行中」小圆点：本标签页只知道自己挂着的流，换设备 / 换浏览器登录时
    // 得问一次服务端才知道哪些会话还在跑（非阻塞，失败留给下一次刷新）。
    useChatStore.getState().refreshRemoteRunningChats()
      .catch(() => { /* 灯保持原样，窗口切回来时会再问一次 */ });

    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveApiUrl, authUserId, authChecking]);

  // 窗口重新切回前台时再问一次"哪些会话还在跑"。
  //
  // 这盏灯反映的是别处（另一台设备 / 另一个标签页）的进度，本标签页收不到它的
  // 事件，所以跑完之后得有人来灭灯。挂在"切回前台"上而不是定时轮询：人不在这个
  // 窗口前时灯亮不亮没人看，没必要为此持续给服务端加压。
  useEffect(() => {
    if (authChecking || !authUserId) return;
    const refresh = () => {
      if (document.visibilityState !== 'visible') return;
      useChatStore.getState().refreshRemoteRunningChats().catch(() => { /* 下次切回来再试 */ });
    };
    document.addEventListener('visibilitychange', refresh);
    window.addEventListener('focus', refresh);
    return () => {
      document.removeEventListener('visibilitychange', refresh);
      window.removeEventListener('focus', refresh);
    };
  }, [authUserId, authChecking]);

  // 双模式：本机执行面就绪（可能晚于首屏）后再并一次本机会话。就绪帧意味着壳已经
  // 成功把身份推给本机后端，所以此刻它一定起得来——首屏那次没赶上的，这次补齐。
  useEffect(() => {
    if (authChecking || !authUserId || !isHybridDual() || !localReady) return;
    let cancelled = false;
    mergeLocalSessions(effectiveApiUrl, () => cancelled)
      .catch(() => { /* 下一次侧边栏加载还会两面都问 */ });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveApiUrl, authUserId, authChecking, localReady]);

  // 计划栏还原 —— 计划清单的真源在服务端（会话 metadata.plan_progress）。
  //
  // 计划栏本身是内存态，刷新就没；而工作流模式下一份计划要跨好几轮才走完（提交作业 →
  // 后台跑几十分钟 → 作业跑完的交付轮收尾）。中途刷新、切走再回来、或者干脆关了页面
  // 第二天再看，过去都只能看到"什么都没有"，或者停在离开时那一步的转圈。这里按服务端
  // 快照恢复：包括它是否已经收尾（settled → done），所以收尾也不再依赖"当时这个标签页
  // 恰好在跟那条流"。本地那份更新（正在跟流）优先，别把实时进度盖回旧快照。
  useEffect(() => {
    const chatId = currentChatId;
    if (!chatId) return;
    const st = useChatStore.getState();
    const persisted = st.store.chats[chatId]?.planProgress;
    if (!persisted) return;
    const live = st.planProgress[chatId];
    if (live && live.updatedAt >= persisted.updatedAt) return;
    st.setPlanProgress(chatId, persisted);
  }, [currentChatId, sessionLoadEpoch]);

  // Lazy-load messages for current chat
  useEffect(() => {
    if (authChecking || !authUser) return;
    const chatId = currentChatId;
    const state = useChatStore.getState();
    if (state.loadedMsgIds.has(chatId)) return;
    // Another run (or the startup preload) is already fetching this chat.
    // If that run gets cancelled it re-bumps the epoch, so skipping here
    // never strands the chat.
    if (inflightMsgLoads.has(chatId)) return;

    let cancelled = false;

    // If this chat isn't in the backend session list we fetched on startup,
    // it might be an automation-generated chat, a notification-linked chat,
    // or any chat we haven't "seen" before. Try to hydrate its session
    // metadata from GET /v1/chats/{id} before loading messages.
    // If the chat also isn't in the local store (i.e. a brand-new local chat
    // the user hasn't typed into yet), the 404 response short-circuits us.
    const hydrateSessionIfMissing = async (): Promise<boolean> => {
      if (state.backendSessionIds.has(chatId)) return true;
      // 只在浏览器里存在、还没发过一句话的新对话：服务端必然 404，别去问。
      if (isLocalDraftChat(chatId)) return false;
      const localChat = state.store.chats[chatId];
      if (localChat && localChat.messages.length > 0) {
        // Local-only chat with content — don't hit backend
        return false;
      }
      try {
        const sr = await authFetch(`${effectiveApiUrl}/v1/chats/${chatId}`, { headers: chatTargetHeaders(chatId) });
        if (!sr.ok || cancelled) return false;
        const sp = await sr.json();
        const s = sp?.data;
        if (!s || !s.chat_id) return false;
        if (cancelled) return false;
        updateStore(prev => {
          const newChatItem = sessionToChatItem(s, prev.chats[s.chat_id]);
          return {
            ...prev,
            chats: { ...prev.chats, [s.chat_id]: newChatItem },
            order: prev.order.includes(s.chat_id) ? prev.order : [s.chat_id, ...prev.order],
          };
        });
        addBackendSessionId(chatId);
        return true;
      } catch {
        return false;
      }
    };

    const fetchMessages = async () => {
      const hydrated = await hydrateSessionIfMissing();
      if (!hydrated || cancelled) return;
      // Take the in-flight lock; the "loaded" store mark is only set after
      // the messages are actually written. Keeping the two separate matters:
      // the mark drives the skeleton-vs-empty-state UI, so setting it during
      // the fetch would flash the empty home page while messages load, and
      // a cancelled/failed load must leave the mark unset so the next visit
      // retries instead of being stuck on the skeleton forever.
      inflightMsgLoads.add(chatId);
      let loaded = false;
      try {
        // A non-2xx page (401 blip, 502 during backend restart, 429…) must not
        // leave the chat marked "loaded" — throw so the retry below kicks in.
        if (!(await reloadChatHistory(chatId))) throw new Error('history page failed');
        loaded = true;
        msgLoadRetryCounts.delete(chatId);
      } catch {
        // HTTP/网络失败：有限次自动重试（问题16：历史对话长时间停在骨架屏）。
        // 超过上限后放弃，等用户下次切入该会话再试。
        if (!cancelled) {
          const attempts = (msgLoadRetryCounts.get(chatId) || 0) + 1;
          msgLoadRetryCounts.set(chatId, attempts);
          if (attempts <= MSG_LOAD_MAX_RETRIES) {
            window.setTimeout(() => {
              const st = useChatStore.getState();
              if (st.currentChatId === chatId && !st.loadedMsgIds.has(chatId)) {
                st.bumpSessionLoadEpoch();
              }
            }, 1500 * attempts);
          }
        }
      } finally {
        inflightMsgLoads.delete(chatId);
        // Switch-back race: if the user already navigated back to this chat
        // while this cancelled run still held the in-flight lock, that
        // navigation's effect run skipped on the lock and nobody will
        // refetch. Re-fire the effect now that the lock is released. Only on
        // cancellation — an HTTP failure must not self-retry in a loop.
        if (!loaded && cancelled && useChatStore.getState().currentChatId === chatId) {
          bumpSessionLoadEpoch();
        }
      }
    };

    fetchMessages();
    return () => { cancelled = true; };
  }, [currentChatId, effectiveApiUrl, authUser, authChecking, sessionLoadEpoch]);

  useChatBatchHydration(currentChatId);

  return {
    effectiveApiUrl,
    refreshCatalog: fetchCatalog,
    searchTimerRef,
  };
}
