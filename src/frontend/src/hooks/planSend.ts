import { chatDraftKey, readComposer } from '../stores/composerStore';
import { prepareChatAttachments, type ChatAttachment } from '../utils/chatAttachments';
import { message } from 'antd';
import { t } from '../i18n';
import { generatePlanStream, updatePlanApi, executePlanStream } from '../api';
import { useChatStore, useCatalogStore, useModelCapabilitiesStore } from '../stores';
import { useProjectStore } from '../stores/projectStore';
import type { ChatItem, ChatMessage } from '../types';
import { ensureFullMessages } from './useChatInit';
import { newMessageUid } from '../utils/messageIdentity';
import { processPlanGenerateStream, processPlanExecuteStream, markPlanDecision, makePlanAppender } from './usePlanMode';


export async function sendPlanMode(
  effectiveApiUrl: string,
  abortControllersRef: React.MutableRefObject<Map<string, AbortController>>,
  generateSummary: (chatId: string) => Promise<void>,
  directMessage?: string,
  // suppressUserEcho: when the main agent automatically switches into plan mode it reuses this flow,
  // but no longer inserts a user bubble (the original user request that triggered this round is already in the session; directMessage is just the task text to be planned).
  opts: { suppressUserEcho?: boolean } = {},
) {
  const { sending, addSendingChatId, removeSendingChatId, currentChatId, updateStore, currentPlanId, setCurrentPlanId } = useChatStore.getState();
  const { catalog } = useCatalogStore.getState();
  const draft = readComposer(chatDraftKey(currentChatId));
  const { input, uploadedFiles, importedSpaceFiles } = draft;
  const msg = directMessage?.trim() || input.trim();
  if (!msg || sending) return;
  if (!effectiveApiUrl) {
    message.error(t('请先在设置中配置 API 地址。'));
    return;
  }

  // Fallback: after a refresh/session switch the in-memory currentPlanId may be lost (the live stream's
  // onSetCurrentPlanId is destroyed along with the old page, and recovery happened to miss the persistence window). In this case
  // retrieve the plan_id directly from the last assistant message in the current session that carries a preview plan segment,
  // otherwise "confirm execution" would be mistaken for a new round of generation.
  let effectivePlanId: string | null = currentPlanId || null;
  if (!effectivePlanId) {
    const msgs = useChatStore.getState().store.chats[currentChatId]?.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== 'assistant') continue;
      const planSeg = m.segments?.find(s => s.type === 'plan' && s.planData);
      if (!planSeg?.planData) continue;
      // Only accept plans still in preview (generated, not yet executed); stop as soon as the most recent plan segment is scanned.
      if (planSeg.planData.mode === 'preview' && planSeg.planData.planId) {
        effectivePlanId = planSeg.planData.planId;
      }
      break;
    }
    if (effectivePlanId) setCurrentPlanId(effectivePlanId);
  }

  const isConfirm = effectivePlanId && /^(确认执行|确认|执行|开始执行|yes|ok|确定)$/i.test(msg);

  const streamChatId = currentChatId;
  addSendingChatId(streamChatId);
  // New round: clear the previous round's settled plan bar
  useChatStore.getState().setPlanProgress(streamChatId, null);

  const projectId = useChatStore.getState().store.chats[currentChatId]?.projectId
    || useProjectStore.getState().currentProjectId || undefined;
  let attachments: ChatAttachment[];
  try {
    attachments = await prepareChatAttachments(
      uploadedFiles, draft.uploads, importedSpaceFiles, effectiveApiUrl, currentChatId, projectId,
    );
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('文件上传失败，请重试'));
    removeSendingChatId(streamChatId);
    return;
  }
  draft.consume(draft, directMessage ? ['invocation', 'attachments', 'references'] : undefined);

  const enabledMcpIds = (catalog.mcp || []).filter(x => x.enabled).map(x => String(x.id).trim()).filter(Boolean);
  const enabledSkillIds = (catalog.skills || []).filter(x => x.enabled).map(x => String(x.id).trim()).filter(Boolean);
  const enabledKbIds = (catalog.kb || []).filter(x => x.enabled).map(x => String(x.id).trim()).filter(Boolean);

  const userMsg: ChatMessage = {
    role: 'user', content: msg, isMarkdown: false, uid: newMessageUid(), ts: Date.now(),
    ...(attachments.length > 0 && {
      attachments: attachments.map(a => ({
        name: a.name, mime_type: a.mime_type, file_id: a.file_id, download_url: a.download_url, origin: a.origin,
      })),
    }),
  };
  if (!opts.suppressUserEcho) {
    updateStore((prev) => {
      const c = prev.chats[currentChatId];
      const nextChat: ChatItem = {
        ...(c || { id: currentChatId, title: '新对话', createdAt: Date.now(), updatedAt: Date.now(), messages: [], favorite: false, pinned: false, businessTopic: '综合咨询' }),
        messages: [...(c?.messages || []), userMsg],
        updatedAt: Date.now(),
        title: c?.title && c.title !== '新对话' ? c.title : msg.slice(0, 18) || '新对话',
      };
      return { chats: { ...prev.chats, [currentChatId]: nextChat }, order: [currentChatId, ...(prev.order || []).filter((x) => x !== currentChatId)] };
    });
  }

  const bubbleUid = newMessageUid();
  const appendAssistant = makePlanAppender(currentChatId, bubbleUid);
  appendAssistant('', true);

  let chatForHistory = useChatStore.getState().store.chats[currentChatId];
  // 计划模式要把整段历史带给后端。常规浏览只铺了最近一屏（见 useChatInit 的
  // MESSAGE_PAGE_SIZE），这里先补齐再取，别只把最近几轮当成全部上下文。
  if (useChatStore.getState().messagePaging[currentChatId]?.hasOlder) {
    await ensureFullMessages(currentChatId);
    chatForHistory = useChatStore.getState().store.chats[currentChatId];
  }
  const historyMessages: Array<{ role: string; content: string }> = [];
  if (chatForHistory?.messages) {
    for (const m of chatForHistory.messages) {
      if (m.uid === userMsg.uid) continue;
      if (m.content && (m.role === 'user' || m.role === 'assistant')) {
        historyMessages.push({ role: m.role, content: m.content });
      }
    }
  }

  const abortController = new AbortController();
  abortControllersRef.current.set(streamChatId, abortController);

  try {
    if (isConfirm && effectivePlanId) {
      // Phase 2: Execute confirmed plan
      markPlanDecision(currentChatId, effectivePlanId, 'confirmed');
      await updatePlanApi(effectivePlanId, { status: 'approved' }, currentChatId);
      const execResp = await executePlanStream(effectivePlanId, abortController.signal, enabledMcpIds, enabledSkillIds, enabledKbIds, currentChatId, historyMessages, undefined, projectId);
      if (!execResp.ok) throw new Error(t('计划执行请求失败: {status}', { status: execResp.status }));
      await processPlanExecuteStream(execResp, currentChatId, effectivePlanId, {
        bubbleUid,
        onSetCurrentPlanId: setCurrentPlanId,
        onAfterComplete: (cid) => { setTimeout(() => generateSummary(cid), 500); },
      });

    } else {
      // Phase 1: Generate plan
      const modelCaps = useModelCapabilitiesStore.getState();
      const selectedModelProviderId = modelCaps.capabilities.user_model_switch_enabled
        ? modelCaps.selectedModelProviderId
        : null;
      const genResp = await generatePlanStream(
        msg,
        'qwen',
        abortController.signal,
        enabledMcpIds,
        enabledSkillIds,
        enabledKbIds,
        currentChatId,
        historyMessages,
        attachments.map(({ name, mime_type, file_id }) => ({ name, mime_type, file_id })),
        undefined,
        projectId,
        selectedModelProviderId,
        opts.suppressUserEcho,
      );
      if (!genResp.ok) throw new Error(t('计划生成请求失败: {status}', { status: genResp.status }));
      const { planEvt } = await processPlanGenerateStream(genResp, currentChatId, {
        bubbleUid,
        onSetCurrentPlanId: setCurrentPlanId,
      });
      if (planEvt) {
        // The plan session now exists on the backend. Generate its model-written
        // conversation title as soon as the preview is ready instead of waiting
        // until the user eventually confirms and finishes the whole plan.
        useChatStore.getState().addBackendSessionId(streamChatId);
        useChatStore.getState().addLoadedMsgId(streamChatId);
        setTimeout(() => generateSummary(streamChatId), 500);
      }
    }

  } catch (e) {
    if (!(e instanceof Error && e.name === 'AbortError')) {
      appendAssistant(t('计划模式出错：{msg}', { msg: e instanceof Error ? e.message : String(e) }), false);
    }
  } finally {
    abortControllersRef.current.delete(streamChatId);
    removeSendingChatId(streamChatId);
    useChatStore.getState().clearActiveRun(streamChatId);
  }
}
