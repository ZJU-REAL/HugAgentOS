import { chatDraftKey, useComposerStore } from './composerStore';
import { create } from 'zustand';
import { isHybridDual, isRegisteredLocalChat, setChatRoutingContext } from '../api';
import { isHomePath, navigateTo, pathForChat, setChatPathResolver } from '../routing/navigation';
import { isDraftChatId, isNewDraftChatId, mergeChatStores, setStreamingIdsProvider, STORAGE_KEY, userScopedKey } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import { usePluginStore } from './pluginStore';
import { SITES_PLUGIN_SLUG, isAutomationEntry } from './chatSupport';
import type { ChatState } from './chatState';
import { createChatInitialState } from './chatInitialState';
import { createChatSelectionActions } from './chatSelectionActions';
import { createChatRunActions } from './chatRunActions';
import { createChatLifecycleActions } from './chatLifecycleActions';
import { createChatModeActions } from './chatModeActions';
import { createChatProjectActions } from './chatProjectActions';

/** 这段对话只在浏览器里存在、服务端还没有：登记过是本地草稿、不在服务端会话列表里、
 *  本地也一条消息都没有。三条同时成立才算 —— 任何一条不成立都退回原来的行为（照常
 *  请求服务端），所以自动化生成、通知点进来的会话不会被误判。
 *
 *  用途是免掉必然 404 的会话详情 / 待确认 / 待回答提问请求：浏览器会把 404 响应打成
 *  红色报错，刷新一次刷屏一片，看着像页面坏了。 */
export function isLocalDraftChat(chatId: string): boolean {
  const s = useChatStore.getState();
  if (!chatId || s.backendSessionIds.has(chatId)) return false;
  if ((s.store.chats[chatId]?.messages?.length ?? 0) > 0) return false;
  return isDraftChatId(s.currentUserId, chatId);
}

/** 这段会话配不配拥有自己的地址。判定要正面证据——服务端有它、它在左侧历史列表里、
 *  或者它已经有消息。不能反过来问「它是不是一段已登记的本地草稿」：草稿名单存在浏览器
 *  里且有数量上限，新建对话多了旧 id 会被挤掉，一被挤掉，一段空白的新对话就会被误判成
 *  正式会话写进地址（新对话页变成 /c/<草稿id>）。 */
export function isAddressableChat(chatId: string): boolean {
  if (!chatId || isAutomationEntry(chatId)) return false;
  const s = useChatStore.getState();
  return s.backendSessionIds.has(chatId)
    || (s.store.order || []).includes(chatId)
    || (s.store.chats[chatId]?.messages?.length ?? 0) > 0;
}
/** Only an unsent project conversation is the project overview. */
export function projectOverviewId(chat: { id: string; projectId?: string } | undefined): string | undefined {
  return chat?.projectId && !isAddressableChat(chat.id) ? chat.projectId : undefined;
}
export const useChatStore = create<ChatState>((set,get)=> {

  // Only frontend-created, unsent drafts get a default. Never move restored history.
  const initializeDraftRunTarget = (chatId: string, newlyCreated = false) => {
    const state = get();
    const chat = state.store.chats[chatId];
    if (!isHybridDual() || !isNewDraftChatId(chatId) || !isLocalDraftChat(chatId) || isRegisteredLocalChat(chatId)
        || chat?.projectId || chat?.runTarget
        || (!newlyCreated && state.store.order.includes(chatId))) return;
    state.setChatRunTarget(chatId, 'local');
  };

  const syncChatUrl = (chatId: string, opts?: { replace?: boolean }) => {
    if (isAutomationEntry(chatId)) return;
    navigateTo(pathForChat(isAddressableChat(chatId) ? chatId : null), opts);
  };

  /** 草稿发出第一条消息就有了历史记录 → 地址从首页换成它自己的 `/c/<会话id>`。
   *  挂在写库之后而不是各个发送入口：普通 / 计划 / 自主循环三条发送路径各记一次，
   *  迟早会漏（事实上第一版就漏了计划模式那条）。
   *  用 replace：后退键该回到上一段对话，而不是回到刚刚那段空白草稿。
   *
   *  **只在首页才升级**。流式输出期间每来一段文字就写一次库，如果条件写成「地址上
   *  没有会话 id」，用户切到我的空间、设置等任何别的页面都满足这个条件，下一帧就会
   *  被拽回会话——多开几段会话时尤其明显。首页是唯一「该有地址却还没有」的位置。 */
  const publishCurrentChatUrl = () => {
    if (!isHomePath()) return;
    const id = get().currentChatId;
    if (isAddressableChat(id)) navigateTo(pathForChat(id), { replace: true });
  };

  setChatPathResolver(() => {
    const id = get().currentChatId;
    return pathForChat(isAddressableChat(id) ? id : null);
  });

  // 合并写盘时：本标签页正在流式输出的会话一律以本内存版本为准
  setStreamingIdsProvider(() => get().sendingChatIds);
  const context = { set, get, initializeDraftRunTarget, syncChatUrl, publishCurrentChatUrl };
  return {
    ...createChatInitialState(),
    ...createChatSelectionActions(context),
    ...createChatRunActions(context),
    ...createChatProjectActions(context),
    ...createChatLifecycleActions(context),
    ...createChatModeActions(context),
  };
});

// ── 跨标签页同步：另一个标签页写入会话数据时，把它按会话粒度合并进本页内存 ──
// storage 事件只在"其他"标签页触发（写入方不触发），不会自激振荡；合并结果
// 不回写磁盘（磁盘上已是并集），避免写风暴。本页正在流式输出的会话保留本页版本。
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e: StorageEvent) => {
    const st = useChatStore.getState();
    const uid = st.currentUserId;
    if (!uid || !e.newValue) return;
    if (e.key !== userScopedKey(STORAGE_KEY, uid)) return;
    let incoming: ChatStoreData | null = null;
    try {
      const parsed = JSON.parse(e.newValue);
      if (parsed && typeof parsed === 'object') {
        incoming = { chats: parsed.chats || {}, order: parsed.order || [] };
      }
    } catch { /* 损坏的快照直接忽略 */ }
    if (!incoming) return;
    const merged = mergeChatStores(st.store, incoming, { preferAllIds: st.sendingChatIds });
    useChatStore.setState({ store: merged, storeRef: merged });
  });
}

setChatRoutingContext((chatId) => useChatStore.getState().store.chats[chatId]);

// Draft state follows conversation identity, independently of persisted message history.
useChatStore.subscribe((state, previous) => {
  if (state.currentUserId !== previous.currentUserId) useComposerStore.getState().resetForUser(state.currentUserId);
  if (state.currentChatId !== previous.currentChatId || state.currentUserId !== previous.currentUserId) {
    const chat = state.store.chats[state.currentChatId];
    const sites = chat?.siteChat ? usePluginStore.getState().installed.find(p => p.slug === SITES_PLUGIN_SLUG && p.enabled !== false) : undefined;
    useComposerStore.getState().activate(chatDraftKey(state.currentChatId), {
      activePlugin: sites ? { id: sites.install_id, name: sites.name } : null,
    });
  }
});
export type { ChatMode, ThinkingEffort, QueuedChatMessage } from './chatState';
export { SITES_PLUGIN_SLUG, isThinkingMode, isTurboMode } from './chatSupport';
