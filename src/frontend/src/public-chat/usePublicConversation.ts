import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router';
import { message } from 'antd';
import { apiRequest, authFetch, getApiUrl, unwrapData, getPendingConfirm, getPendingUserQuestions, mergePendingUserQuestionRecovery } from '../api';
import { isAddressableChat, useChatStore } from '../stores/chatStore';
import { chatDraftKey, readComposer } from '../stores/composerStore';
import { processChatStream, markRunCancelledByUser } from '../hooks/chatStream';
import { reloadChatHistory } from '../hooks/chatHistoryLoader';
import { standaloneFromPath } from '../routing/standalone';
import { useModelCapabilitiesStore } from '../stores/modelCapabilitiesStore';
import { useUIStore } from '../stores/uiStore';
import { prepareChatAttachments } from '../utils/chatAttachments';
import { newMessageUid } from '../utils/messageIdentity';
import { t } from '../i18n';
import type { PublicChatInfo } from './PublicChatContext';
import type { ChatItem } from '../types';

export function createPublicDraft(info: PublicChatInfo) {
  const st = useChatStore.getState();
  const id = st.currentChatId;
  const now = Date.now();
  const draft: ChatItem = { id, title: t('新对话'), createdAt: now, updatedAt: now,
    messages: [], favorite: false, pinned: false,
    ...(info.kind === 'agents' ? { agentId: info.id, agentName: info.name } : { projectId: info.id, projectName: info.name }),
  };
  st.updateStore(prev => ({ ...prev, chats: { ...prev.chats, [id]: draft } }));
}

export function usePublicConversation(info: PublicChatInfo) {
  const { pathname } = useLocation();
  const currentId = useChatStore(s => s.currentChatId);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const controllers = useRef(new Map<string, AbortController>());
  const hydrated = useRef(new Set<string>());
  const request = async (path: string, init?: RequestInit) => {
    const response = await authFetch(`${getApiUrl()}${path}`, init);
    if (!response.ok) throw new Error(t('对话暂不可用，请重试或联系发布者。'));
    return response;
  };
  useEffect(() => {
    let cancelled = false;
    const targetId = standaloneFromPath(pathname)?.chatId;
    const st = useChatStore.getState();
    if (!targetId) {
      if (isAddressableChat(st.currentChatId) || !st.store.chats[st.currentChatId]) {
        st.newChat();
        createPublicDraft(info);
      }
      setLoading(false); setError('');
      return;
    }
    if (hydrated.current.has(targetId) && retry === 0) { st.adoptChatFromUrl(targetId); setLoading(false); setError(''); return; }
    setLoading(true); setError('');
    void (async () => {
      // Never display cached messages before checking this visitor's access.
      await apiRequest(`/v1/chats/${encodeURIComponent(targetId)}`);
      if (cancelled) return;
      st.adoptChatFromUrl(targetId);
      st.addBackendSessionId(targetId);
      if (!await reloadChatHistory(targetId)) throw new Error(t('加载失败'));
      if (cancelled) return;
      const questionIds = new Set((useUIStore.getState().pendingUserQuestions[targetId] || []).map(item => item.requestId));
      await Promise.allSettled([
        getPendingConfirm(targetId).then(({ confirms, designPick }) => {
          if (cancelled) return;
          useUIStore.getState().hydratePendingConfirmQueue(targetId, confirms);
          useUIStore.getState().setPendingDesignPick(targetId, designPick);
        }),
        getPendingUserQuestions(targetId).then(items => {
          if (cancelled) return;
          const ui = useUIStore.getState();
          ui.hydratePendingUserQuestionQueue(targetId,
            mergePendingUserQuestionRecovery(items, ui.pendingUserQuestions[targetId] || [], questionIds));
        }),
      ]);
      if (cancelled) return;
      hydrated.current.add(targetId);
      setLoading(false);
      const active = unwrapData<{ run_id?: string }>(await apiRequest(`/v1/chats/${encodeURIComponent(targetId)}/active-run`));
      if (cancelled || !active?.run_id || controllers.current.has(targetId)) return;
      const controller = new AbortController(); controllers.current.set(targetId, controller);
      st.addSendingChatId(targetId);
      try {
        const response = await request(`/v1/chats/stream/${encodeURIComponent(active.run_id)}?from=0`, { signal: controller.signal });
        await processChatStream(response, { chatId: targetId, enableThinking: true, signal: controller.signal });
      } finally { controllers.current.delete(targetId); st.removeSendingChatId(targetId); }
    })().catch(e => { if (!cancelled) { setLoading(false); setError(e instanceof Error ? e.message : t('加载失败')); } });
    return () => { cancelled = true; };
    // Resource identity is fixed for this mounted entry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname, retry]);
  useEffect(() => () => { for (const controller of controllers.current.values()) controller.abort(); }, []);

  const send = useCallback((text?: string) => {
    const st = useChatStore.getState();
    const id = st.currentChatId;
    const composer = readComposer(chatDraftKey(id));
    const content = (text ?? composer.input).trim();
    if (!content || st.sendingChatIds.has(id)) return;
    const controller = new AbortController(); controllers.current.set(id, controller);
    st.addSendingChatId(id);
    void (async () => {
      const attachments = await prepareChatAttachments(composer.uploadedFiles, composer.uploads,
        composer.importedSpaceFiles, getApiUrl(), id, info.kind === 'projects' ? info.id : undefined);
      composer.consume(composer);
      const mode = st.chatMode;
      const model = useModelCapabilitiesStore.getState();
      st.updateStore(prev => ({ chats: { ...prev.chats, [id]: {
        ...prev.chats[id], title: prev.chats[id].messages.length ? prev.chats[id].title : content.slice(0, 48),
        updatedAt: Date.now(), messages: [...prev.chats[id].messages, {
          uid: newMessageUid(), role: 'user', content, ts: Date.now(), ...(attachments.length ? { attachments } : {}),
        }],
      } }, order: prev.order.includes(id) ? prev.order : [id, ...prev.order] }));
      hydrated.current.add(id);
      const response = await request('/v1/agents/responses', { method: 'POST', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
          chat_id: id, message: content, stream: true, chat_mode: mode === 'turbo' ? 'fast' : mode,
          attachments: attachments.map(({ name, mime_type, file_id }) => ({ name, mime_type, file_id })),
          ...(model.capabilities.user_model_switch_enabled && model.selectedModelProviderId
            ? { model_provider_id: model.selectedModelProviderId } : {}),
          ...(info.kind === 'agents' ? { agent_id: info.id } : { project_id: info.id }),
        }),
      });
      await processChatStream(response, { chatId: id, enableThinking: true, signal: controller.signal });
      st.addBackendSessionId(id); st.addLoadedMsgId(id);
    })().catch(e => {
      if (!controller.signal.aborted) void message.error(e instanceof Error ? e.message : t('发送失败'));
    }).finally(() => { controllers.current.delete(id); st.removeSendingChatId(id); });
    // request uses the entry's immutable visitor transport.
  }, [info]);
  const abort = async () => {
    const st = useChatStore.getState();
    const runId = st.activeRuns[currentId]?.runId;
    try {
      if (!runId) return;
      await apiRequest(`/v1/chat-runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
      markRunCancelledByUser(runId);
      controllers.current.get(currentId)?.abort();
    } catch { void message.error(t('中止失败，请重试')); }
  };
  return { send, abort, loading, error, retry: () => setRetry(value => value + 1) };
}
