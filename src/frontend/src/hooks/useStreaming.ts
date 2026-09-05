import { useEffect, useRef } from 'react';
import { Modal, message } from 'antd';
import { t } from '../i18n';
import { authFetch, getFollowUpQuestions, regenerateMessage, editAndRegenerate, cancelChatRun, steerChatRun, getChatRunSteers, withdrawChatRunSteer, followChatRun, getActiveChatRun, cancelBatchPlan, cancelPlanApi, getLoop, UPLOAD_MAX_BYTES, UPLOAD_MAX_MB, isLocalProject, projectTargetHeaders, chatTargetHeaders, registerLocalChat } from '../api';
import { processPlanExecuteStream, processPlanGenerateStream } from './usePlanMode';
import { uploadFileToOSS } from '../utils/fileParser';
import { inferBusinessTopic } from '../utils/history';
import { resolveBatchModeActive, resolveWorkflowModeActive } from '../utils/chatMode';
import { useChatStore, useAuthStore, useCatalogStore, useChatModeStore, useFileStore, useUIStore, useBatchStore, useModelCapabilitiesStore } from '../stores';
import { useProjectStore } from '../stores/projectStore';
import { isThinkingMode } from '../stores/chatStore';
import { processChatStream, getStreamActivityTs, hasStreamedRun, isRunCancelledByUser, markRunCancelledByUser } from './chatStream';
import { reloadChatHistory } from './useChatInit';
import { parseAppliedQueueHandoff, type QueuedRunHandoff } from '../utils/streamHandoff';
import {
  captureChatInvocation,
  chatInvocationMessageProps,
  chatInvocationRequestFields,
  createQueuedChatTurn,
  hasChatInvocation,
  normalizeChatInvocation,
  queuedChatInvocation,
  type ChatInvocationContext,
} from '../utils/chatInvocation';
import { sendPlanMode } from './usePlanMode';
import { sendLoopMode, processLoopStream, continueLoop as continueLoopImpl } from './useLoopMode';
import { useLoopStore } from '../stores/loopStore';
import { hasUnclosedThink } from '../utils/segments';
import type { ChatItem, ChatMessage } from '../types';
import type { QueuedChatMessage } from '../stores/chatStore';

export function useStreaming(
  effectiveApiUrl: string,
  generateSummary: (chatId: string) => Promise<void>,
  generateClassification: (chatId: string) => Promise<void>,
) {
  const fileUploadMap = useRef<Map<File, Promise<{ file_id: string; download_url: string }>>>(new Map());
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
    const picked = Array.from(files);
    if (fileInputRef.current) fileInputRef.current.value = '';

    // 超限的文件当场拦下并说明上限：以前既不校验、上传失败也被 catch 吞掉，
    // 挑一个 90MB 的文件会「安静地」挂在输入框上，用户完全不知道它没传上去（问题 42）。
    // 上限与 nginx 的 client_max_body_size 同一个来源（VITE_UPLOAD_MAX_MB）。
    const oversized = picked.filter((f) => f.size > UPLOAD_MAX_BYTES);
    const newFiles = picked.filter((f) => f.size <= UPLOAD_MAX_BYTES);
    if (oversized.length > 0) {
      message.error(
        oversized.length === 1
          ? t('「{name}」超过 {n} MB，无法上传', { name: oversized[0].name, n: UPLOAD_MAX_MB })
          : t('{k} 个文件超过 {n} MB，已跳过', { k: oversized.length, n: UPLOAD_MAX_MB }),
      );
    }
    if (newFiles.length === 0) return;

    const { setUploadedFiles, uploadedFiles } = useFileStore.getState();
    setUploadedFiles([...uploadedFiles, ...newFiles]);

    const curApiUrl = effectiveApiUrl ?? '';
    const curChatId = useChatStore.getState().currentChatId;

    for (const file of newFiles) {
      const { addUploadingFile, removeUploadingFile } = useFileStore.getState();
      addUploadingFile(file);
      const promise = uploadFileToOSS(file, curApiUrl, curChatId)
        .then((res) => {
          // uploadFileToOSS 失败时返回空 file_id 而不是抛错。以前这里不看返回值，
          // 附件就静静地停在输入框上、实际根本没传上去，发送时也不会带上。
          if (!res.file_id) {
            message.error(t('「{name}」上传失败，请重试', { name: file.name }));
          }
          return res;
        })
        .catch(() => {
          message.error(t('「{name}」上传失败，请重试', { name: file.name }));
          return { file_id: '', download_url: '' };
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

  function queueDuringRun(
    directMessage?: string,
    invocationOverride?: ChatInvocationContext,
  ) {
    const state = useChatStore.getState();
    const content = (directMessage ?? state.input).trim();
    if (!content) return;
    const chat = state.currentChat();
    if (state.planMode || state.loopMode || resolveBatchModeActive(chat)) {
      message.info(t('当前运行模式暂不支持追加消息'));
      return;
    }
    if (state.queuedMessages[state.currentChatId]) {
      message.info(t('已有一条待发送消息，请先编辑或删除'));
      return;
    }
    const queued: QueuedChatMessage = createQueuedChatTurn({
      id: `steer_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
      content,
      createdAt: Date.now(),
      source: state,
      invocationOverride,
    });
    state.setQueuedMessage(state.currentChatId, queued);
    if (directMessage === undefined) state.setInput('');
  }

  function sendQueuedAsNextTurn(chatId: string, queued: QueuedChatMessage) {
    const store = useChatStore.getState();
    const staleController = abortControllersRef.current.get(chatId);
    if (staleController) {
      staleController.abort();
      abortControllersRef.current.delete(chatId);
    }
    store.removeSendingChatId(chatId);
    store.clearActiveRun(chatId);
    store.setQueuedMessage(chatId, null);

    if (store.currentChatId !== chatId) {
      store.setQueuedMessage(chatId, { ...queued, status: 'queued' });
      return;
    }

    window.setTimeout(() => {
      const latest = useChatStore.getState();
      if (latest.currentChatId !== chatId || latest.sendingChatIds.has(chatId)) {
        latest.setQueuedMessage(chatId, { ...queued, status: 'queued' });
        return;
      }
      void smartSend(queued.content, queuedChatInvocation(queued));
    }, 0);
  }

  function settleQueuedMessageAfterRun(
    chatId: string,
    assistantTs?: number,
    autoSend = true,
  ) {
    const store = useChatStore.getState();
    const queued = store.queuedMessages[chatId];
    if (!queued) return;

    if (queued.status === 'applied') {
      if (queued.appliedMessageId) {
        commitAppliedQueuedMessage(chatId, queued, assistantTs);
      } else {
        // A restored durable card has no local SSE message id. Its user turn
        // is already committed in the DB, so reload history instead of
        // inventing a duplicate local message.
        void reloadChatHistory(chatId);
      }
      store.setQueuedMessage(chatId, null);
      return;
    }

    // The backend still owns an accepted/claimed durable instruction. Do not
    // turn it into a new local send merely because the source SSE ended.
    if (queued.status === 'steering' && queued.targetRunId) return;

    if (autoSend && store.currentChatId === chatId) {
      sendQueuedAsNextTurn(chatId, queued);
      return;
    }

    if (queued.status === 'steering') {
      store.updateQueuedMessage(chatId, (current) => ({
        ...current,
        status: 'queued',
      }));
    }
  }

  async function activateQueuedMessage(chatId?: string) {
    const state = useChatStore.getState();
    const targetId = chatId || state.currentChatId;
    const queued = state.queuedMessages[targetId];
    if (!queued || queued.status === 'applied') return;

    if (!state.sendingChatIds.has(targetId)) {
      state.setQueuedMessage(targetId, null);
      if (state.currentChatId === targetId) {
        await smartSend(queued.content, queuedChatInvocation(queued));
      }
      return;
    }

    const activeRun = state.activeRuns[targetId];
    if (!activeRun?.runId) {
      message.info(t('任务正在启动，请稍后再试'));
      return;
    }

    // The visible answer may have finished a moment before the local SSE
    // finally block clears `sendingChatIds`. Confirm the run is still live so
    // an instruction is not queued against a run that has no next tool call.
    let liveRun: Awaited<ReturnType<typeof getActiveChatRun>> | undefined;
    try {
      liveRun = await getActiveChatRun(
        targetId,
        useAuthStore.getState().authUser?.user_id,
      );
    } catch {
      // A transient probe failure should not turn a live steer into a duplicate
      // ordinary message; continue with the locally tracked run.
    }
    const currentQueued = useChatStore.getState().queuedMessages[targetId];
    if (!currentQueued || currentQueued.id !== queued.id) {
      // The old stream may have settled and started this queued message while
      // the active-run probe was in flight. Do not cancel or send it twice.
      return;
    }
    if (liveRun === null) {
      sendQueuedAsNextTurn(targetId, currentQueued);
      return;
    }

    // The current AgentScope executor has already assembled its skills/tools.
    // Referenced turns must start through the ordinary chat endpoint after this
    // run completes so the backend can validate and assemble the requested capability.
    if (hasChatInvocation(currentQueued.invocation)) {
      message.info(t('带引用的消息将在当前任务结束后发送'));
      return;
    }

    const runId = liveRun?.run_id || activeRun.runId;
    if (liveRun?.run_id && liveRun.run_id !== activeRun.runId) {
      state.setActiveRun(targetId, {
        runId: liveRun.run_id,
        messageId: liveRun.message_id,
        lastOffset: liveRun.last_event_offset || 0,
      });
    }

    state.updateQueuedMessage(targetId, (current) => ({
      ...current,
      status: 'steering',
      targetRunId: runId,
    }));
    try {
      const accepted = await steerChatRun(runId, currentQueued.id, currentQueued.content, targetId);
      useChatStore.getState().updateQueuedMessage(targetId, (current) => ({
        ...current,
        status: accepted.status === 'applied' ? 'applied' : 'steering',
        targetRunId: runId,
        durableStatus: accepted.status,
      }));
      // The POST response can be lost after durable acceptance. Query the
      // authoritative queue so the card reflects server state instead of
      // guessing from transport success alone.
      await reconcileDurableSteerQueue(targetId, runId);
    } catch (error) {
      // A transport error can happen after the database accepted the request.
      // Reconcile the stable steer id before making the card retryable.
      await reconcileDurableSteerQueue(targetId, runId);
      const reconciled = useChatStore.getState().queuedMessages[targetId];
      if (reconciled?.durableStatus) return;
      let stillLive: Awaited<ReturnType<typeof getActiveChatRun>> | undefined;
      try {
        stillLive = await getActiveChatRun(
          targetId,
          useAuthStore.getState().authUser?.user_id,
        );
      } catch {
        // Keep the queued card actionable when the status probe also fails.
      }
      if (stillLive === null) {
        const latestQueued = useChatStore.getState().queuedMessages[targetId];
        if (latestQueued) sendQueuedAsNextTurn(targetId, latestQueued);
        return;
      }
      useChatStore.getState().updateQueuedMessage(targetId, (current) => ({
        ...current,
        status: 'queued',
        targetRunId: undefined,
        durableStatus: undefined,
      }));
      message.error(t('立即开始失败：{msg}', { msg: (error as Error).message || String(error) }));
    }
  }

  async function discardQueuedMessage(chatId?: string) {
    const state = useChatStore.getState();
    const targetId = chatId || state.currentChatId;
    const queued = state.queuedMessages[targetId];
    if (!queued || queued.status === 'applied') return;
    const activeRun = state.activeRuns[targetId];
    const durableRunId = queued.targetRunId || activeRun?.runId;
    if (queued.status === 'steering' && durableRunId) {
      try {
        const removed = await withdrawChatRunSteer(durableRunId, queued.id, targetId);
        if (!removed) {
          message.info(t('指令已经生效，无法撤回'));
          return;
        }
      } catch (error) {
        message.error(t('撤回失败：{msg}', { msg: (error as Error).message || String(error) }));
        return;
      }
    }
    useChatStore.getState().setQueuedMessage(targetId, null);
  }

  /** Reconcile a restored queue card with the database-backed five-state queue. */
  async function reconcileDurableSteerQueue(chatId: string, runId: string) {
    const queued = useChatStore.getState().queuedMessages[chatId];
    if (!queued) return;
    try {
      const items = await getChatRunSteers(runId, chatId);
      const durable = items.find((item) => item.steer_id === queued.id);
      if (!durable) return;
      if (durable.status === 'applied') {
        useChatStore.getState().updateQueuedMessage(chatId, (current) => ({
          ...current,
          status: 'applied',
          targetRunId: runId,
          durableStatus: 'applied',
        }));
      } else if (durable.status === 'accepted' || durable.status === 'claimed') {
        useChatStore.getState().updateQueuedMessage(chatId, (current) => ({
          ...current,
          status: 'steering',
          targetRunId: runId,
          durableStatus: durable.status,
        }));
      } else {
        // cancelled / superseded are no longer owned by the backend worker;
        // restore an editable local card instead of silently dropping text.
        useChatStore.getState().updateQueuedMessage(chatId, (current) => ({
          ...current,
          id: `steer_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
          status: 'queued',
          targetRunId: undefined,
          durableStatus: undefined,
        }));
      }
    } catch {
      // A status-read outage does not change whether the instruction was
      // accepted. Keep the restored card untouched and retry on next resume.
    }
  }

  function commitAppliedQueuedMessage(
    chatId: string,
    queued: QueuedChatMessage,
    assistantTs?: number,
  ) {
    useChatStore.getState().updateStore((prev) => {
      const chat = prev.chats[chatId];
      if (!chat) return prev;
      const messages = [...chat.messages];
      if (queued.appliedMessageId && messages.some((item) => item.messageId === queued.appliedMessageId)) {
        return prev;
      }
      const userMessage: ChatMessage = {
        role: 'user',
        content: queued.content,
        isMarkdown: false,
        ts: Date.now(),
        messageId: queued.appliedMessageId,
        ...chatInvocationMessageProps(normalizeChatInvocation(queued.invocation)),
      };
      let assistantIndex = assistantTs === undefined
        ? -1
        : messages.findIndex((item) => item.role === 'assistant' && item.ts === assistantTs);
      if (assistantIndex < 0) {
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          if (messages[index].role === 'assistant') {
            assistantIndex = index;
            break;
          }
        }
      }
      messages.splice(assistantIndex >= 0 ? assistantIndex : messages.length, 0, userMessage);
      return {
        ...prev,
        chats: {
          ...prev.chats,
          [chatId]: { ...chat, messages, updatedAt: Date.now() },
        },
      };
    });
  }

  /** 流式期间用户手动重命名过、但当时后端会话尚未创建（PATCH 被跳过）——
   *  流结束、会话已在后端后补一次同步（问题13）。幂等，多调无害。 */
  function syncManualTitleToBackend(chatId: string) {
    const chat = useChatStore.getState().store.chats[chatId];
    if (!chat?.titleManuallySet || !chat.title || !effectiveApiUrl) return;
    void authFetch(`${effectiveApiUrl}/v1/chats/${chatId}`, {
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
    const { input, setInput, sending, addSendingChatId, removeSendingChatId, chatMode, currentChatId, updateStore, addBackendSessionId, addLoadedMsgId, quotedFollowUp, setQuotedFollowUp, activeSkill, setActiveSkill, activePlugin, setActivePlugin, activeConnector, setActiveConnector, activeMention, setActiveMention } = useChatStore.getState();
    const { catalog } = useCatalogStore.getState();
    const { uploadedFiles, setUploadedFiles, setUploadingFiles, importedSpaceFiles, clearImportedSpaceFiles } = useFileStore.getState();

    const msg = directMessage?.trim() || input.trim();
    if (!msg || sending) return;
    if (!effectiveApiUrl) {
      message.error(t('请先在设置中配置 API 地址。'));
      useCatalogStore.getState().setPanel('settings');
      return;
    }

    const currentInvocation = invocationOverride === undefined
      ? captureChatInvocation({ activeSkill, activePlugin, activeConnector, activeMention })
      : normalizeChatInvocation(invocationOverride);
    const currentSkill = currentInvocation.skill;
    const currentPlugin = currentInvocation.plugin;
    const currentConnector = currentInvocation.connector;
    const currentMention = currentInvocation.mention;

    // Keep the @name prefix for persisted history/display compatibility. The authoritative
    // routing key is mention_agent_id below, so the backend can bypass the main agent and run
    // the selected sub-agent directly without a name lookup or a second call_subagent spawn.
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
    // …and the previous round's settled plan bar (a new turn starts a fresh plan, if any)
    useChatStore.getState().setPlanProgress(streamChatId, null);

    type Attachment = { name: string; mime_type: string; file_id: string; download_url: string };
    const attachments: Attachment[] = [];
    const failedUploads: string[] = [];
    for (const file of uploadedFiles) {
      const promise = fileUploadMap.current.get(file);
      const result = promise ? await promise : { file_id: '', download_url: '' };
      if (!result.file_id) {
        failedUploads.push(file.name);
        continue;
      }
      attachments.push({ name: file.name, mime_type: file.type || '', file_id: result.file_id, download_url: result.download_url });
    }
    if (failedUploads.length > 0) {
      message.error(t('文件上传失败，请移除后重试：{names}', { names: failedUploads.join('、') }));
      removeSendingChatId(streamChatId);
      return;
    }
    if (!directMessage) setInput('');
    if (quotedFollowUp) setQuotedFollowUp(null);
    if (currentSkill) setActiveSkill(null);
    if (currentPlugin) setActivePlugin(null);
    if (currentConnector) setActiveConnector(null);
    if (currentMention) setActiveMention(null);
    // After sending a message, auto-collapse the "prompt hub" sidebar
    if (useUIStore.getState().promptHubOpen) {
      useUIStore.getState().setPromptHubOpen(false);
    }
    const spaceResults = importedSpaceFiles.map((f) => ({
      name: f.name,
      mime_type: f.mime_type,
      file_id: f.file_id,
      download_url: f.download_url,
    }));
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
        title: c?.title && c.title !== '新对话' ? c.title : msg.slice(0, 18) || '新对话',
        businessTopic: inferredTopic,
        // 发送即落当前模式与思考强度：首条消息前 setModeSlug/setChatMode 没有记录
        // 可写，这里补上，刷新/切对话后模式位和强度档才恢复得回来。
        modeSlug: useChatStore.getState().modeSlug,
        thinkingEffort: useChatStore.getState().chatMode,
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
      abortControllersRef.current.set(streamChatId, abortController);

      const currentChat = useChatStore.getState().store.chats[currentChatId];
      const agentId = (currentChat as any)?.agentId || undefined;
      const batchChat = resolveBatchModeActive(currentChat);
      const workflowChat = resolveWorkflowModeActive(currentChat);
      const modelCaps = useModelCapabilitiesStore.getState();
      const selectedModelProviderId = modelCaps.capabilities.user_model_switch_enabled
        ? modelCaps.selectedModelProviderId
        : null;

      // 混合路由：项目挂载 ID 提前算好——既进请求体，也决定该对话走云端还是本机执行面。
      const effectiveProjectId = (() => {
        const chat = useChatStore.getState().store.chats[currentChatId];
        const fromChat = (chat as { projectId?: string } | undefined)?.projectId;
        if (fromChat) return fromChat;
        return useProjectStore.getState().currentProjectId || undefined;
      })();
      // 本地项目对话，或用户在运行位置选择器选了「本机」的普通对话 → 本机执行面。
      const runTargetLocal =
        (useChatStore.getState().store.chats[currentChatId] as { runTarget?: string } | undefined)
          ?.runTarget === 'local';
      if (isLocalProject(effectiveProjectId) || runTargetLocal) registerLocalChat(currentChatId);

      // 锁死强度的模式按模式默认档上行：切回历史对话恢复模式时，chatMode 可能
      // 还停在别的对话选的档位，不能把它带进锁档模式（显式选模式时 ChatModeSwitch
      // 也是切到默认档，这里保持同一语义）。
      const activeModeSpec = useChatModeStore.getState().modeOf(useChatStore.getState().modeSlug);
      const wireChatMode = activeModeSpec.effort_locked ? activeModeSpec.default_effort : chatMode;
      const r = await authFetch(`${effectiveApiUrl}/v1/chats/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...projectTargetHeaders(effectiveProjectId),
          ...chatTargetHeaders(currentChatId),
        },
        body: JSON.stringify({
          chat_id: currentChatId,
          message: wireMsg,
          model_name: 'qwen',
          ...(selectedModelProviderId ? { model_provider_id: selectedModelProviderId } : {}),
          chat_mode: wireChatMode,
          mode_slug: useChatStore.getState().modeSlug,
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
          ...(agentId ? { agent_id: agentId } : {}),
          ...chatInvocationRequestFields(currentInvocation),
          ...(batchChat ? { batch_chat: true } : {}),
          ...(workflowChat ? { workflow_chat: true } : {}),
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

      const outcome = await processChatStreamWithHandoffRecovery(r, streamChatId, {
        enableThinking: isThinkingMode(chatMode),
        signal: abortController.signal,
      });
      streamOutcome = outcome;

      addBackendSessionId(currentChatId);
      addLoadedMsgId(currentChatId);
      syncManualTitleToBackend(currentChatId);

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
            if (followUpAbortRef.current.get(_pollChatId) === pollAc) {
              followUpAbortRef.current.delete(_pollChatId);
            }
          }
        })();
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
      settleQueuedMessageAfterRun(
        streamChatId,
        streamOutcome?.placeholderTs,
        streamOutcome !== undefined,
      );
      // NOTE: do NOT clear uploadedFiles / fileUploadMap here. This round's
      // attachments were already cleared right after they were assembled
      // (before the request), so anything present now was uploaded by the
      // user DURING streaming for the next question — wiping it here made
      // those attachments silently vanish when the stream ended.
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
  interface FollowStreamOptions {
    enableThinking?: boolean;
    pendingNotice?: string;
    signal?: AbortSignal;
    seedFrom?: ChatMessage;
  }

  async function processRegenerateStream(
    response: Response,
    chatId: string,
    opts: FollowStreamOptions = {},
  ) {
    const outcome = await processChatStreamWithHandoffRecovery(response, chatId, opts);
    useChatStore.getState().addBackendSessionId(chatId);
    useChatStore.getState().addLoadedMsgId(chatId);
    syncManualTitleToBackend(chatId);
    setTimeout(() => generateSummary(chatId), 500);
    setTimeout(() => generateClassification(chatId), 800);
    return outcome;
  }

  async function discoverQueuedRun(
    sourceRunId: string,
    chatId: string,
  ): Promise<QueuedRunHandoff | undefined> {
    try {
      const items = await getChatRunSteers(sourceRunId, chatId);
      for (const item of items) {
        const handoff = parseAppliedQueueHandoff(
          item as unknown as Record<string, unknown>,
          sourceRunId,
        );
        if (handoff) return handoff;
      }
      return undefined;
    } catch {
      return undefined;
    }
  }

  /** Consume a stream and recover a DB-committed handoff even if its Redis event was lost. */
  async function processChatStreamWithHandoffRecovery(
    response: Response,
    chatId: string,
    { enableThinking = false, pendingNotice, signal, seedFrom }: FollowStreamOptions = {},
  ) {
    let outcome: Awaited<ReturnType<typeof processChatStream>> | undefined;
    try {
      outcome = await processChatStream(response, { chatId, enableThinking, pendingNotice, seedFrom });
    } catch (error) {
      const sourceRunId = useChatStore.getState().activeRuns[chatId]?.runId;
      const recovered = await followQueuedRunChain(
        undefined,
        chatId,
        enableThinking,
        signal,
        sourceRunId,
      );
      if (!recovered) throw error;
      return recovered;
    }
    const sourceRunId = useChatStore.getState().activeRuns[chatId]?.runId;
    return (
      await followQueuedRunChain(outcome, chatId, enableThinking, signal, sourceRunId)
    ) ?? outcome;
  }

  /**
   * A followUp/nextRun, or a steer that missed the final safe boundary, is
   * committed together with the source run's completion.
   * Follow the committed child immediately so a fast child cannot finish in the
   * background before the 20-second active-run poll notices it.
   */
  async function followQueuedRunChain(
    initial: Awaited<ReturnType<typeof processChatStream>> | undefined,
    chatId: string,
    enableThinking: boolean,
    signal?: AbortSignal,
    initialSourceRunId?: string,
  ) {
    let outcome = initial;
    const seen = new Set<string>();
    let sourceRunId = initialSourceRunId;
    let usedDurableBackfill = false;
    while (!outcome?.aborted) {
      let queued = outcome?.queuedRun;
      if (!queued && sourceRunId) {
        queued = await discoverQueuedRun(sourceRunId, chatId);
        if (queued) usedDurableBackfill = true;
      }
      if (!queued) break;
      if (seen.has(queued.runId)) break;
      seen.add(queued.runId);

      useChatStore.getState().updateStore((prev) => {
        const chat = prev.chats[chatId];
        if (!chat || chat.messages.some((item) => item.messageId === queued.userMessageId)) return prev;
        const lastTs = chat.messages.length > 0 ? chat.messages[chat.messages.length - 1].ts : 0;
        const userMessage: ChatMessage = {
          role: 'user',
          content: queued.message,
          isMarkdown: false,
          ts: Math.max(Date.now(), lastTs + 1),
          messageId: queued.userMessageId,
        };
        return {
          ...prev,
          chats: {
            ...prev.chats,
            [chatId]: {
              ...chat,
              messages: [...chat.messages, userMessage],
              updatedAt: Date.now(),
            },
          },
        };
      });
      const localQueued = useChatStore.getState().queuedMessages[chatId];
      if (localQueued?.id === queued.steerId) {
        useChatStore.getState().setQueuedMessage(chatId, null);
      }
      useChatStore.getState().setActiveRun(chatId, {
        runId: queued.runId,
        messageId: queued.messageId,
        lastOffset: 0,
      });

      const response = await followChatRun(
        queued.runId,
        0,
        signal,
        useAuthStore.getState().authUser?.user_id,
        chatId,
      );
      if (!response.ok || !response.body) throw new Error(await response.text());
      outcome = await processChatStream(response, { chatId, enableThinking });
      sourceRunId = queued.runId;
    }
    if (usedDurableBackfill) {
      // The projection was incomplete, so DB history is the final authority
      // for any child that finished before its replay was attached.
      await reloadChatHistory(chatId);
    }
    return outcome;
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
        truncateMessagesFrom(streamChatId, targetMsg);
      }

      const r = await regenerateMessage(streamChatId, messageIndex, abortController.signal);
      if (!r.ok || !r.body) throw new Error(await r.text());

      await processRegenerateStream(r, streamChatId, {
        enableThinking: isThinkingMode(useChatStore.getState().chatMode),
        signal: abortController.signal,
      });
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
    if (!newContent.trim()) return;

    // 编辑重发是**破坏性**的：后端 delete_messages_from 会把这条之后的消息全部硬删，
    // 撤不回来。编辑第一轮时，后面几十轮问答会一声不响地消失（问题 24）。
    // 所以在动手之前先把代价说清楚，让用户自己决定。
    {
      const chat = useChatStore.getState().store.chats[currentChatId];
      const msgs = chat?.messages || [];
      const targetTs = msgs[messageIndex]?.ts;
      const droppedRounds = typeof targetTs === 'number'
        ? msgs.filter((m) => m.ts > targetTs && m.role === 'user').length
        : 0;
      if (droppedRounds > 0) {
        const confirmed = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            title: t('编辑后将丢弃后续对话'),
            content: t('这条消息之后还有 {n} 轮问答，编辑重发会把它们一并删除且无法恢复。确定继续吗？', { n: droppedRounds }),
            okText: t('继续编辑'),
            okButtonProps: { danger: true },
            cancelText: t('取消'),
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return;
      }
    }
    if (sending) {
      // 正在流式输出时点「发送」：先停止当前回答再编辑重发（对齐主流产品行为），
      // 而不是静默吞掉点击。abort 触发本地 AbortError → 原流的 finally 清理
      // sendingChatIds；等一拍让清理落地后继续。
      abort(currentChatId);
      await new Promise((res) => setTimeout(res, 250));
    }
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);
    setEditingMessageTs(null);

    const abortController = new AbortController();
    abortControllersRef.current.set(streamChatId, abortController);

    try {
      const chat = useChatStore.getState().store.chats[streamChatId];
      const targetMsg = chat?.messages[messageIndex];
      if (targetMsg) {
        truncateMessagesFrom(streamChatId, targetMsg);
      }

      // Add the edited user message to local store. Editing only rewrites the text —
      // the backend replays the original turn's attachments and its skill / plugin /
      // connector / @agent selection, so the local echo has to keep showing them.
      const userMsg: ChatMessage = {
        role: 'user', content: newContent.trim(), isMarkdown: false, ts: Date.now(),
        ...(targetMsg?.attachments?.length ? { attachments: targetMsg.attachments } : {}),
        ...(targetMsg?.quotedFollowUp ? { quotedFollowUp: targetMsg.quotedFollowUp } : {}),
        ...(targetMsg?.skillId ? { skillId: targetMsg.skillId } : {}),
        ...(targetMsg?.skillName ? { skillName: targetMsg.skillName } : {}),
        ...(targetMsg?.pluginName ? { pluginName: targetMsg.pluginName } : {}),
        ...(targetMsg?.connectorName ? { connectorName: targetMsg.connectorName } : {}),
        ...(targetMsg?.mentionName ? { mentionName: targetMsg.mentionName } : {}),
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

      await processRegenerateStream(r, streamChatId, {
        enableThinking: isThinkingMode(useChatStore.getState().chatMode),
        signal: abortController.signal,
      });
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        message.error(t('编辑重发失败：{msg}', { msg: e?.message || String(e) }));
      }
    } finally {
      abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
    }
  }

  async function smartSend(directMessage?: string, invocationOverride?: ChatInvocationContext) {
    const { planMode, loopMode, sending } = useChatStore.getState();
    if (sending) {
      queueDuringRun(directMessage, invocationOverride);
      return;
    }
    if (planMode) {
      return sendPlanMode(effectiveApiUrl, abortControllersRef, fileUploadMap, generateSummary, directMessage);
    }
    if (loopMode) {
      return sendLoopMode(abortControllersRef, directMessage);
    }
    return send(directMessage, invocationOverride);
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
    const uid = useAuthStore.getState().authUser?.user_id;
    if (activeRun?.runId) {
      // 先登记用户意图，再发取消请求：取消是 fire-and-forget，请求失败或后端
      // 协作式取消慢一拍时，这条登记保证任何重挂路径都不会再把它捡起来重放。
      markRunCancelledByUser(activeRun.runId);
      // fire-and-forget: a failed cancel call must not block the local abort
      cancelChatRun(activeRun.runId, uid, targetId).catch(() => { /* noop — backend orphan recovery is the safety net */ });
    } else {
      // 本窗口没拿到这一轮的 run_id（跟随权在另一个窗口 / 刷新后还没挂上）时，
      // 旧代码直接跳过取消，后端那轮继续跑；切回来 resumeRunIfAny 一挂就表现成
      // "已中断的任务又开始执行了"。这里补一次反查，按会话取活的 run 再取消。
      void getActiveChatRun(targetId, uid)
        .then((run) => {
          if (!run?.run_id) return;
          markRunCancelledByUser(run.run_id);
          return cancelChatRun(run.run_id, uid, targetId);
        })
        .catch(() => { /* noop — 后端孤儿回收兜底 */ });
    }
    const controller = abortControllersRef.current.get(targetId);
    if (controller) {
      controller.abort();
      abortControllersRef.current.delete(targetId);
    }

    // 计划模式：把后端的计划和它的 run 一并取消。只 abort 本地 SSE 是不够的——计划在
    // 后端仍是 approved/执行中，切走再切回来（resumeRunIfAny 会重新挂上那个还活着的
    // run）就表现为「已经中断的任务又自己跑起来了」（问题 32）。
    {
      const chat = useChatStore.getState().store.chats[targetId];
      const execPlanId = (chat?.messages || [])
        .flatMap((m) => m.segments || [])
        .filter((seg) => seg.type === 'plan' && seg.planData?.mode === 'executing' && !seg.planData?.cancelled)
        .map((seg) => seg.planData?.planId)
        .filter((id): id is string => !!id)
        .pop();
      if (execPlanId) {
        cancelPlanApi(execPlanId).catch(() => { /* noop —— 本地已经断流，后端有孤儿回收兜底 */ });
      }
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

  /* ───────────────────────────────────────────
     断连看门狗 —— 「后台早跑完了，前台还在转圈，只能靠刷新页面才发现」的根治。

     成因：一轮长工具（批量作业 run_job 实测能把一轮撑到 54 分钟）期间，SSE 上只有每
     15 秒一行心跳。中间任何一层（代理、NAT、休眠唤醒）把连接悄悄掐成半开时，fetch 既
     不报错也不结束，reader 就永远 await 下去——气泡于是停在最后一帧转圈，而后端那轮
     早已正常跑完落库。过去唯一的出路是用户自己刷新页面（resumeRunIfAny 只在切会话/
     刷新时跑一次）。

     判据用**传输层活性**而不是「有没有新内容」：长工具期间没有可渲染事件是正常的，
     心跳断了才是真断了。连丢 5 拍（75 秒）才动手，宁可晚一点也不误伤慢链路。
     ─────────────────────────────────────────── */
  const RUN_STALL_MS = 75_000;
  const RUN_WATCH_EVERY_MS = 20_000;
  /** 正在对账的会话，防止两轮定时器叠在同一个会话上互相拆台。 */
  const reconcilingRef = useRef<Set<string>>(new Set());

  async function reconcileStalledRun(chatId: string) {
    if (reconcilingRef.current.has(chatId)) return;
    reconcilingRef.current.add(chatId);
    try {
      const uid = useAuthStore.getState().authUser?.user_id;
      let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
      try {
        active = await getActiveChatRun(chatId, uid);
      } catch {
        return; // 查不到就等下一轮：宁可继续转圈，也不要凭一次网络抖动拆掉正在跑的轮次
      }
      const live = !!active && (active.status === 'running' || active.status === 'pending');
      // 本地这条连接已经确定是死的（75 秒没有任何字节），先把它断掉，让气泡收尾
      abortControllersRef.current.get(chatId)?.abort();

      if (!live) {
        // 后端那轮已经终态：结束僵尸 UI，并把库里的最终消息拉回来——用户不用再手动刷新。
        cleanupZombieRunState(chatId, active?.status || 'completed');
        await reloadChatHistory(chatId);
        return;
      }

      // run 还活着 → 重挂。等 abort 的 finally 把 sendingChatIds 清掉，否则 resumeRunIfAny
      // 会被它自己的「正在发送就别插手」守卫挡回来。
      for (let i = 0; i < 12 && useChatStore.getState().sendingChatIds.has(chatId); i++) {
        await new Promise((r) => setTimeout(r, 250));
      }
      if (useChatStore.getState().sendingChatIds.has(chatId)) return; // 没让开就等下一轮
      await resumeRunIfAny(chatId);
    } finally {
      reconcilingRef.current.delete(chatId);
    }
  }

  /** 后端自己发起的那一轮 —— 批量作业跑完/中途播报会在**同一个会话里入队一条新 run**
   *  （job_wakeup），用户没点任何东西，前端也就没有任何本地流。过去只有切会话或刷新
   *  才会去看一眼有没有在跑的 run，于是这轮播报全程隐身：作业早交付完了，页面上还是
   *  用户离开时的样子。这里在当前会话空闲时补一眼，发现有活的 run 就照常跟随。 */
  async function attachServerStartedRun(chatId: string) {
    const store = useChatStore.getState();
    if (store.sendingChatIds.has(chatId) || store.activeRuns[chatId]) return;
    let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
    try {
      active = await getActiveChatRun(chatId, useAuthStore.getState().authUser?.user_id);
    } catch {
      return;
    }
    if (!active?.run_id || (active.status !== 'running' && active.status !== 'pending')) return;
    // 用户按过停止的那一轮：后端可能还没落终态，但用户的意图是终局的，不许重挂
    if (isRunCancelledByUser(active.run_id)) return;
    // 自己刚跑完那一轮的残影（本地流已收尾、后端还没落终态）——认错了会把同一轮重放成两个气泡
    if (hasStreamedRun(active.run_id)) return;
    // 这轮的用户侧消息（唤醒指令）和助手行都是后端落的库；resumeRunIfAny 会先把历史
    // 拉齐、再以库里那行为基态接上流。
    await resumeRunIfAny(chatId);
  }

  useEffect(() => {
    const timer = window.setInterval(() => {
      const { sendingChatIds, activeRuns, currentChatId } = useChatStore.getState();
      const now = Date.now();
      sendingChatIds.forEach((cid) => {
        // 还没拿到 run_id 的（刚 POST 出去、首帧未到）不归看门狗管
        if (!activeRuns[cid]) return;
        const last = getStreamActivityTs(cid);
        if (!last || now - last < RUN_STALL_MS) return;
        void reconcileStalledRun(cid);
      });
      // 后台标签页不必占着这一发：切回来时 currentChatId 的 effect 本来就会补跟随
      if (currentChatId && typeof document !== 'undefined' && document.visibilityState !== 'hidden') {
        void attachServerStartedRun(currentChatId);
      }
    }, RUN_WATCH_EVERY_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function resumeRunIfAny(chatId: string) {
    const uid = useAuthStore.getState().authUser?.user_id;
    let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
    try {
      active = await getActiveChatRun(chatId, uid);
    } catch {
      return;
    }
    const restoredQueued = useChatStore.getState().queuedMessages[chatId];
    const durableRunId = restoredQueued?.targetRunId || active?.run_id;
    if (durableRunId) await reconcileDurableSteerQueue(chatId, durableRunId);
    // Autonomous-loop plan-bar reconciliation: a plan bar restored from localStorage may still
    // read running while the backend run has already ended (stopped / finished / crashed). As
    // long as there's no "active loop run" that would be followed below, wind the plan bar down
    // to the real loop state, so it doesn't stay stuck on "in progress" forever after a refresh.
    await reconcileLoopBar(chatId, active);

    if (!active || !active.run_id) {
      settleQueuedMessageAfterRun(chatId, undefined, false);
      return;
    }
    // 用户已经按过停止：即便后端这一轮还挂着 running（协作式取消尚未落终态、
    // 或取消请求失败），也不许再挂上去重放——那正是"中断的任务又开始执行了"。
    // 顺手补一刀取消，让后端那轮真的停下来。
    if (isRunCancelledByUser(active.run_id)) {
      cancelChatRun(active.run_id, uid, chatId).catch(() => { /* noop */ });
      cleanupZombieRunState(chatId, 'cancelled');
      settleQueuedMessageAfterRun(chatId, undefined, false);
      return;
    }
    if (active.status !== 'running' && active.status !== 'pending') {
      // Run already terminal (failed / cancelled / completed) — the backend's
      // recover_orphan_runs marks zombie running runs failed on restart and writes a terminal
      // event. But the frontend may still have leftover sendingChatIds + a last assistant
      // message with isStreaming=true. Explicitly clean up this zombie UI state, and for the
      // failed path show a toast once so the user resends.
      cleanupZombieRunState(chatId, active.status);
      // 终态的行已经在库里定稿（正文、工具卡、错误或计划快照都在），拉一次就是最终样子。
      await reloadChatHistory(chatId);
      return;
    }

    // Re-read state at the latest moment — user may have started a fresh send
    // during the active-run round-trip.
    if (useChatStore.getState().sendingChatIds.has(chatId)) return;

    // activeRun 在锁外登记：即使本标签页没拿到跟随权，停止按钮也能取消该 run
    useChatStore.getState().setActiveRun(chatId, {
      runId: active.run_id,
      messageId: active.message_id,
      lastOffset: active.last_event_offset || 0,
    });

    // ── 跨标签页互斥：同一 run 只允许一个标签页跟随 SSE ──
    // 过去复制标签页/多开时两个标签页同时 follow 同一 run，各自用不同的
    // placeholderTs 建气泡，互相覆盖 localStorage，产生重复/半截气泡与
    // "回答无对应问题"（问题17）。Web Locks 随标签页关闭自动释放。
    const runLockName = `hugagent_run_follow_${active.run_id}`;
    const activeRun = active;
    const doFollowRun = () => followActiveRun(chatId, activeRun, uid);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const locksApi = typeof navigator !== 'undefined' ? (navigator as any).locks : undefined;
    if (locksApi?.request) {
      await locksApi.request(runLockName, { ifAvailable: true }, async (lock: unknown) => {
        if (!lock) return; // 另一个标签页正在跟随该 run
        if (useChatStore.getState().sendingChatIds.has(chatId)) return;
        await doFollowRun();
      });
    } else {
      await doFollowRun();
    }
  }

  /** 实际跟随一个后台 run 的 SSE（plan / loop / 普通对话三种分支）。 */
  async function followActiveRun(
    chatId: string,
    active: NonNullable<Awaited<ReturnType<typeof getActiveChatRun>>>,
    uid: string | undefined,
  ) {
    const { addSendingChatId, removeSendingChatId } = useChatStore.getState();

    // Plan mode: live-replay the plan event stream (plan_step_* / tool_call / tool_result /
    // plan_complete), fully continuous with the pre-refresh progress.
    if (active.kind === 'plan_execute' || active.kind === 'plan_generate') {
      addSendingChatId(chatId);
      const ac = new AbortController();
      abortControllersRef.current.set(chatId, ac);
      try {
        const resp = await followChatRun(active.run_id, 0, ac.signal, uid, chatId);
        if (!resp.ok || !resp.body) return;
        if (active.kind === 'plan_execute' && active.plan_id) {
          await processPlanExecuteStream(resp, chatId, active.plan_id, {
            placeholderTs: Date.now(),
            onSetCurrentPlanId: useChatStore.getState().setCurrentPlanId,
            onAfterComplete: (cid) => {
              // After replay completes, refresh the message list, replacing client-built state
              // with the final message in the DB, ensuring the stop button, isStreaming flag, etc. wind down correctly.
              void reloadChatHistory(cid);
            },
          });
        } else if (active.kind === 'plan_generate') {
          await processPlanGenerateStream(resp, chatId, {
            placeholderTs: Date.now(),
            onSetCurrentPlanId: useChatStore.getState().setCurrentPlanId,
          });
          // Also refresh after generate completes: pick up the DB-persisted assistant message + plan_snapshot
          await reloadChatHistory(chatId);
        }
      } catch (e: any) {
        if (e?.name !== 'AbortError') {
          // The task may have already finished in the DB; history is the final authority.
          await reloadChatHistory(chatId);
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
      // The replay's run_started frame carries the message_id, so it takes over the bubble
      // history already rendered for this run instead of drawing a second one.
      const ac = new AbortController();
      abortControllersRef.current.set(chatId, ac);
      try {
        const resp = await followChatRun(active.run_id, 0, ac.signal, uid, chatId);
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

    // 服务端那一行是这一轮的基态：从它记下的 event_offset 之后接着喂流。基态停在一个
    // 没闭合的 <think> 里时剥离器接不上，只能从头重放。
    await reloadChatHistory(chatId);
    const base = useChatStore.getState().store.chats[chatId]?.messages
      .find((m) => m.role === 'assistant' && m.messageId === active.message_id);
    const seedFrom = base && !hasUnclosedThink(base.content) ? base : undefined;
    const fromOffset = seedFrom?.inFlight?.eventOffset ?? 0;

    addSendingChatId(chatId);

    const abortController = new AbortController();
    abortControllersRef.current.set(chatId, abortController);
    let streamOutcome: Awaited<ReturnType<typeof processChatStream>> | undefined;

    try {
      const r = await followChatRun(active.run_id, fromOffset, abortController.signal, uid, chatId);
      if (!r.ok || !r.body) return;
      streamOutcome = await processRegenerateStream(r, chatId, {
        enableThinking: !!active.enable_thinking,
        signal: abortController.signal,
        seedFrom,
      });
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        // Resume failure is handled silently — UX-wise it's equivalent to "the task runs in the background and the final message arrives via the next refresh"
      }
    } finally {
      abortControllersRef.current.delete(chatId);
      removeSendingChatId(chatId);
      useChatStore.getState().clearActiveRun(chatId);
      settleQueuedMessageAfterRun(
        chatId,
        streamOutcome?.placeholderTs,
        streamOutcome !== undefined,
      );
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
          truncateMessagesFrom(chatId, m);
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
      await processRegenerateStream(r, chatId, {
        enableThinking: isThinkingMode(useChatStore.getState().chatMode),
        signal: abortController.signal,
      });
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

  return {
    send: smartSend,
    abort,
    activateQueuedMessage,
    discardQueuedMessage,
    handleFileSelect,
    removeFile,
    fileUploadMap,
    regenerate,
    editAndResend,
    resumeRunIfAny,
    cancelAndResumeBatch,
    continueLoop,
  };
}
