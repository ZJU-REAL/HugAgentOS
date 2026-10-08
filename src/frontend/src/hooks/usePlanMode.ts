import { t } from '../i18n';
import { getPlanApi } from '../api';
import { stripMcpToolPrefix } from '../utils/constants';
import { useChatStore } from '../stores';
import type { ChatMessage, MessageSegment, ToolCall } from '../types';
import { newMessageUid } from '../utils/messageIdentity';


/** Mark the approval decision on the newest preview plan segment carrying planId
 *  (hides the confirm/discard buttons on the card afterwards). */
export function markPlanDecision(chatId: string, planId: string, decision: 'confirmed' | 'cancelled') {
  useChatStore.getState().updateStore((prev) => {
    const c = prev.chats[chatId];
    if (!c) return { chats: prev.chats, order: prev.order };
    const msgs = [...(c.messages || [])];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== 'assistant' || !m.segments) continue;
      const segIdx = m.segments.findIndex(
        (s) => s.type === 'plan' && s.planData?.mode === 'preview' && s.planData.planId === planId,
      );
      if (segIdx < 0) continue;
      const segments = [...m.segments];
      const seg = segments[segIdx];
      segments[segIdx] = { ...seg, planData: { ...seg.planData!, decided: decision } };
      msgs[i] = { ...m, segments };
      return { chats: { ...prev.chats, [chatId]: { ...c, messages: msgs } }, order: prev.order };
    }
    return { chats: prev.chats, order: prev.order };
  });
}

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
    bubbleUid?: string;
    onSetCurrentPlanId?: (id: string | null) => void;
    onAfterComplete?: (chatId: string) => void;
  } = {},
): Promise<void> {
  // Manual plan mode renders the complete execution state in its message card.
  // Clear any compact-strip state left by an older frontend bundle or a
  // recovered stream so the same plan is never duplicated above the composer.
  useChatStore.getState().setPlanProgress(chatId, null);

  const decoder = new TextDecoder();
  const execReader = response.body?.getReader();
  if (!execReader) return;

  const bubbleUid = options.bubbleUid ?? newMessageUid();
  const appendAssistant = makePlanAppender(chatId, bubbleUid);

  let execBuf = '';
  const stepResults: Record<string, { status: string; summary: string; text: string; title: string; order: number; step_id: string }> = {};
  const toolCalls: ToolCall[] = [];
  let planTitle = '';
  let planDesc = '';
  let planStepDefs: Array<Record<string, unknown>> = [];
  let planCompleted = false;
  let planAgentNameMap: Record<string, string> | undefined;

  try {
    const plan = await getPlanApi(planId, chatId);
    planTitle = plan.title;
    planDesc = plan.description || '';
    planStepDefs = plan.steps as any[];
    planAgentNameMap = (plan as any).agent_name_map || undefined;
  } catch { /* fallback: collect steps incrementally from the stream */ }

  const buildExecPlanData = (mode: 'executing' | 'complete', completedSteps?: number, totalSteps?: number, resultText?: string, cancelled?: boolean): MessageSegment['planData'] => {
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
      // 带上 planId：中止时要靠它调 /v1/plans/{id}/cancel 把后端的计划和 run 一起停掉。
      // 放在 planData 里而不是只依赖全局 currentPlanId，才能按会话精确定位。
      planId,
      title: planTitle || t('执行中...'),
      description: planDesc || undefined,
      steps,
      completedSteps,
      totalSteps,
      resultText,
      agentNameMap: planAgentNameMap,
      cancelled,
    };
  };

  const updatePlanCard = (streaming: boolean, mode: 'executing' | 'complete' = 'executing', completedSteps?: number, totalSteps?: number, resultText?: string, cancelled?: boolean) => {
    const planData = buildExecPlanData(mode, completedSteps, totalSteps, resultText, cancelled);
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
                      resultDisplayName = t('调用智能体：{name}', { name: (evt.subagent_name as string).trim() });
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
  } catch (e: unknown) {
    // 用户中断（停止按钮 / 关闭计划模式）：AbortError 原来直接往上抛，卡片就停在
    // 最后一次 updatePlanCard(true) 的状态——mode 还是 'executing'、streaming 还是
    // true，于是「执行中」的转圈永远停不下来，只有刷新页面才会好（问题 31）。
    // 这里把卡片落到「已中断」这个终态，未跑完的步骤也不再显示成 running。
    if ((e as { name?: string })?.name === 'AbortError') {
      toolCalls.forEach((tc) => { if (tc.status === 'running') tc.status = 'interrupted'; });
      Object.values(stepResults).forEach((r) => { if (r.status === 'running') r.status = 'failed'; });
      updatePlanCard(false, 'executing', undefined, undefined, undefined, true);
      options.onSetCurrentPlanId?.(null);
      return;
    }
    throw e;
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
    bubbleUid?: string;
    onSetCurrentPlanId?: (id: string | null) => void;
  } = {},
): Promise<{ planEvt: Record<string, unknown> | null; errorEvt: Record<string, unknown> | null }> {
  const bubbleUid = options.bubbleUid ?? newMessageUid();
  const appendAssistant = makePlanAppender(chatId, bubbleUid);

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
export function makePlanAppender(chatId: string, bubbleUid: string) {
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
      if (last?.role === 'assistant' && last.uid === bubbleUid) {
        msgs[msgs.length - 1] = { ...last, ...updated };
      } else {
        msgs.push({ role: 'assistant', uid: bubbleUid, ts: Date.now(), ...updated });
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
