import { useRef } from 'react';
import { message } from 'antd';
import { t } from '../i18n';
import { authFetch, getFollowUpQuestions, regenerateMessage, editAndRegenerate, cancelChatRun, followChatRun, getActiveChatRun, cancelBatchPlan, getLoop } from '../api';
import { processPlanExecuteStream, processPlanGenerateStream } from './usePlanMode';
import { parseFileContent, parseSpaceFileContent, uploadFileToOSS } from '../utils/fileParser';
import { inferBusinessTopic } from '../utils/history';
import { useChatStore, useAuthStore, useCatalogStore, useFileStore, useUIStore, useBatchStore, useModelCapabilitiesStore } from '../stores';
import { useProjectStore } from '../stores/projectStore';
import { isThinkingMode } from '../stores/chatStore';
import { processChatStream } from './chatStream';
import { sendPlanMode } from './usePlanMode';
import { sendLoopMode, processLoopStream, continueLoop as continueLoopImpl } from './useLoopMode';
import { useLoopStore } from '../stores/loopStore';
import type { ChatItem, ChatMessage } from '../types';

export function useStreaming(
  effectiveApiUrl: string,
  generateSummary: (chatId: string) => Promise<void>,
  generateClassification: (chatId: string) => Promise<void>,
) {
  const fileUploadMap = useRef<Map<File, Promise<{ content: string; file_id: string; download_url: string }>>>(new Map());
  /** AbortControllers keyed by chat id — allows multiple chats to stream in parallel
   *  (e.g. user starts chat A, switches to new chat B, sends while A is still running). */
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  /** Separate AbortControllers for the post-stream follow-up question polling
   *  loop. The main `abortControllersRef` is cleared in the SSE `finally`
   *  block before polling starts, so we can't reuse it — without independent
   *  tracking the polling fires-and-forgets and survives chat switches /
   *  logouts as a memory-leaking ghost request. */
  const followUpAbortRef = useRef<Map<string, AbortController>>(new Map());
  /** Plan F short-term fix: dedupe which chats have already been shown the "session
   *  interrupted" toast. Each chat gets it once, so users switching back and forth between
   *  chats aren't spammed. The Set lives only in the current hook instance (reset on page
   *  refresh, which exactly matches the "new window should re-notify" semantics). */
  const interruptedNoticeShownRef = useRef<Set<string>>(new Set());

  /** Plan F short-term fix: when resume / an SSE error discovers the chat's run is already
   *  failed/cancelled, explicitly wind down the zombie streaming state in the UI — clear
   *  sendingChatIds and flag the last ``isStreaming=true`` assistant message false — and for
   *  the genuinely-interrupted case (failed) show the "previous session didn't finish due to a
   *  server restart, please resend" toast once.
   *
   *  The backend startup hook ``recover_orphan_runs`` already marks zombie runs failed and
   *  writes a terminal SSE event; but the legacy frontend ``resumeRunIfAny`` code
   *  early-returned on non-running/pending without winding down, leaving the last assistant
   *  bubble's streaming cursor spinning forever. */
  function cleanupZombieRunState(chatId: string, runStatus: string) {
    const store = useChatStore.getState();
    store.updateStore((prev) => {
      const c = prev.chats[chatId];
      if (!c) return { chats: prev.chats, order: prev.order };
      const msgs = [...(c.messages || [])];
      const last = msgs[msgs.length - 1];
      if (last?.role === 'assistant' && last.isStreaming) {
        msgs[msgs.length - 1] = { ...last, isStreaming: false };
      }
      return {
        chats: { ...prev.chats, [chatId]: { ...c, messages: msgs } },
        order: prev.order,
      };
    });
    store.removeSendingChatId(chatId);
    store.clearActiveRun(chatId);
    if (runStatus === 'failed' && !interruptedNoticeShownRef.current.has(chatId)) {
      interruptedNoticeShownRef.current.add(chatId);
      message.warning(t('上次会话因服务端重启未完成，请重新发起'));
    }
  }

  function handleFileSelect(
    e: React.ChangeEvent<HTMLInputElement>,
    fileInputRef: React.RefObject<HTMLInputElement | null>,
  ) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const newFiles = Array.from(files);
    if (fileInputRef.current) fileInputRef.current.value = '';

    const { setUploadedFiles, uploadedFiles } = useFileStore.getState();
    setUploadedFiles([...uploadedFiles, ...newFiles]);

    const curApiUrl = effectiveApiUrl ?? '';
    const curChatId = useChatStore.getState().currentChatId;

    for (const file of newFiles) {
      const { addUploadingFile, removeUploadingFile } = useFileStore.getState();
      addUploadingFile(file);
      const promise = Promise.all([
        parseFileContent(file, curApiUrl),
        uploadFileToOSS(file, curApiUrl, curChatId),
      ]).then(([content, { file_id, download_url }]) => {
        if (!file_id) message.warning(t('文件"{name}"上传失败，发送后将无法下载', { name: file.name }));
        return { content, file_id, download_url };
      })
        .catch(() => {
          message.warning(t('文件"{name}"上传失败，发送后将无法下载', { name: file.name }));
          return { content: '', file_id: '', download_url: '' };
        })
        .finally(() => { removeUploadingFile(file); });
      fileUploadMap.current.set(file, promise);
    }
  }

  function removeFile(index: number) {
    const { uploadedFiles, setUploadedFiles, removeUploadingFile } = useFileStore.getState();
    const removedFile = uploadedFiles[index];
    if (removedFile) {
      fileUploadMap.current.delete(removedFile);
      removeUploadingFile(removedFile);
    }
    setUploadedFiles(uploadedFiles.filter((_, i) => i !== index));
  }

  async function send(directMessage?: string) {
    const { input, setInput, sending, addSendingChatId, removeSendingChatId, chatMode, currentChatId, updateStore, addBackendSessionId, addLoadedMsgId, quotedFollowUp, setQuotedFollowUp, activeSkill, setActiveSkill, activePlugin, setActivePlugin, activeMention, setActiveMention } = useChatStore.getState();
    const { catalog } = useCatalogStore.getState();
    const { uploadedFiles, setUploadedFiles, setUploadingFiles, importedSpaceFiles, clearImportedSpaceFiles } = useFileStore.getState();

    let msg = directMessage?.trim() || input.trim();
    if (!msg || sending) return;
    if (!effectiveApiUrl) {
      message.error(t('请先在设置中配置 API 地址。'));
      useCatalogStore.getState().setPanel('settings');
      return;
    }

    const currentSkill = activeSkill;
    const currentPlugin = activePlugin;
    const currentMention = activeMention;

    // The "wire message" sent to the backend needs the @agent-name prefix; the routing layer's
    // _parse_agent_mentions uses it to route the message to the matching sub-agent. But the msg
    // used for display/storage stays clean — the @mention is rendered separately by the
    // mentionName badge on the bubble and must not repeat in the body (it would show twice).
    let wireMsg = currentMention ? `@${currentMention.name} ${msg}` : msg;

    // "Site building" conversation: append site-building guidance to the wire message (the msg
    // shown in the bubble stays clean; the @Sites marker is rendered separately by the input-box
    // chip). Branch on session state:
    //   - editing session (chat is bound to the site source workspace projectId) → guide toward
    //     incremental edits on the project folder's original files; forbid regenerating the whole
    //     site in /workspace/site (otherwise publish would pack the project folder and the new code would be dropped);
    //   - site-building session → guide toward generating a complete static site in the sandbox and publishing via publish_site.
    const siteChatItem = useChatStore.getState().store.chats[currentChatId];
    if (siteChatItem?.siteChat) {
      if (siteChatItem.projectId) {
        const folder = siteChatItem.projectName || '';
        const folderHint = folder ? `/myspace/${folder}/` : '/myspace/<项目文件夹>/';
        wireMsg =
          `${wireMsg}\n\n` +
          `[系统提示：这是「站点编辑」会话。该站点的全部源码已在项目文件夹 ${folderHint} 中，` +
          `请先用 glob 查看现有文件，然后**直接在原文件上增量修改**——不要在其他目录重新生成整站。` +
          `发布方式按工程类型分流：① 项目里**有 package.json**（React 构建型工程）→ 先跑` +
          ` init 脚本自愈依赖，再改 src/ 源码 → npm run build → publish_site 带` +
          ' src_dir=构建产物目录 + source_dir=项目文件夹（详见 site-builder 技能「编辑会话」一节），' +
          '**绝不能把源码目录直接当站点发布**；② 没有 package.json（静态站）→ 改完直接调 publish_site' +
          '（title 传站点名即可，src_dir 与 site_id 都不用传，后端按本会话绑定的项目自动定位' +
          '同一站点）。两种方式 URL 都不变、版本 +1，发布后把访问链接以 markdown 链接形式发给用户。]';
      } else {
        wireMsg =
          `${wireMsg}\n\n` +
          '[系统提示：这是「站点建站」会话。请在沙箱工作目录里生成完整的静态网站' +
          '（必须包含 index.html 入口，可包含多页面、CSS、JS、图片等），完成后调用 ' +
          'publish_site 工具发布，并把访问链接以 markdown 链接形式发给用户。' +
          '若用户要在已发布站点上继续修改，带上该站点的 site_id 重新发布（URL 不变、版本 +1）。]';
      }
    }

    // Snapshot the chat id — user may switch chats mid-stream, but this stream
    // continues writing to the chat it was started in.
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);
    // New send round: clear any leftover "pending confirm" queue from the previous round
    useUIStore.getState().clearPendingConfirm(streamChatId);
    if (!directMessage) setInput('');
    if (quotedFollowUp) setQuotedFollowUp(null);
    if (currentSkill) setActiveSkill(null);
    if (currentPlugin) setActivePlugin(null);
    if (currentMention) setActiveMention(null);
    // After sending a message, auto-collapse the "prompt hub" sidebar
    if (useUIStore.getState().promptHubOpen) {
      useUIStore.getState().setPromptHubOpen(false);
    }

    type Attachment = { name: string; content: string; mime_type: string; file_id: string; download_url: string };
    const attachments: Attachment[] = [];
    for (const file of uploadedFiles) {
      const promise = fileUploadMap.current.get(file);
      const result = promise ? await promise : { content: '', file_id: '', download_url: '' };
      attachments.push({ name: file.name, content: result.content, mime_type: file.type || '', file_id: result.file_id, download_url: result.download_url });
    }
    const spaceResults = await Promise.all(
      importedSpaceFiles.map(async (f) => ({
        name: f.name,
        content: await parseSpaceFileContent(f.download_url, f.name, f.mime_type, effectiveApiUrl ?? ''),
        mime_type: f.mime_type, file_id: f.file_id, download_url: f.download_url,
      })),
    );
    attachments.push(...spaceResults);
    setUploadedFiles([]);
    setUploadingFiles(new Set());
    clearImportedSpaceFiles();
    fileUploadMap.current.clear();

    const userMsg: ChatMessage = {
      role: 'user',
      content: msg,
      isMarkdown: false,
      ts: Date.now(),
      ...(quotedFollowUp && {
        quotedFollowUp: {
          text: quotedFollowUp.text,
          ts: quotedFollowUp.ts,
        },
      }),
      ...(attachments.length > 0 && {
        attachments: attachments.map(a => ({
          name: a.name,
          mime_type: a.mime_type,
          file_id: a.file_id,
          download_url: a.download_url,
        })),
      }),
      ...(currentSkill ? { skillId: currentSkill.id, skillName: currentSkill.name } : {}),
      ...(currentPlugin ? { pluginName: currentPlugin.name } : {}),
      ...(currentMention ? { mentionName: currentMention.name } : {}),
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
        title: c?.title && c.title !== '新对话' ? c.title : msg.slice(0, 18) || '新对话',
        businessTopic: inferredTopic,
      };
      return {
        chats: { ...prev.chats, [currentChatId]: nextChat },
        order: [currentChatId, ...(prev.order || []).filter((x) => x !== currentChatId)],
      };
    });

    try {
      const enabledKbIds = (catalog.kb || [])
        .filter((x) => !!x.enabled)
        .map((x) => String(x.id).trim())
        .filter((x) => !!x);

      const abortController = new AbortController();
      abortControllersRef.current.set(streamChatId, abortController);

      const currentChat = useChatStore.getState().store.chats[currentChatId];
      const agentId = (currentChat as any)?.agentId || undefined;
      const planChat = !!(currentChat as any)?.planChat;
      const batchChat = !!(currentChat as any)?.batchChat;
      const modelCaps = useModelCapabilitiesStore.getState();
      const selectedModelProviderId = modelCaps.capabilities.user_model_switch_enabled
        ? modelCaps.selectedModelProviderId
        : null;

      const r = await authFetch(`${effectiveApiUrl}/v1/chats/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: currentChatId,
          message: wireMsg,
          model_name: 'qwen',
          ...(selectedModelProviderId ? { model_provider_id: selectedModelProviderId } : {}),
          chat_mode: chatMode,
          attachments,
          enabled_kbs: enabledKbIds,
          ...(quotedFollowUp ? {
            quoted_follow_up: {
              text: quotedFollowUp.text,
              ts: quotedFollowUp.ts,
            },
          } : {}),
          ...(agentId ? { agent_id: agentId } : {}),
          ...(currentSkill ? { skill_id: currentSkill.id, skill_name: currentSkill.name } : {}),
          ...(currentPlugin && currentPlugin.skillIds.length > 0 ? { skill_ids: currentPlugin.skillIds } : {}),
          ...(currentPlugin && currentPlugin.mcpIds.length > 0 ? { mcp_ids: currentPlugin.mcpIds } : {}),
          ...(currentPlugin ? { plugin_name: currentPlugin.name } : {}),
          ...(currentMention ? { mention_name: currentMention.name } : {}),
          ...(planChat ? { plan_chat: true } : {}),
          ...(batchChat ? { batch_chat: true } : {}),
          // Project mount: read from the chat's own projectId (the frontend binds it when
          // creating/fetching the session). When the chat has no bound project, fall back to
          // useProjectStore.currentProjectId — this only applies to the first message sent while
          // the user is on the "project details" panel (chat freshly minted, not yet written
          // back); after switching to another chat, chat.projectId is the sole source of truth,
          // preventing store residue from polluting ordinary conversations.
          ...((() => {
            const chat = useChatStore.getState().store.chats[currentChatId];
            const fromChat = (chat as { projectId?: string } | undefined)?.projectId;
            if (fromChat) return { project_id: fromChat };
            const fromStore = useProjectStore.getState().currentProjectId;
            return fromStore ? { project_id: fromStore } : {};
          })()),
        }),
        signal: abortController.signal,
      });
      if (!r.ok || !r.body) throw new Error(await r.text());

      const outcome = await processChatStream(r, {
        chatId: streamChatId,
        enableThinking: isThinkingMode(chatMode),
      });

      addBackendSessionId(currentChatId);
      addLoadedMsgId(currentChatId);

      if (outcome.pendingPlanRedirect) {
        // Main agent hands off to plan mode: this turn (including the enter_plan_mode tool card)
        // is already persisted; now start the existing plan-mode pipeline with the to-be-planned
        // task. Deferred via setTimeout(0) — let this send's finally clear the sending state first
        // to avoid racing sendPlanMode's addSendingChatId; suppressUserEcho reuses the full flow
        // without inserting another user bubble (the original request is the task).
        const _planTask = outcome.pendingPlanRedirect;
        setTimeout(() => {
          useChatStore.getState().setPlanMode(true);
          void sendPlanMode(effectiveApiUrl, abortControllersRef, fileUploadMap, generateSummary, _planTask, { suppressUserEcho: true });
        }, 0);
      } else {
      setTimeout(() => generateSummary(currentChatId), 500);
      setTimeout(() => generateClassification(currentChatId), 800);

      if (outcome.metaMessageId && outcome.metaFollowUps.length === 0) {
        const _pollChatId = currentChatId;
        const _pollMsgId = outcome.metaMessageId;
        const _pollTs = outcome.placeholderTs;

        // Supersede any prior polling still running for this chat (rare —
        // would only happen if a previous run somehow leaked).
        followUpAbortRef.current.get(_pollChatId)?.abort();
        const pollAc = new AbortController();
        followUpAbortRef.current.set(_pollChatId, pollAc);

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
                      (m) => m.role === 'assistant' && (m.messageId === _pollMsgId || m.ts === _pollTs),
                    );
                    if (idx >= 0) {
                      msgs[idx] = { ...msgs[idx], followUpQuestions: questions };
                    }
                    return { chats: { ...prev.chats, [_pollChatId]: { ...c, messages: msgs } }, order: prev.order };
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
            if (followUpAbortRef.current.get(_pollChatId) === pollAc) {
              followUpAbortRef.current.delete(_pollChatId);
            }
          }
        })();
      }
      }
    } catch (e: any) {
      // Plan F short-term fix: every error path must flag the placeholder's isStreaming false;
      // otherwise when SSE throws due to a backend restart / network interruption, the last
      // assistant bubble's streaming cursor keeps spinning — users who don't see / miss the
      // toast will assume it's still working.
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
      if (e?.name !== 'AbortError') {
        // Failed to fetch / TypeError usually means the backend is down / the SSE stream broke —
        // give one more hint than the generic error so the user knows it was an interruption, not a real failure.
        const raw = e?.message || String(e);
        const isNetworkError = /Failed to fetch|NetworkError|ERR_CONNECTION/i.test(raw);
        message.error(isNetworkError ? t('与服务端连接中断，请重新发送') : t('发送失败：{msg}', { msg: raw }));
      }
    } finally {
      abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
      // Clean up activeRun — the SSE has hit [DONE] / errored / been interrupted
      useChatStore.getState().clearActiveRun(streamChatId);
      setUploadedFiles([]);
      setUploadingFiles(new Set());
      fileUploadMap.current.clear();
    }
  }

  /**
   * Shared by regenerate / edit / reconnect-replay / batch cancel-and-resume: exactly the same
   * unified stream processor as send() (processChatStream), just without creating a user message.
   *
   * `pendingNotice`: shown in the streaming bubble until the first real event arrives (the
   * confirm-then-continue scenario — MiniMax may buffer the whole turn for minutes; without it
   * the bubble is a dead spinner). Render-only, never enters the body / never persisted.
   * `enableThinking`: this run's thinking mode — the <think> stripper must start in the correct
   * initial phase, otherwise replayed/regenerated reasoning gets flattened into the visible body.
   */
  async function processRegenerateStream(
    response: Response,
    chatId: string,
    pendingNotice?: string,
    enableThinking: boolean = false,
  ) {
    await processChatStream(response, { chatId, enableThinking, pendingNotice });
    useChatStore.getState().addBackendSessionId(chatId);
    useChatStore.getState().addLoadedMsgId(chatId);
    setTimeout(() => generateSummary(chatId), 500);
    setTimeout(() => generateClassification(chatId), 800);
  }

  /** Regenerate the last assistant response */
  async function regenerate(messageIndex: number) {
    const { sending, addSendingChatId, removeSendingChatId, currentChatId, truncateMessagesFrom } = useChatStore.getState();
    if (sending) return;
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);

    const abortController = new AbortController();
    abortControllersRef.current.set(streamChatId, abortController);

    try {
      const chat = useChatStore.getState().store.chats[streamChatId];
      const targetMsg = chat?.messages[messageIndex];
      if (targetMsg) {
        truncateMessagesFrom(streamChatId, targetMsg.ts);
      }

      const r = await regenerateMessage(streamChatId, messageIndex, abortController.signal);
      if (!r.ok || !r.body) throw new Error(await r.text());

      await processRegenerateStream(r, streamChatId, undefined, isThinkingMode(useChatStore.getState().chatMode));
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        message.error(t('重新生成失败：{msg}', { msg: e?.message || String(e) }));
      }
    } finally {
      abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
    }
  }

  /** Edit a user message and regenerate */
  async function editAndResend(messageIndex: number, newContent: string) {
    const { sending, addSendingChatId, removeSendingChatId, currentChatId, truncateMessagesFrom, setEditingMessageTs } = useChatStore.getState();
    if (sending || !newContent.trim()) return;
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);
    setEditingMessageTs(null);

    const abortController = new AbortController();
    abortControllersRef.current.set(streamChatId, abortController);

    try {
      const chat = useChatStore.getState().store.chats[streamChatId];
      const targetMsg = chat?.messages[messageIndex];
      if (targetMsg) {
        truncateMessagesFrom(streamChatId, targetMsg.ts);
      }

      // Add the edited user message to local store
      const userMsg: ChatMessage = {
        role: 'user', content: newContent.trim(), isMarkdown: false, ts: Date.now(),
      };
      useChatStore.getState().updateStore((prev) => {
        const c = prev.chats[streamChatId];
        const msgs = [...(c?.messages || []), userMsg];
        return {
          chats: { ...prev.chats, [streamChatId]: { ...(c as any), messages: msgs, updatedAt: Date.now() } },
          order: [streamChatId, ...(prev.order || []).filter(x => x !== streamChatId)],
        };
      });

      const r = await editAndRegenerate(streamChatId, messageIndex, newContent.trim(), abortController.signal);
      if (!r.ok || !r.body) throw new Error(await r.text());

      await processRegenerateStream(r, streamChatId, undefined, isThinkingMode(useChatStore.getState().chatMode));
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        message.error(t('编辑重发失败：{msg}', { msg: e?.message || String(e) }));
      }
    } finally {
      abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
    }
  }

  async function smartSend(directMessage?: string) {
    const { planMode, loopMode } = useChatStore.getState();
    if (planMode) {
      return sendPlanMode(effectiveApiUrl, abortControllersRef, fileUploadMap, generateSummary, directMessage);
    }
    if (loopMode) {
      return sendLoopMode(abortControllersRef, directMessage);
    }
    return send(directMessage);
  }

  /** Abort the stream for a specific chat (defaults to the currently viewed chat).
   *  Actually kills the background task: first call /v1/chat-runs/{run_id}/cancel, then abort
   *  the local SSE connection. Also cancels any batch plans still executing on this chat —
   *  batch tasks have their own SSE stream and plan_id, are not in abortControllersRef, and
   *  must be handled separately.
   */
  function abort(chatId?: string) {
    const targetId = chatId || useChatStore.getState().currentChatId;
    const activeRun = useChatStore.getState().activeRuns[targetId];
    if (activeRun?.runId) {
      const uid = useAuthStore.getState().authUser?.user_id;
      // fire-and-forget: a failed cancel call must not block the local abort
      cancelChatRun(activeRun.runId, uid).catch(() => { /* noop — backend orphan recovery is the safety net */ });
    }
    const controller = abortControllersRef.current.get(targetId);
    if (controller) {
      controller.abort();
      abortControllersRef.current.delete(targetId);
    }

    // Autonomous loop: on stop, wind the chat's "plan bar" down from running to cancelled —
    // otherwise the replay path's AbortError is silently swallowed and the plan bar stays stuck
    // on "in progress" forever (bug fix).
    const _lp = useLoopStore.getState().livePlan;
    if (_lp && _lp.chatId === targetId && (_lp.status === 'running' || !_lp.status)) {
      useLoopStore.getState().finishLivePlan('cancelled');
    }

    // Cancel post-stream follow-up question polling for this chat —
    // the loop is fire-and-forget so without this it survives chat
    // switches as a leaked timer + pending fetch.
    const pollAc = followUpAbortRef.current.get(targetId);
    if (pollAc) {
      pollAc.abort();
      followUpAbortRef.current.delete(targetId);
    }

    // Cancel batch plans on this chat that are still running or awaiting confirmation
    const batchState = useBatchStore.getState();
    const activePlans = Object.values(batchState.plans).filter(
      (p) => p.meta.chat_id === targetId
        && (p.status === 'running' || p.status === 'awaiting_confirm'),
    );
    for (const p of activePlans) {
      // Backend: mark cancelled + cancel the in-flight runner task
      cancelBatchPlan(p.meta.plan_id).catch(() => { /* noop */ });
      // Frontend: immediately close the SSE fetch + update store state
      batchState.disconnectStream(p.meta.plan_id);
      batchState.cancel(p.meta.plan_id);
    }
  }

  /**
   * Resume an in-flight backend run after page refresh / chat switch.
   * Looks up the active run for this chat and pipes the SSE replay through
   * the same handler used for regenerate/edit. No-op if no active run.
   */
  async function reconcileLoopBar(
    chatId: string,
    active: Awaited<ReturnType<typeof getActiveChatRun>>,
  ) {
    const lp = useLoopStore.getState().livePlan;
    // Only handle a plan bar that belongs to this chat and still shows in-progress
    if (!lp || lp.chatId !== chatId || (lp.status && lp.status !== 'running')) return;
    // An active loop run will be followed by resumeRunIfAny's autonomous_loop branch → keep running
    if (active && active.run_id && active.kind === 'autonomous_loop'
      && (active.status === 'running' || active.status === 'pending')) return;
    // Otherwise the loop is no longer running — look up the real terminal state to wind down (treat as "cancelled/stopped" if unfindable)
    const TERMINAL = ['completed', 'cancelled', 'budget_exhausted', 'failed', 'awaiting_human'];
    let finalStatus = 'cancelled';
    if (lp.loopId) {
      try {
        const loop = await getLoop(lp.loopId);
        if (loop?.status && TERMINAL.includes(loop.status)) finalStatus = loop.status;
      } catch { /* if unfindable, wind down as cancelled */ }
    }
    // The user may have restarted the run during reconciliation; re-check to avoid a wrongful wind-down
    const cur = useLoopStore.getState().livePlan;
    if (cur && cur.chatId === chatId && (cur.status === 'running' || !cur.status)) {
      useLoopStore.getState().finishLivePlan(finalStatus);
    }
  }

  async function resumeRunIfAny(chatId: string) {
    const uid = useAuthStore.getState().authUser?.user_id;
    let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
    try {
      active = await getActiveChatRun(chatId, uid);
    } catch {
      return;
    }
    // Autonomous-loop plan-bar reconciliation: a plan bar restored from localStorage may still
    // read running while the backend run has already ended (stopped / finished / crashed). As
    // long as there's no "active loop run" that would be followed below, wind the plan bar down
    // to the real loop state, so it doesn't stay stuck on "in progress" forever after a refresh.
    await reconcileLoopBar(chatId, active);

    if (!active || !active.run_id) return;
    if (active.status !== 'running' && active.status !== 'pending') {
      // Run already terminal (failed / cancelled / completed) — the backend's
      // recover_orphan_runs marks zombie running runs failed on restart and writes a terminal
      // event. But the frontend may still have leftover sendingChatIds + a last assistant
      // message with isStreaming=true. Explicitly clean up this zombie UI state, and for the
      // failed path show a toast once so the user resends.
      cleanupZombieRunState(chatId, active.status);
      // Race safety net: a plan generate/execute run may finish exactly between "first message
      // fetch after refresh" and "the active-run lookup here". By then the live stream's
      // onSetCurrentPlanId died with the old page, and the plan wasn't yet persisted at first
      // message fetch → currentPlanId stays null, so the subsequent "confirm execute" gets
      // treated as a fresh generation round. Force a message rescan so useChatInit's history
      // scan restores currentPlanId from the persisted plan_snapshot(mode=preview).
      if (active.kind === 'plan_generate' || active.kind === 'plan_execute') {
        useChatStore.getState().removeLoadedMsgId(chatId);
        useChatStore.getState().bumpSessionLoadEpoch();
      }
      return;
    }

    // Re-read state at the latest moment — user may have started a fresh send
    // during the active-run round-trip.
    if (useChatStore.getState().sendingChatIds.has(chatId)) return;

    const { addSendingChatId, removeSendingChatId } = useChatStore.getState();

    useChatStore.getState().setActiveRun(chatId, {
      runId: active.run_id,
      messageId: active.message_id,
      lastOffset: active.last_event_offset || 0,
    });

    // Plan mode: live-replay the plan event stream (plan_step_* / tool_call / tool_result /
    // plan_complete), fully continuous with the pre-refresh progress.
    if (active.kind === 'plan_execute' || active.kind === 'plan_generate') {
      addSendingChatId(chatId);
      const ac = new AbortController();
      abortControllersRef.current.set(chatId, ac);
      try {
        const resp = await followChatRun(active.run_id, 0, ac.signal, uid);
        if (!resp.ok || !resp.body) return;
        if (active.kind === 'plan_execute' && active.plan_id) {
          await processPlanExecuteStream(resp, chatId, active.plan_id, {
            placeholderTs: Date.now(),
            onSetCurrentPlanId: useChatStore.getState().setCurrentPlanId,
            onAfterComplete: (cid) => {
              // After replay completes, refresh the message list, replacing client-built state
              // with the final message in the DB, ensuring the stop button, isStreaming flag, etc. wind down correctly.
              useChatStore.getState().removeLoadedMsgId(cid);
              useChatStore.getState().bumpSessionLoadEpoch();
            },
          });
        } else if (active.kind === 'plan_generate') {
          await processPlanGenerateStream(resp, chatId, {
            placeholderTs: Date.now(),
            onSetCurrentPlanId: useChatStore.getState().setCurrentPlanId,
          });
          // Also refresh after generate completes: pick up the DB-persisted assistant message + plan_snapshot
          useChatStore.getState().removeLoadedMsgId(chatId);
          useChatStore.getState().bumpSessionLoadEpoch();
        }
      } catch (e: any) {
        if (e?.name !== 'AbortError') {
          // Replay-failure safety net: refresh the message list (the task may have already finished in the DB)
          useChatStore.getState().removeLoadedMsgId(chatId);
          useChatStore.getState().bumpSessionLoadEpoch();
        }
      } finally {
        abortControllersRef.current.delete(chatId);
        removeSendingChatId(chatId);
        useChatStore.getState().clearActiveRun(chatId);
      }
      return;
    }

    // Autonomous-loop replay: full replay from offset 0, both restoring the worker's body/tool
    // bubbles and rebuilding the "plan bar" above the input box
    // (loop_plan/iteration_started/requirement_passed).
    if (active.kind === 'autonomous_loop') {
      addSendingChatId(chatId);
      // The backend's incrementally persisted "in progress" assistant message is already shown by
      // history loading; the replay rebuilds the same bubble in full from offset 0, so keeping
      // both would duplicate it. Remove the trailing assistant placeholder before rebuilding and
      // let the replay redraw it (when the run is dead and there's no replay, this path isn't
      // taken and the last progress is kept untouched).
      const _chat = useChatStore.getState().store.chats[chatId];
      const _msgs = _chat?.messages;
      if (_msgs && _msgs.length > 0 && _msgs[_msgs.length - 1]?.role === 'assistant') {
        useChatStore.getState().truncateMessagesFrom(chatId, _msgs[_msgs.length - 1].ts);
      }
      const ac = new AbortController();
      abortControllersRef.current.set(chatId, ac);
      try {
        const resp = await followChatRun(active.run_id, 0, ac.signal, uid);
        if (resp.ok && resp.body) await processLoopStream(resp, chatId, !!active.enable_thinking);
      } catch (e: any) {
        if (e?.name !== 'AbortError') { /* replay failure is silent — the final message arrives with the next refresh */ }
      } finally {
        abortControllersRef.current.delete(chatId);
        removeSendingChatId(chatId);
        useChatStore.getState().clearActiveRun(chatId);
      }
      return;
    }

    addSendingChatId(chatId);

    const abortController = new AbortController();
    abortControllersRef.current.set(chatId, abortController);

    try {
      const r = await followChatRun(active.run_id, active.last_event_offset || 0, abortController.signal, uid);
      if (!r.ok || !r.body) return;
      await processRegenerateStream(r, chatId, undefined, !!active.enable_thinking);
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        // Resume failure is handled silently — UX-wise it's equivalent to "the task runs in the background and the final message arrives via the next refresh"
      }
    } finally {
      abortControllersRef.current.delete(chatId);
      removeSendingChatId(chatId);
      useChatStore.getState().clearActiveRun(chatId);
    }
  }

  /** Cancel a pending batch plan and re-stream the original user message
   *  with batch_plan disabled, so the agent answers via ordinary tools.
   *
   *  The backend endpoint (POST /v1/batch/{plan_id}/cancel-and-resume):
   *    1. marks the plan cancelled
   *    2. deletes the assistant turn that triggered batch_plan
   *    3. re-streams the user message with disable_batch_plan=true
   *
   *  Frontend mirrors the assistant-turn deletion in chatStore so the UI
   *  reflects the same state, then consumes the SSE via the regenerate
   *  pipeline (since the response shape is identical).
   */
  async function cancelAndResumeBatch(planId: string, chatId: string) {
    const { addSendingChatId, removeSendingChatId, truncateMessagesFrom } =
      useChatStore.getState();
    addSendingChatId(chatId);

    // Drop the dangling empty assistant turn from the local store. We pick
    // the latest assistant message — the backend does the same lookup
    // server-side so the two stay in sync.
    const chat = useChatStore.getState().store.chats[chatId];
    if (chat?.messages?.length) {
      for (let i = chat.messages.length - 1; i >= 0; i--) {
        const m = chat.messages[i];
        if (m.role === 'assistant') {
          truncateMessagesFrom(chatId, m.ts);
          break;
        }
      }
    }

    const abortController = new AbortController();
    abortControllersRef.current.set(chatId, abortController);

    try {
      const r = await authFetch(
        `${effectiveApiUrl}/v1/batch/${encodeURIComponent(planId)}/cancel-and-resume`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: abortController.signal,
        },
      );
      if (!r.ok || !r.body) {
        throw new Error(await r.text() || `cancel-and-resume failed: ${r.status}`);
      }
      // The endpoint streams the same SSE shape as /chats/regenerate, so
      // we can reuse the existing consumer.
      await processRegenerateStream(r, chatId, undefined, isThinkingMode(useChatStore.getState().chatMode));
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        message.error(t('取消批量并继续失败：{msg}', { msg: e?.message || String(e) }));
        throw e;
      }
    } finally {
      abortControllersRef.current.delete(chatId);
      removeSendingChatId(chatId);
    }
  }

  function continueLoop(chatId?: string) {
    return continueLoopImpl(abortControllersRef, chatId);
  }

  return { send: smartSend, abort, handleFileSelect, removeFile, fileUploadMap, regenerate, editAndResend, resumeRunIfAny, cancelAndResumeBatch, continueLoop };
}
