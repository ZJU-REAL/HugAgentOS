import { StreamDisconnectedError } from './chatStream/transport';
import { chatDraftKey, readComposer } from '../stores/composerStore';
import { prepareChatAttachments, type ChatAttachment } from '../utils/chatAttachments';
import { message } from 'antd';
import { t } from '../i18n';
import { authFetch, getFollowUpQuestions, isLocalProject, projectTargetHeaders, chatTargetHeaders, registerLocalChat } from '../api';
import { inferBusinessTopic } from '../utils/history';
import { resolveBatchModeActive, resolveSiteModeActive, resolveWorkflowModeActive } from '../utils/chatMode';
import { composeCommandMessage, seedChatTitle } from '../utils/projectCommands';
import { useChatStore, useCatalogStore, useChatModeStore, useUIStore, useModelCapabilitiesStore } from '../stores';
import { useProjectStore } from '../stores/projectStore';
import { isThinkingMode } from '../stores/chatStore';
import { processChatStream } from './chatStream';
import { captureChatInvocation, chatInvocationMessageProps, chatInvocationRequestFields, normalizeChatInvocation, type ChatInvocationContext } from '../utils/chatInvocation';
import { newMessageUid } from '../utils/messageIdentity';
import type { ChatItem, ChatMessage } from '../types';
import type { StreamingContext } from './streamingContext';

export function createStreamSend(ctx: Pick<StreamingContext, 'abortControllersRef' | 'effectiveApiUrl' | 'followUpAbortRef' | 'generateClassification' | 'generateSummary' | 'processChatStreamWithHandoffRecovery' | 'settleQueuedMessageAfterRun'>) {


  /** 流式期间用户手动重命名过、但当时后端会话尚未创建（PATCH 被跳过）——
   *  流结束、会话已在后端后补一次同步（问题13）。幂等，多调无害。 */
  function syncManualTitleToBackend(chatId: string) {
    const chat = useChatStore.getState().store.chats[chatId];
    if (!chat?.titleManuallySet || !chat.title || !ctx.effectiveApiUrl) return;
    void authFetch(`${ctx.effectiveApiUrl}/v1/chats/${chatId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: chat.title,
        metadata: {
          businessTopic: chat.businessTopic || '综合咨询',
          ...(chat.agentId ? { agent_id: chat.agentId } : {}),
          ...(chat.agentName ? { agent_name: chat.agentName } : {}),
          ...(chat.planChat ? { plan_chat: true } : {}),
          ...(chat.batchChat ? { batch_chat: true } : {}),
          ...(chat.workflowChat ? { workflow_chat: true } : {}),
          title_manually_set: true,
        },
      }),
    }).catch(() => { /* 下次流结束还会再试 */ });
  }


  async function send(directMessage?: string, invocationOverride?: ChatInvocationContext) {
    const { sending, addSendingChatId, removeSendingChatId, chatMode, modeSlug, currentChatId, updateStore, addBackendSessionId, addLoadedMsgId } = useChatStore.getState();
    const draft = readComposer(chatDraftKey(currentChatId));
    const { input, activeSkill, activePlugin, activeConnector, activeMention, activeCommand } = draft;
    // A queued replay owns its captured message, not the next turn currently being edited.
    const usesDraft = invocationOverride === undefined;
    const quotedFollowUp = usesDraft ? draft.quotedFollowUp : null;
    const referencedChats = usesDraft ? draft.referencedChats : [];
    const uploadedFiles = usesDraft ? draft.uploadedFiles : [];
    const importedSpaceFiles = usesDraft ? draft.importedSpaceFiles : [];
    const { catalog } = useCatalogStore.getState();

    // 命令 chip（/init）不产生编辑器文本，发送时在这里还原成命令原文；directMessage 是别处
    // 直接指定的整条消息，不受输入框里的 chip 影响。
    const currentCommand = directMessage ? null : activeCommand;
    const msg = directMessage?.trim() || composeCommandMessage(input, currentCommand);
    if (!msg || sending) return;
    if (!ctx.effectiveApiUrl) {
      message.error(t('请先在设置中配置 API 地址。'));
      useCatalogStore.getState().setPanel('settings');
      return;
    }

    const currentInvocation = invocationOverride === undefined
      ? captureChatInvocation({ activeSkill, activePlugin, activeConnector, activeMention })
      : normalizeChatInvocation(invocationOverride);
    const currentMention = currentInvocation.mention;

    // Keep the @name prefix for persisted history/display compatibility. The authoritative
    // routing key is mention_agent_id below, so the backend can bypass the main agent and run
    // the selected sub-agent directly without a name lookup or a second call_subagent spawn.
    const wireMsg = currentMention ? `@${currentMention.name} ${msg}` : msg;

    // Preserve the site conversation marker for UI/history. Working rules are provided
    // on demand by the sites plugin's site-builder skill, never spliced into user messages.
    const siteMode = resolveSiteModeActive(useChatStore.getState().store.chats[currentChatId], msg);

    // Snapshot the chat id — user may switch chats mid-stream, but this stream
    // continues writing to the chat it was started in.
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);
    // New send round: clear any leftover "pending confirm" queue from the previous round
    useUIStore.getState().clearPendingConfirm(streamChatId);
    // …and the previous round's settled plan bar (a new turn starts a fresh plan, if any)
    useChatStore.getState().setPlanProgress(streamChatId, null);

    const effectiveProjectId = useChatStore.getState().store.chats[currentChatId]?.projectId
      || useProjectStore.getState().currentProjectId || undefined;
    let attachments: ChatAttachment[];
    try {
      attachments = await prepareChatAttachments(
        uploadedFiles, draft.uploads, importedSpaceFiles, ctx.effectiveApiUrl, currentChatId, effectiveProjectId,
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('文件上传失败，请重试'));
      removeSendingChatId(streamChatId);
      return;
    }
    if (usesDraft) draft.consume(
      { ...draft, activeCommand: directMessage ? null : draft.activeCommand },
      directMessage ? ['invocation', 'attachments', 'references'] : undefined,
    );
    // After sending a message, auto-collapse the "prompt hub" sidebar
    if (useUIStore.getState().promptHubOpen) {
      useUIStore.getState().setPromptHubOpen(false);
    }

    const userMsg: ChatMessage = {
      role: 'user',
      content: msg,
      isMarkdown: false,
      uid: newMessageUid(),
      ts: Date.now(),
      ...(quotedFollowUp && {
        quotedFollowUp: {
          text: quotedFollowUp.text,
          ts: quotedFollowUp.ts,
        },
      }),
      ...(referencedChats.length > 0 && {
        referencedChats: referencedChats.map((c) => ({
          chat_id: c.chat_id,
          title: c.title,
          message_count: c.message_count,
          last_active_display: c.last_active_display,
        })),
      }),
      ...(attachments.length > 0 && {
        attachments: attachments.map(a => ({
          name: a.name,
          mime_type: a.mime_type,
          file_id: a.file_id,
          download_url: a.download_url,
          origin: a.origin,
        })),
      }),
      ...chatInvocationMessageProps(currentInvocation),
    };

    updateStore((prev) => {
      const c = prev.chats[currentChatId];
      const inferredTopic = c?.businessTopic && c.businessTopic !== '综合咨询' ? c.businessTopic : inferBusinessTopic(msg);
      const nextChat: ChatItem = {
        ...(c || {
          id: currentChatId,
          title: '新对话',
          createdAt: Date.now(),
          updatedAt: Date.now(),
          messages: [],
          favorite: false,
          pinned: false,
          businessTopic: '综合咨询',
        }),
        messages: [...(c?.messages || []), userMsg],
        updatedAt: Date.now(),
        title: c?.title && c.title !== '新对话' ? c.title : seedChatTitle(msg, '新对话'),
        businessTopic: inferredTopic,
        // 发送即落当前模式与思考强度：首条消息前 setModeSlug/setChatMode 没有记录
        // 可写，这里补上，刷新/切对话后模式位和强度档才恢复得回来。
        modeSlug,
        thinkingEffort: chatMode,
      };
      return {
        chats: { ...prev.chats, [currentChatId]: nextChat },
        order: [currentChatId, ...(prev.order || []).filter((x) => x !== currentChatId)],
      };
    });

    let streamOutcome: Awaited<ReturnType<typeof processChatStream>> | undefined;
    try {
      const enabledKbIds = (catalog.kb || [])
        .filter((x) => !!x.enabled)
        .map((x) => String(x.id).trim())
        .filter((x) => !!x);

      const abortController = new AbortController();
      ctx.abortControllersRef.current.set(streamChatId, abortController);

      const currentChat = useChatStore.getState().store.chats[currentChatId];
      const agentId = currentChat?.agentId || undefined;
      const batchChat = resolveBatchModeActive(currentChat);
      const workflowChat = resolveWorkflowModeActive(currentChat);
      const modelCaps = useModelCapabilitiesStore.getState();
      const selectedModelProviderId = modelCaps.capabilities.user_model_switch_enabled
        ? modelCaps.selectedModelProviderId
        : null;

      // 本地项目对话，或用户在运行位置选择器选了「本机」的普通对话 → 本机执行面。
      const runTargetLocal =
        (useChatStore.getState().store.chats[currentChatId] as { runTarget?: string } | undefined)
          ?.runTarget === 'local';
      if (isLocalProject(effectiveProjectId) || (!effectiveProjectId && runTargetLocal)) registerLocalChat(currentChatId);

      // 锁死强度的模式按模式默认档上行：切回历史对话恢复模式时，chatMode 可能
      // 还停在别的对话选的档位，不能把它带进锁档模式（显式选模式时 ChatModeSwitch
      // 也是切到默认档，这里保持同一语义）。
      const activeModeSpec = useChatModeStore.getState().modeOf(modeSlug);
      const wireChatMode = activeModeSpec.effort_locked ? activeModeSpec.default_effort : chatMode;
      const r = await authFetch(`${ctx.effectiveApiUrl}/v1/agents/responses`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(effectiveProjectId ? projectTargetHeaders(effectiveProjectId) : chatTargetHeaders(currentChatId)),
        },
        body: JSON.stringify({
          stream: true,
          chat_id: currentChatId,
          message: wireMsg,
          model_name: 'qwen',
          ...(selectedModelProviderId ? { model_provider_id: selectedModelProviderId } : {}),
          chat_mode: wireChatMode,
          mode_slug: modeSlug,
          attachments: attachments.map(({ name, mime_type, file_id }) => ({
            name,
            mime_type,
            file_id,
          })),
          enabled_kbs: enabledKbIds,
          ...(quotedFollowUp ? {
            quoted_follow_up: {
              text: quotedFollowUp.text,
              ts: quotedFollowUp.ts,
            },
          } : {}),
          // 只上行 ID：标题、概览等名片内容由后端按当前权限现查，前端传的不作数。
          ...(referencedChats.length > 0 ? {
            referenced_chats: referencedChats.map((c) => ({ chat_id: c.chat_id })),
          } : {}),
          ...(agentId ? { agent_id: agentId } : {}),
          ...chatInvocationRequestFields(currentInvocation),
          ...(batchChat ? { batch_chat: true } : {}),
          ...(workflowChat ? { workflow_chat: true } : {}),
          ...(siteMode ? { site_chat: true } : {}),
          // Project mount: read from the chat's own projectId (the frontend binds it when
          // creating/fetching the session). When the chat has no bound project, fall back to
          // useProjectStore.currentProjectId — this only applies to the first message sent while
          // the user is on the "project details" panel (chat freshly minted, not yet written
          // back); after switching to another chat, chat.projectId is the sole source of truth,
          // preventing store residue from polluting ordinary conversations.
          ...(effectiveProjectId ? { project_id: effectiveProjectId } : {}),
        }),
        signal: abortController.signal,
      });
      if (!r.ok || !r.body) throw new Error(await r.text());

      const outcome = await ctx.processChatStreamWithHandoffRecovery(r, streamChatId, {
        enableThinking: isThinkingMode(chatMode),
        signal: abortController.signal,
      });
      streamOutcome = outcome;

      addBackendSessionId(currentChatId);
      addLoadedMsgId(currentChatId);
      syncManualTitleToBackend(currentChatId);

      setTimeout(() => ctx.generateSummary(currentChatId), 500);
      setTimeout(() => ctx.generateClassification(currentChatId), 800);

      if (outcome.metaMessageId && outcome.metaFollowUps.length === 0) {
        const _pollChatId = currentChatId;
        const _pollMsgId = outcome.metaMessageId;

        // Supersede any prior polling still running for this chat (rare —
        // would only happen if a previous run somehow leaked).
        ctx.followUpAbortRef.current.get(_pollChatId)?.abort();
        const pollAc = new AbortController();
        ctx.followUpAbortRef.current.set(_pollChatId, pollAc);

        (async () => {
          const abortableDelay = (ms: number) => new Promise<void>((resolve, reject) => {
            const t = window.setTimeout(resolve, ms);
            const onAbort = () => {
              window.clearTimeout(t);
              reject(new DOMException('aborted', 'AbortError'));
            };
            if (pollAc.signal.aborted) return onAbort();
            pollAc.signal.addEventListener('abort', onAbort, { once: true });
          });

          try {
            await abortableDelay(4000);
            for (let attempt = 0; attempt < 5; attempt++) {
              if (pollAc.signal.aborted) return;
              if (attempt > 0) await abortableDelay(3000);
              try {
                const questions = await getFollowUpQuestions(_pollChatId, _pollMsgId);
                if (pollAc.signal.aborted) return;
                if (questions.length > 0) {
                  useChatStore.getState().updateStore((prev) => {
                    const c = prev.chats[_pollChatId];
                    if (!c) return { chats: prev.chats, order: prev.order };
                    const msgs = [...(c.messages || [])];
                    const idx = msgs.findIndex(
                      (m) => m.role === 'assistant' && m.messageId === _pollMsgId,
                    );
                    if (idx >= 0) {
                      msgs[idx] = { ...msgs[idx], followUpQuestions: questions };
                    }
                    // 必须推进 updatedAt：引导问题是流结束后轮询补写的，只有发起提问的
                    // 那个标签页会跑这段轮询。跨标签页合并按 chat 粒度取 updatedAt 严格
                    // 更大的一方（见 storage.mergeChatStores），不改时间戳这份带引导问题
                    // 的快照就永远赢不过另一个窗口手里的旧副本 —— 表现为同一段对话，
                    // 一个窗口有引导问题、另一个没有。
                    return {
                      chats: { ...prev.chats, [_pollChatId]: { ...c, messages: msgs, updatedAt: Date.now() } },
                      order: prev.order,
                    };
                  });
                  break;
                }
              } catch {
                // ignore single-attempt polling errors; AbortError will hit the outer catch
              }
            }
          } catch {
            // AbortError — silently exit
          } finally {
            // Only clean up if we're still the current controller; a newer
            // run may have replaced us via the supersede path above.
            if (ctx.followUpAbortRef.current.get(_pollChatId) === pollAc) {
              ctx.followUpAbortRef.current.delete(_pollChatId);
            }
          }
        })();
      }
    } catch (e) {
      if (!(e instanceof StreamDisconnectedError)) {
      useChatStore.getState().updateStore((prev) => {
        const c = prev.chats[currentChatId];
        if (!c) return { chats: prev.chats, order: prev.order };
        const msgs = [...(c.messages || [])];
        const last = msgs[msgs.length - 1];
        if (last?.role === 'assistant' && last.isStreaming) {
          // Also move still-running tools to a terminal state (same semantics as
          // finalizeRunningTools on the normal completion path) — otherwise ToolProgressInline,
          // which only looks at tool.status, would forever show "calling" with the timer ticking
          // after termination, and it persists across refreshes via localStorage.
          const finalizedTools = last.toolCalls?.map((tc) =>
            tc.status === 'running' ? { ...tc, status: 'success' as const } : tc,
          );
          msgs[msgs.length - 1] = { ...last, isStreaming: false, toolCalls: finalizedTools };
        }
        return { chats: { ...prev.chats, [currentChatId]: { ...c, messages: msgs } }, order: prev.order };
      });
      }
      if (e instanceof StreamDisconnectedError) {
        message.info(t('连接已中断，任务状态尚未同步'));
      } else if (!(e instanceof Error && e.name === 'AbortError')) {
        // Failed to fetch / TypeError usually means the backend is down / the SSE stream broke —
        // give one more hint than the generic error so the user knows it was an interruption, not a real failure.
        const raw = e instanceof Error ? e.message : String(e);
        const isNetworkError = /Failed to fetch|NetworkError|ERR_CONNECTION/i.test(raw);
        message.error(isNetworkError ? t('与服务端连接中断，请重新发送') : t('发送失败：{msg}', { msg: raw }));
      }
    } finally {
      ctx.abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
      // Clean up activeRun — the SSE has hit [DONE] / errored / been interrupted
      useChatStore.getState().clearActiveRun(streamChatId);
      ctx.settleQueuedMessageAfterRun(
        streamChatId,
        streamOutcome?.bubbleUid,
        streamOutcome?.settled === true,
      );
      // NOTE: do NOT clear draft attachments here. This round's
      // attachments were already cleared right after they were assembled
      // (before the request), so anything present now was uploaded by the
      // user DURING streaming for the next question — wiping it here made
      // those attachments silently vanish when the stream ended.
    }
  }
return { syncManualTitleToBackend, send };
}
