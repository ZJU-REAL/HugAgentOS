import { message } from 'antd';
import { t } from '../i18n';
import { generatePlanStream, updatePlanApi, executePlanStream, getPlanApi } from '../api';
import { parseSpaceFileContent } from '../utils/fileParser';
import { stripMcpToolPrefix } from '../utils/constants';
import { useChatStore, useCatalogStore, useFileStore, useModelCapabilitiesStore } from '../stores';
import { useProjectStore } from '../stores/projectStore';
import type { ChatItem, ChatMessage, MessageSegment, ToolCall } from '../types';

/**
 * Shared SSE consumer for plan execute streams (used by both first-time
 * send and resume-after-refresh). Maintains the executing plan card +
 * tool calls in real time. Returns when the stream ends.
 */
export async function processPlanExecuteStream(
  response: Response,
  chatId: string,
  planId: string,
  options: {
    placeholderTs?: number;
    onSetCurrentPlanId?: (id: string | null) => void;
    onAfterComplete?: (chatId: string) => void;
  } = {},
): Promise<void> {
  const decoder = new TextDecoder();
  const execReader = response.body?.getReader();
  if (!execReader) return;

  const placeholderTs = options.placeholderTs ?? Date.now();
  const appendAssistant = makePlanAppender(chatId, placeholderTs);

  let execBuf = '';
  const stepResults: Record<string, { status: string; summary: string; text: string; title: string; order: number; step_id: string }> = {};
  const toolCalls: ToolCall[] = [];
  let planTitle = '';
  let planDesc = '';
  let planStepDefs: Array<Record<string, unknown>> = [];
  let planCompleted = false;
  let planAgentNameMap: Record<string, string> | undefined;

  try {
    const plan = await getPlanApi(planId);
    planTitle = plan.title;
    planDesc = plan.description || '';
    planStepDefs = plan.steps as any[];
    planAgentNameMap = (plan as any).agent_name_map || undefined;
  } catch { /* fallback: collect steps incrementally from the stream */ }

  const buildExecPlanData = (mode: 'executing' | 'complete', completedSteps?: number, totalSteps?: number, resultText?: string): MessageSegment['planData'] => {
    const stepSource = planStepDefs.length > 0 ? planStepDefs : Object.values(stepResults).sort((a, b) => a.order - b.order);
    const steps = stepSource.map(s => {
      const sid = (s as any).step_id;
      const r = sid ? stepResults[sid] : undefined;
      return {
        step_order: r?.order || (s as any).step_order || 0,
        title: r?.title || (s as any).title || '',
        description: (s as any).description,
        status: (r?.status || 'pending') as any,
        summary: r?.summary || '',
        text: r?.text || '',
      };
    });
    return {
      mode,
      title: planTitle || t('执行中...'),
      description: planDesc || undefined,
      steps,
      completedSteps,
      totalSteps,
      resultText,
      agentNameMap: planAgentNameMap,
    };
  };

  const updatePlanCard = (streaming: boolean, mode: 'executing' | 'complete' = 'executing', completedSteps?: number, totalSteps?: number, resultText?: string) => {
    const planData = buildExecPlanData(mode, completedSteps, totalSteps, resultText);
    const segments: MessageSegment[] = [{ type: 'plan', planData }];
    toolCalls.forEach((_tc, idx) => { segments.push({ type: 'tool', toolIndex: idx }); });
    const content = resultText || '';
    if (resultText) segments.push({ type: 'text', content });
    appendAssistant(content, streaming, [...toolCalls], [...segments]);
  };

  updatePlanCard(true);

  try {
    while (true) {
      const { done, value } = await execReader.read();
      if (done) break;
      execBuf += decoder.decode(value, { stream: true });
      const blocks = execBuf.split(/\n\n+/);
      execBuf = blocks.pop() || '';
      for (const block of blocks) {
        for (const line of block.split(/\r?\n/)) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const data = trimmed.slice(5).trim();
          if (data === '[DONE]') break;
          try {
            const evt = JSON.parse(data);
            const stepId = evt.step_id as string | undefined;
            // First frame run_started — store activeRun so the stop button is usable
            if (evt.type === 'run_started' && typeof evt.run_id === 'string') {
              useChatStore.getState().setActiveRun(chatId, {
                runId: evt.run_id,
                messageId: typeof evt.message_id === 'string' ? evt.message_id : '',
              });
              continue;
            }
            switch (evt.type) {
              case 'plan_step_start':
                if (stepId) stepResults[stepId] = { status: 'running', summary: '', text: '', title: evt.title || '', order: evt.step_order || 0, step_id: stepId };
                updatePlanCard(true);
                break;
              case 'plan_step_progress':
                if (stepId && stepResults[stepId]) { stepResults[stepId].text += evt.delta || ''; updatePlanCard(true); }
                break;
              case 'tool_call': {
                if (stepId) {
                  let tcDisplayName = typeof evt.tool_display_name === 'string' && evt.tool_display_name.trim()
                    && !evt.tool_display_name.trim().startsWith('mcp__')
                    ? evt.tool_display_name.trim()
                    : undefined;
                  if (tcDisplayName && typeof evt.subagent_name === 'string' && evt.subagent_name.trim()) {
                    tcDisplayName += `:${(evt.subagent_name as string).trim()}`;
                  }
                  toolCalls.push({ id: evt.tool_id, name: stripMcpToolPrefix(evt.tool_name || 'unknown'), displayName: tcDisplayName, input: evt.tool_args, status: 'running', timestamp: Date.now() });
                  updatePlanCard(true);
                }
                break;
              }
              case 'tool_result': {
                if (evt.tool_id) {
                  const idx = toolCalls.findIndex(t => t.id === evt.tool_id);
                  if (idx >= 0) {
                    let resultDisplayName: string | undefined;
                    if (typeof evt.subagent_name === 'string' && evt.subagent_name.trim()) {
                      resultDisplayName = t('调用子智能体：{name}', { name: (evt.subagent_name as string).trim() });
                    }
                    toolCalls[idx] = { ...toolCalls[idx], output: evt.result, status: 'success', ...(resultDisplayName ? { displayName: resultDisplayName } : {}) };
                    updatePlanCard(true);
                  }
                }
                break;
              }
              case 'plan_step_complete':
                if (stepId && stepResults[stepId]) {
                  stepResults[stepId].status = evt.status || 'success';
                  stepResults[stepId].summary = evt.summary || '';
                  stepResults[stepId].text = '';
                  updatePlanCard(true);
                }
                break;
              case 'plan_error':
                if (stepId && stepResults[stepId]) { stepResults[stepId].status = 'failed'; stepResults[stepId].summary = evt.error || t('执行出错'); }
                updatePlanCard(true);
                break;
              case 'plan_complete': {
                planCompleted = true;
                updatePlanCard(false, 'complete', evt.completed_steps, evt.total_steps, evt.result_text || undefined);
                break;
              }
            }
          } catch { /* skip invalid JSON */ }
        }
      }
    }
  } finally {
    try { execReader.releaseLock(); } catch { /* ignore */ }
  }

  toolCalls.forEach(tc => { if (tc.status === 'running') tc.status = 'success'; });
  if (!planCompleted) updatePlanCard(false);
  options.onSetCurrentPlanId?.(null);
  useChatStore.getState().addBackendSessionId(chatId);
  useChatStore.getState().addLoadedMsgId(chatId);
  options.onAfterComplete?.(chatId);
}

/**
 * Shared SSE consumer for plan generate streams. Returns the parsed plan
 * event (or null) so the caller can transition into preview UI.
 */
export async function processPlanGenerateStream(
  response: Response,
  chatId: string,
  options: {
    placeholderTs?: number;
    onSetCurrentPlanId?: (id: string | null) => void;
  } = {},
): Promise<{ planEvt: Record<string, unknown> | null; errorEvt: Record<string, unknown> | null }> {
  const placeholderTs = options.placeholderTs ?? Date.now();
  const appendAssistant = makePlanAppender(chatId, placeholderTs);

  // First show a placeholder streaming message (in the initial scenario the caller appends it ahead of time; in the replay scenario we add it once here)
  appendAssistant(t('🔍 正在分析任务并生成执行计划...'), true);

  const events = await readPlanSse(response, (evt) => {
    if (evt.type === 'run_started' && typeof evt.run_id === 'string') {
      useChatStore.getState().setActiveRun(chatId, {
        runId: evt.run_id as string,
        messageId: typeof evt.message_id === 'string' ? evt.message_id : '',
      });
    }
  });
  const planEvt = events.find(e => e.type === 'plan_generated') || null;
  const errorEvt = events.find(e => e.type === 'plan_error') || null;

  if (errorEvt) {
    appendAssistant(t('计划生成失败：{error}', { error: String(errorEvt.error) }), false);
    return { planEvt: null, errorEvt };
  }
  if (!planEvt) {
    appendAssistant(t('计划生成未返回有效结果，请重试。'), false);
    return { planEvt: null, errorEvt: null };
  }

  options.onSetCurrentPlanId?.(planEvt.plan_id as string);
  const planSegData = buildPlanSegmentData(planEvt);
  const planSegments: MessageSegment[] = [{ type: 'plan', planData: planSegData }];
  appendAssistant('', false, undefined, planSegments);
  return { planEvt, errorEvt: null };
}

/** Helper: read SSE stream and collect events.
 *
 * Optional ``onEvent`` callback fires synchronously as each event arrives —
 * useful for capturing ``run_started`` to wire up the stop button before the
 * full stream completes.
 */
export async function readPlanSse(
  response: Response,
  onEvent?: (event: Record<string, unknown>) => void,
): Promise<Array<Record<string, unknown>>> {
  const events: Array<Record<string, unknown>> = [];
  const reader = response.body?.getReader();
  if (!reader) return events;
  const decoder = new TextDecoder();
  let buf = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const blocks = buf.split(/\n\n+/);
      buf = blocks.pop() || '';
      for (const block of blocks) {
        for (const line of block.split(/\r?\n/)) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const data = trimmed.slice(5).trim();
          if (data === '[DONE]') return events;
          try {
            const parsed = JSON.parse(data);
            events.push(parsed);
            if (onEvent) onEvent(parsed);
          } catch { /* skip */ }
        }
      }
    }
  } finally { reader.releaseLock(); }
  return events;
}

/** Helper: append/update assistant message in current chat */
export function makePlanAppender(chatId: string, ts: number) {
  return (content: string, streaming: boolean, toolCalls?: ToolCall[], segments?: MessageSegment[]) => {
    useChatStore.getState().updateStore((prev) => {
      const c = prev.chats[chatId];
      const msgs = [...(c?.messages || [])];
      const last = msgs[msgs.length - 1];
      const isMd = content.includes('\n') || content.includes('**') || /^\s*[#\-\d]/.test(content);
      const updated: Partial<ChatMessage> & { content: string; isMarkdown: boolean; isStreaming: boolean } = {
        content, isMarkdown: isMd, isStreaming: streaming,
        ...(toolCalls && toolCalls.length > 0 ? { toolCalls } : {}),
        ...(segments && segments.length > 0 ? { segments } : {}),
      };
      if (last?.role === 'assistant' && last.ts === ts) {
        msgs[msgs.length - 1] = { ...last, ...updated };
      } else {
        msgs.push({ role: 'assistant', ts, ...updated });
      }
      // Mid-stream updates keep the existing updatedAt / order: the sendPlanMode entry
      // already pushed the chat to the front, and bumping again on every SSE chunk would
      // make the sidebar jitter up and down under concurrent multi-session activity.
      return { chats: { ...prev.chats, [chatId]: { ...(c as any), messages: msgs } }, order: prev.order };
    });
  };
}

/** Build structured plan data for PlanCard segment rendering */
export function buildPlanSegmentData(planData: Record<string, unknown>): MessageSegment['planData'] {
  const steps = (planData.steps || []) as Array<Record<string, unknown>>;
  return {
    mode: 'preview',
    planId: planData.plan_id ? String(planData.plan_id) : undefined,
    title: String(planData.title || ''),
    description: planData.description ? String(planData.description) : undefined,
    steps: steps.map(s => ({
      step_order: Number(s.step_order || 0),
      title: String(s.title || ''),
      description: s.description ? String(s.description) : undefined,
      expected_tools: (s.expected_tools as string[]) || [],
      expected_skills: (s.expected_skills as string[]) || [],
      expected_agents: (s.expected_agents as string[]) || [],
      acceptance_criteria: s.acceptance_criteria ? String(s.acceptance_criteria) : undefined,
    })),
    agentNameMap: (planData.agent_name_map as Record<string, string>) || undefined,
  };
}

export async function sendPlanMode(
  effectiveApiUrl: string,
  abortControllersRef: React.MutableRefObject<Map<string, AbortController>>,
  fileUploadMap: React.MutableRefObject<Map<File, Promise<{ content: string; file_id: string; download_url: string }>>>,
  generateSummary: (chatId: string) => Promise<void>,
  directMessage?: string,
  // suppressUserEcho: when the main agent automatically switches into plan mode it reuses this flow,
  // but no longer inserts a user bubble (the original user request that triggered this round is already in the session; directMessage is just the task text to be planned).
  opts: { suppressUserEcho?: boolean } = {},
) {
  const { input, setInput, sending, addSendingChatId, removeSendingChatId, currentChatId, updateStore, currentPlanId, setCurrentPlanId } = useChatStore.getState();
  const { catalog } = useCatalogStore.getState();
  const { uploadedFiles, setUploadedFiles, setUploadingFiles, importedSpaceFiles, clearImportedSpaceFiles } = useFileStore.getState();
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
  if (!directMessage) setInput('');

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

  const enabledMcpIds = (catalog.mcp || []).filter(x => x.enabled).map(x => String(x.id).trim()).filter(Boolean);
  const enabledSkillIds = (catalog.skills || []).filter(x => x.enabled).map(x => String(x.id).trim()).filter(Boolean);
  const enabledKbIds = (catalog.kb || []).filter(x => x.enabled).map(x => String(x.id).trim()).filter(Boolean);

  const userMsg: ChatMessage = {
    role: 'user', content: msg, isMarkdown: false, ts: Date.now(),
    ...(attachments.length > 0 && {
      attachments: attachments.map(a => ({
        name: a.name, mime_type: a.mime_type, file_id: a.file_id, download_url: a.download_url,
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

  const placeholderTs = Date.now();
  const appendAssistant = makePlanAppender(currentChatId, placeholderTs);
  appendAssistant('', true);

  const chatForHistory = useChatStore.getState().store.chats[currentChatId];
  // Project mounting: prefer the projectId bound to the session itself; when the session was just minted and hasn't been written back yet, fall back to
  // the currently active project (consistent with the logic in useStreaming.send).
  const projectId =
    (chatForHistory as { projectId?: string } | undefined)?.projectId ||
    useProjectStore.getState().currentProjectId ||
    undefined;
  const historyMessages: Array<{ role: string; content: string }> = [];
  if (chatForHistory?.messages) {
    for (const m of chatForHistory.messages) {
      if (m.ts === userMsg.ts) continue;
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
      await updatePlanApi(effectivePlanId, { status: 'approved' });
      const execResp = await executePlanStream(effectivePlanId, abortController.signal, enabledMcpIds, enabledSkillIds, enabledKbIds, currentChatId, historyMessages, undefined, projectId);
      if (!execResp.ok) throw new Error(t('计划执行请求失败: {status}', { status: execResp.status }));
      await processPlanExecuteStream(execResp, currentChatId, effectivePlanId, {
        placeholderTs,
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
        attachments,
        undefined,
        projectId,
        selectedModelProviderId,
        opts.suppressUserEcho,
      );
      if (!genResp.ok) throw new Error(t('计划生成请求失败: {status}', { status: genResp.status }));
      await processPlanGenerateStream(genResp, currentChatId, {
        placeholderTs,
        onSetCurrentPlanId: setCurrentPlanId,
      });
    }

  } catch (e: any) {
    if (e?.name !== 'AbortError') {
      appendAssistant(t('计划模式出错：{msg}', { msg: e?.message || String(e) }), false);
    }
  } finally {
    abortControllersRef.current.delete(streamChatId);
    removeSendingChatId(streamChatId);
    useChatStore.getState().clearActiveRun(streamChatId);
  }
}
