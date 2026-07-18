import { useRef } from 'react';
import { Modal, message } from 'antd';
import { t } from '../i18n';
import { authFetch } from '../api';
import { nowId } from '../storage';
import { buildHistorySegments } from '../utils/segments';
import { triggerPdfDownload, toSafeFileName } from '../utils/export';
import { SUMMARY_MAX_ROUNDS } from '../utils/constants';
import { formatDateKey } from '../utils/date';
import { useChatStore, useCatalogStore, useUIStore, useAutomationChatStore } from '../stores';
import { usePageConfigStore } from '../stores/pageConfigStore';
import type { ChatItem, ChatMessage } from '../types';

export function useChatActions(effectiveApiUrl: string) {
  const {
    store, updateStore, currentChatId, setCurrentChatId,
    setToolResultPanel,
    backendSessionIds, removeBackendSessionId,
    removeLoadedMsgId, addLoadedMsgId,
    clearShareSelection,
  } = useChatStore();
  const storeRef = useRef(store);
  storeRef.current = store;

  const { setPanel } = useCatalogStore();
  const { setEditingChatId, setEditingTitle, editingTitle } = useUIStore();

  function setPanelSafe(p: import('../types').PanelKey) {
    setPanel(p);
  }

  function newChat(inputRef: React.RefObject<HTMLTextAreaElement | null>) {
    const id = nowId('chat');
    if (useAutomationChatStore.getState().activeGroup) {
      useAutomationChatStore.getState().exitAutomationChat();
    }
    setCurrentChatId(id);
    setPanelSafe('chat');
    setToolResultPanel(null);
    clearShareSelection();
    inputRef.current?.focus();
  }

  function deleteChat(id: string) {
    Modal.confirm({
      title: t('删除历史对话'),
      content: t('确定删除该历史对话吗？该操作不可恢复。'),
      okText: t('删除'),
      okButtonProps: { danger: true },
      cancelText: t('取消'),
      onOk: () => {
        if (effectiveApiUrl && backendSessionIds.has(id)) {
          void authFetch(`${effectiveApiUrl}/v1/chats/${id}`, { method: 'DELETE' }).catch(() => {});
        }
        removeBackendSessionId(id);
        removeLoadedMsgId(id);
        updateStore((prev) => {
          const next = { chats: { ...prev.chats }, order: (prev.order || []).filter((x) => x !== id) };
          delete (next.chats as any)[id];
          if (currentChatId === id) {
            const nextId = next.order?.[0] || nowId('chat');
            setCurrentChatId(nextId);
          }
          return next;
        });
      },
    });
  }

  function patchChat(id: string, patch: Partial<ChatItem>) {
    // Favorite-only changes should NOT reorder or bump updatedAt
    const isFavoriteOnly = Object.keys(patch).length === 1 && patch.favorite !== undefined;

    updateStore((prev) => {
      const target = prev.chats[id];
      if (!target) return prev;
      const next: ChatItem = {
        ...target,
        ...patch,
        ...(isFavoriteOnly ? {} : { updatedAt: Date.now() }),
      };
      return {
        chats: { ...prev.chats, [id]: next },
        order: isFavoriteOnly
          ? (prev.order || [])
          : [id, ...(prev.order || []).filter((x) => x !== id)],
      };
    });

    // Use getState() to read the freshest backendSessionIds (avoids stale closures)
    const latestBackendIds = useChatStore.getState().backendSessionIds;
    if (effectiveApiUrl && latestBackendIds.has(id)) {
      const backendPatch: Record<string, unknown> = {};
      const latestChat = useChatStore.getState().store.chats[id];
      if (patch.title !== undefined) backendPatch.title = patch.title;
      if (patch.pinned !== undefined) backendPatch.pinned = patch.pinned;
      if (patch.favorite !== undefined) backendPatch.favorite = patch.favorite;
      if (patch.businessTopic !== undefined) {
        backendPatch.metadata = {
          businessTopic: patch.businessTopic,
          ...(latestChat?.agentId ? { agent_id: latestChat.agentId } : {}),
          ...(latestChat?.agentName ? { agent_name: latestChat.agentName } : {}),
          ...(latestChat?.planChat ? { plan_chat: true } : {}),
          ...(latestChat?.batchChat ? { batch_chat: true } : {}),
        };
      }
      if (Object.keys(backendPatch).length > 0) {
        void authFetch(`${effectiveApiUrl}/v1/chats/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(backendPatch),
        }).then((resp) => {
          if (!resp.ok) {
            console.error(`[patchChat] PATCH /v1/chats/${id} failed: ${resp.status}`);
            // Revert local state on failure
            if (patch.favorite !== undefined) {
              updateStore((prev) => {
                const target = prev.chats[id];
                if (!target) return prev;
                return { ...prev, chats: { ...prev.chats, [id]: { ...target, favorite: !patch.favorite } } };
              });
              message.error(t('收藏状态同步失败，请重试'));
            }
          }
        }).catch((err) => {
          console.error('[patchChat] network error:', err);
          if (patch.favorite !== undefined) {
            updateStore((prev) => {
              const target = prev.chats[id];
              if (!target) return prev;
              return { ...prev, chats: { ...prev.chats, [id]: { ...target, favorite: !patch.favorite } } };
            });
            message.error(t('网络异常，收藏状态同步失败'));
          }
        });
      }
    }
  }

  function toggleChatPinned(id: string) {
    const item = storeRef.current.chats[id];
    if (!item) return;
    patchChat(id, { pinned: !item.pinned });
  }

  function toggleChatFavorite(id: string) {
    const item = storeRef.current.chats[id];
    if (!item) return;
    patchChat(id, { favorite: !item.favorite });
  }

  function startRenameChat(item: ChatItem) {
    setEditingChatId(item.id);
    setEditingTitle(item.title || '新对话');
  }

  function commitRenameChat(id: string) {
    const nextTitle = editingTitle.trim() || '新对话';
    patchChat(id, { title: nextTitle });
    setEditingChatId(null);
    setEditingTitle('');
  }

  async function loadChatMessagesForExport(chatId: string): Promise<ChatMessage[]> {
    if (!effectiveApiUrl) return [];
    let page = 1;
    const allMessages: ChatMessage[] = [];

    while (true) {
      const r = await authFetch(`${effectiveApiUrl}/v1/chats/${chatId}/messages?page=${page}&page_size=100`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const payload = await r.json();
      const items: any[] = payload?.data?.items || [];
      for (const m of items) {
        const rawContent = String(m.content || '');
        const { cleanContent } = m.role === 'assistant'
          ? buildHistorySegments(rawContent)
          : { cleanContent: rawContent };
        allMessages.push({
          role: (m.role === 'assistant' ? 'assistant' : 'user') as 'user' | 'assistant',
          content: cleanContent,
          ts: m.created_at ? new Date(m.created_at).getTime() : Date.now(),
          isMarkdown: !!(m.metadata?.is_markdown),
        });
      }
      const pagination = payload?.data?.pagination;
      if (!pagination?.has_next) break;
      page += 1;
    }
    return allMessages;
  }

  async function exportChatRecord(chatId: string) {
    const target = storeRef.current.chats[chatId];
    if (!target) return;

    let messages = target.messages || [];
    const needsBackendLoad = messages.length === 0 && !!effectiveApiUrl && backendSessionIds.has(chatId);
    if (needsBackendLoad) {
      try {
        messages = await loadChatMessagesForExport(chatId);
        addLoadedMsgId(chatId);
        updateStore((prev) => {
          const c = prev.chats[chatId];
          if (!c) return prev;
          return { ...prev, chats: { ...prev.chats, [chatId]: { ...c, messages } } };
        });
      } catch {
        message.error(t('导出失败：加载对话内容失败'));
        return;
      }
    }

    const title = target.title || t('对话记录');
    const dateStr = formatDateKey(new Date());
    const safeTitle = toSafeFileName(title) || t('对话记录');
    const brandName = usePageConfigStore.getState().config.branding.product_name || 'HugAgentOS';
    triggerPdfDownload(`【${brandName}】${safeTitle}_${dateStr}.pdf`, title, messages, target.createdAt);
    message.success(t('对话已导出为 PDF'));
  }

  async function createChatShare(chatId: string, selectedTs: number[], expiryOption: '3d' | '15d' | '3m' | 'permanent') {
    const target = storeRef.current.chats[chatId];
    if (!target) throw new Error(t('当前会话不存在'));

    const selectedSet = new Set(selectedTs);
    const items = (target.messages || [])
      .filter((msg) => selectedSet.has(msg.ts))
      .map((msg) => {
        const planSeg = msg.segments?.find((s) => s.type === 'plan');
        return {
          role: msg.role,
          content: msg.content || '',
          is_markdown: Boolean(msg.isMarkdown),
          created_at: new Date(msg.ts).toISOString(),
          ...(planSeg?.planData ? { plan_data: planSeg.planData } : {}),
        };
      });

    if (items.length === 0) {
      throw new Error(t('请先选择要分享的对话记录'));
    }

    const response = await authFetch(`${effectiveApiUrl}/v1/chat-shares`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        origin_message_ts: [...selectedTs].sort((a, b) => a - b)[0] ?? null,
        title: target.title || '分享会话',
        items,
        expiry_option: expiryOption,
      }),
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload?.detail;
      const detailStr = typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
        ? detail.map((d: any) => d?.msg || JSON.stringify(d)).join('; ')
        : detail ? JSON.stringify(detail) : '';
      throw new Error(payload?.message || detailStr || `HTTP ${response.status}`);
    }

    return payload?.data as { share_id: string; preview_url: string; expires_at?: string | null; expiry_option: '3d' | '15d' | '3m' | 'permanent' };
  }

  async function generateSummary(chatId: string) {
    const chat = storeRef.current.chats[chatId];
    if (!chat || !effectiveApiUrl) return;
    const userMessages = chat.messages.filter(m => m.role === 'user');
    const assistantMessages = chat.messages.filter(m => m.role === 'assistant');
    if (userMessages.length === 0 || assistantMessages.length === 0) return;
    const exchangeCount = Math.min(userMessages.length, assistantMessages.length);
    if (exchangeCount > SUMMARY_MAX_ROUNDS) return;

    try {
      const response = await authFetch(`${effectiveApiUrl}/v1/summary`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: chat.messages.map(m => ({ role: m.role, content: m.content })) })
      });
      if (!response.ok) return;
      const data = await response.json();
      if (data?.data?.enabled === false) return;
      const summary = data?.data?.summary;
      if (summary && summary !== '新对话') {
        patchChat(chatId, { title: summary });
      }
    } catch (error) {
      console.warn('Failed to generate summary:', error);
    }
  }

  async function generateClassification(chatId: string) {
    const chat = storeRef.current.chats[chatId];
    if (!chat || !effectiveApiUrl) return;
    const userMessages = chat.messages.filter(m => m.role === 'user');
    const assistantMessages = chat.messages.filter(m => m.role === 'assistant');
    if (userMessages.length === 0 || assistantMessages.length === 0) return;
    const exchangeCount = Math.min(userMessages.length, assistantMessages.length);
    if (exchangeCount > SUMMARY_MAX_ROUNDS) return;

    try {
      const response = await authFetch(`${effectiveApiUrl}/v1/classify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: chat.messages.map(m => ({ role: m.role, content: m.content })) })
      });
      if (!response.ok) return;
      const data = await response.json();
      if (data?.data?.enabled === false) return;
      const topic = data?.data?.topic;
      if (topic) patchChat(chatId, { businessTopic: topic });
    } catch (error) {
      console.warn('Failed to classify conversation:', error);
    }
  }

  return {
    storeRef,
    setPanelSafe,
    newChat,
    deleteChat,
    patchChat,
    toggleChatPinned,
    toggleChatFavorite,
    startRenameChat,
    commitRenameChat,
    exportChatRecord,
    createChatShare,
    generateSummary,
    generateClassification,
  };
}
