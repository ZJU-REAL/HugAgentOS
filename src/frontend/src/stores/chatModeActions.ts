import { saveChatStoreDebounced } from '../storage';
import type { ChatStore as ChatStoreData } from '../types';
import type { ChatState } from './chatState';
import type { ChatActionsContext } from './chatSupport';
import { chatDraftKey, useComposerStore } from './composerStore';
import { t } from '../i18n';
import { newDraftChatId } from '../storage';
import type { ChatItem } from '../types';
import { usePluginStore } from './pluginStore';
import { SITES_PLUGIN_SLUG } from './chatSupport';

export function createChatModeActions({ set, get, initializeDraftRunTarget, syncChatUrl }: ChatActionsContext): Pick<ChatState,
  'enterChatMode' | 'exitChatMode' | 'enterSiteMode'
> {
  return {
    enterChatMode: (mode, opts) => {
        const inPlace = !!opts?.inPlace;
        const { store, currentChatId, currentUserId, sendingChatIds } = get();
        const planChat = mode === 'plan';
        const existing = store.chats[currentChatId];
        const now = Date.now();
        // inPlace: always switch the current chat in place (no new chat, no navigation).
        // Otherwise: current chat has no messages yet → reuse in place; has messages → create a new chat in that mode.
        const reuse = inPlace || !existing || existing.messages.length === 0;
        const targetId = reuse ? currentChatId : newDraftChatId(currentUserId);
        const base: ChatItem = reuse && existing
          ? existing
          : {
              id: targetId,
              title: t('新对话'),
              createdAt: now,
              updatedAt: now,
              messages: [],
              favorite: false,
              pinned: false,
              businessTopic: '综合咨询',
              // New chats inherit the source chat's project binding, keeping plan/batch under the current project
              ...(existing?.projectId
                ? { projectId: existing.projectId, projectName: existing.projectName }
                : {}),
            };
        const nextChat: ChatItem = { ...base, id: targetId, updatedAt: now };
        // Plan / batch / workflow 三者互斥：进入一个就清掉另外两个的标记
        delete nextChat.planChat;
        delete nextChat.planModeActive;
        delete nextChat.batchChat;
        delete nextChat.batchModeActive;
        delete nextChat.workflowChat;
        delete nextChat.workflowModeActive;
        if (mode === 'plan') {
          nextChat.planChat = true;
          nextChat.planModeActive = true;
        } else if (mode === 'workflow') {
          nextChat.workflowChat = true;
          nextChat.workflowModeActive = true;
        } else {
          nextChat.batchChat = true;
          nextChat.batchModeActive = true;
        }
        const next: ChatStoreData = {
          chats: { ...store.chats, [targetId]: nextChat },
          // When reusing an empty chat, leave order untouched (it enters the sidebar history when the first message is sent, consistent with bindChatProject); new chats go to the top.
          order: reuse ? store.order : [targetId, ...store.order.filter((oid) => oid !== targetId)],
        };
        set({ store: next, storeRef: next });
        saveChatStoreDebounced(currentUserId, next);
        syncChatUrl(targetId);
        set({
          currentChatId: targetId,
          planMode: planChat,
          // Plan / batch and the autonomous loop are mutually exclusive: leaving loopMode on would
          // keep a stale composer intent that resurfaces the moment plan/batch is closed again.
          loopMode: false,
          currentPlanId: null,
          toolResultPanel: null,
          sending: sendingChatIds.has(targetId),
        });
        initializeDraftRunTarget(targetId, !reuse);
      },
    exitChatMode: (mode) => {
        if (mode === 'plan') {
          // setPlanMode already persists planModeActive = false on the current chat.
          get().setPlanMode(false);
          return;
        }
        const { currentChatId, currentUserId, store } = get();
        const chat = store.chats[currentChatId];
        if (!chat) return;
        const patch =
          mode === 'workflow' ? { workflowModeActive: false } : { batchModeActive: false };
        const next: ChatStoreData = {
          ...store,
          chats: {
            ...store.chats,
            [currentChatId]: { ...chat, ...patch },
          },
        };
        set({ store: next, storeRef: next });
        saveChatStoreDebounced(currentUserId, next);
      },
    enterSiteMode: (opts) => {
        const { store, currentChatId, currentUserId, sendingChatIds } = get();
        // Resolve the installed "sites" plugin: site-building capability is purely plugin-gated; only when installed do its skills + MCP get attached to this chat.
        const sitesPlugin = usePluginStore
          .getState()
          .installed.find((p) => p.slug === SITES_PLUGIN_SLUG && p.enabled !== false);
        const sitesActivePlugin = sitesPlugin
          ? {
              id: sitesPlugin.install_id,
              name: sitesPlugin.name,
            }
          : null;
        const projectId = opts?.projectId?.trim() || undefined;
        // When editing an existing site, name the chat after the site title and avoid reusing the current empty chat (switch to a clean editing chat).
        const isEdit = !!(projectId && opts?.title);
        const existing = store.chats[currentChatId];
        const now = Date.now();
        // If the current chat is empty and this isn't an edit, reuse in place; otherwise create a new site-building chat (consistent with enterChatMode).
        const reuse = !isEdit && (!existing || existing.messages.length === 0);
        const targetId = reuse ? currentChatId : newDraftChatId(currentUserId);
        const base: ChatItem = reuse && existing
          ? existing
          : {
              id: targetId,
              title: opts?.title ? t('编辑站点：{name}', { name: opts.title }) : t('新对话'),
              createdAt: now,
              updatedAt: now,
              messages: [],
              favorite: false,
              pinned: false,
              businessTopic: '综合咨询',
            };
        // Site building is mutually exclusive with plan / batch
        const nextChat: ChatItem = { ...base, id: targetId, updatedAt: now, siteChat: true };
        delete nextChat.planChat;
        delete nextChat.planModeActive;
        delete nextChat.batchChat;
        delete nextChat.batchModeActive;
        // Bind the site source-code project → messages are sent with project_id automatically, and agent file tools operate inside the project folder.
        if (projectId) {
          nextChat.projectId = projectId;
          if (opts?.projectName) nextChat.projectName = opts.projectName;
        } else {
          delete nextChat.projectId;
          delete nextChat.projectName;
        }
        const next: ChatStoreData = {
          chats: { ...store.chats, [targetId]: nextChat },
          order: reuse ? store.order : [targetId, ...store.order.filter((oid) => oid !== targetId)],
        };
        set({ store: next, storeRef: next });
        saveChatStoreDebounced(currentUserId, next);
        useComposerStore.getState().activate(chatDraftKey(targetId), { activePlugin: sitesActivePlugin });
        syncChatUrl(targetId);
        set({
          currentChatId: targetId,
          planMode: false,
          currentPlanId: null,
          toolResultPanel: null,
          loopMode: false,
          sending: sendingChatIds.has(targetId),
        });
        initializeDraftRunTarget(targetId, !reuse);
        return !!sitesPlugin;
      }
  };
}
