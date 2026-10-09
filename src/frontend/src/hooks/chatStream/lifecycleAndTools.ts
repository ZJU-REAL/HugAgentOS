import { t } from '../../i18n';
import { useCanvasStore,useChatStore,useUIStore } from '../../stores';
import type { CitationItem } from '../../types';
import {
parseContextCompactionState,
parseContextUsageSnapshot
} from '../../utils/contextUsage';
import { parseQueuedRunHandoff } from '../../utils/streamHandoff';
import {
deferThinkingTextFragmentBeforeTool
} from '../../utils/streamSegments';
import { resolveToolCardIndex,toolCardIndexById } from '../../utils/toolMatching';
import { _seenRuns,applySubagentEvent,canOpenPluginCanvasForChat,canvasTabTitle,findAutoCanvas } from './runtime';
import type { ChatStreamState } from './state';

import { appendRevisionText,appendRevisionThinking,autoOpenOntologySidebar,claimPersistedBubble,ensureOntologyRevision,finalizeRunningTools,getEventToolDisplayName,getEventToolId,getEventToolRawName,toolStartedAt } from './messageReducer';
import { appendOrUpdate,applySteerBoundary } from './messageStore';
export function reduceLifecycleAndTools(s: ChatStreamState,eventObj: Record<string, unknown>,obj: Record<string, unknown>, eventType: string,ontologyRevisionTool: boolean): boolean {
if (eventType === 'run_started') {
    const runId = typeof eventObj.run_id === 'string' ? eventObj.run_id : '';
    const messageId = typeof eventObj.message_id === 'string' ? eventObj.message_id : '';
    if (runId) {
        // 有上限地记一笔：单页会话再长也不该让这个集合无限涨
        if (_seenRuns.size > 200)
            _seenRuns.clear();
        _seenRuns.add(runId);
        useChatStore.getState().setActiveRun(s.chatId, { runId, messageId });
    }
    if (messageId) {
        // 第一帧就认领服务端身份：后端在接纳轮次时已经建好了这一行，历史里
        // 若已经把它渲染出来（重载/刷新后跟随），就直接接管那个气泡、丢掉本地
        // 占位，不再另起一个。
        s.metaMessageId = messageId;
        claimPersistedBubble(s);
        appendOrUpdate(s, true, undefined, messageId);
    }
    // 服务端记的本轮真实起点（chat_runs.started_at，恢复时不重写）。接管一轮
    // 已经在跑的对话时，用时要从它算起，而不是从本地这只气泡诞生的一刻。
    if (typeof eventObj.started_at === 'number') {
        s.bubbleStartedAt = eventObj.started_at;
    }
    return true;
}
if (eventType === 'steer_applied') {
    applySteerBoundary(s, eventObj);
    return true;
}
if (eventType === 'queued_run_started') {
    s.queuedRun = parseQueuedRunHandoff(eventObj);
    return true;
}
if (eventType === 'vision_progress') {
    // 视觉桥在模型开口前先把图转成文字证据，这段是纯网络等待。不报出来的话，
    // 界面上只有一个笼统的「深度拥抱中」在走秒，用户不知道系统在干什么。
    const running = eventObj.status === 'running';
    const count = typeof eventObj.count === 'number' ? eventObj.count : 1;
    useChatStore.getState().setVisionReading(s.chatId, running ? count : 0);
    return true;
}
if (eventType === 'compaction_notice') {
    // Earlier context was compacted in the background after the previous turn ended;
    // the backend notifies once in this turn's first frame
    // → ChatArea shows a dismissible notice bar
    const chatStore = useChatStore.getState();
    chatStore.setCompactionNotice(s.chatId);
    const contextCompaction = parseContextCompactionState(eventObj.context_compaction);
    if (contextCompaction)
        chatStore.setContextCompaction(s.chatId, contextCompaction);
    return true;
}
if (eventType === 'context_usage') {
    const contextUsage = parseContextUsageSnapshot(eventObj);
    if (contextUsage)
        useChatStore.getState().setContextUsage(s.chatId, contextUsage);
    return true;
}
if (eventType === 'end') {
    if (finalizeRunningTools(s))
        appendOrUpdate(s, true);
    s.streamEnded = true;
    return true;
}
if (eventType === 'error') {
    s.streamEnded = true;
    const streamError = typeof obj.error === 'string' ? obj.error : t('流式响应异常');
    // A server error frame is terminal for this run. Suspended question
    // tools are cancelled with it, but their resolved signal may not be
    // drained before task cancellation writes the terminal frame.
    const ui = useUIStore.getState();
    for (const request of ui.pendingUserQuestions[s.chatId] ?? []) {
        ui.resolvePendingUserQuestion(s.chatId, request.requestId);
    }
    if (s.ontologyGovernance?.review.status === 'running') {
        s.ontologyGovernance = {
            ...s.ontologyGovernance,
            review: {
                ...s.ontologyGovernance.review,
                status: 'failed',
                verdict: 'escalate',
                revised: false,
                error: streamError,
                feedback: [t('自动评审未完成，原文已保留。')],
                manual_review: {
                    required: true,
                    title: t('领域本体人工复核'),
                    summary: t('自动评审未完成，原文已保留，请重新发起评审或人工核对。'),
                    items: [],
                    actions: [],
                },
            },
        };
        appendOrUpdate(s, false, s.allCitations);
    }
    throw new Error(streamError);
}
if (eventType === 'tool_pending' && ontologyRevisionTool) {
    const revision = ensureOntologyRevision(s);
    revision.status = 'streaming';
    revision.toolPending = true;
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'tool_pending') {
    if (!s.toolPending) {
        s.toolPending = true;
        appendOrUpdate(s, true);
    }
    return true;
}
if (s.toolPending && eventType !== 'heartbeat') {
    s.toolPending = false;
    appendOrUpdate(s, true);
}
if (eventType === 'ontology_repair') {
    const revision = ensureOntologyRevision(s, typeof eventObj.source === 'string' ? eventObj.source : undefined);
    revision.status = eventObj.status === 'completed' ? 'completed' : 'streaming';
    if (eventObj.status === 'started')
        autoOpenOntologySidebar(s);
    if (eventObj.status === 'started' || eventObj.status === 'completed') {
        revision.toolPending = false;
    }
    if (typeof eventObj.tool_calls === 'number')
        revision.toolCallCount = eventObj.tool_calls;
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'ontology_revision_thinking') {
    const content = String(eventObj.delta || eventObj.content || '');
    if (content)
        appendRevisionThinking(s, content);
    ensureOntologyRevision(s).toolPending = false;
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'ontology_revision') {
    const content = String(eventObj.delta || eventObj.content || '');
    // The backend has already separated pre-wrapper reasoning from the
    // <ontology_revision> body, so every delta can be rendered directly.
    // Sending it through the outer <think> state machine would buffer the
    // whole candidate as hidden reasoning when no </think> tag follows.
    const revision = ensureOntologyRevision(s);
    revision.status = 'streaming';
    revision.toolPending = false;
    autoOpenOntologySidebar(s);
    if (content)
        appendRevisionText(s, content);
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (ontologyRevisionTool && (eventType === 'tool_use' || eventType === 'tool_call_start' || eventType === 'tool_call' || eventType === 'tool_start')) {
    const revision = ensureOntologyRevision(s);
    revision.toolPending = false;
    const eventToolId = getEventToolId(s, eventObj);
    const existingIndex = eventToolId ? toolCardIndexById(revision.toolCalls, eventToolId) : -1;
    const toolInput = eventObj.input ?? eventObj.args ?? eventObj.tool_args ?? eventObj.arguments;
    const rawName = getEventToolRawName(s, eventObj) || t('工具调用');
    const displayName = getEventToolDisplayName(s, eventObj);
    if (existingIndex >= 0) {
        revision.toolCalls[existingIndex] = {
            ...revision.toolCalls[existingIndex],
            input: toolInput,
            status: 'running',
        };
    }
    else {
        revision.toolCalls = [...revision.toolCalls, {
                id: eventToolId || `ontology_tool_${Date.now()}_${revision.toolCalls.length}`,
                name: rawName,
                displayName,
                input: toolInput,
                status: 'running',
                timestamp: toolStartedAt(s, eventObj),
                scope: 'ontology_revision',
            }];
    }
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (ontologyRevisionTool && eventType === 'tool_call_delta') {
    const revision = ensureOntologyRevision(s);
    revision.toolPending = false;
    const eventToolId = getEventToolId(s, eventObj);
    const delta = typeof eventObj.arguments_delta === 'string' ? eventObj.arguments_delta : '';
    const index = eventToolId ? toolCardIndexById(revision.toolCalls, eventToolId) : -1;
    if (index < 0) {
        revision.toolCalls = [...revision.toolCalls, {
                id: eventToolId || `ontology_tool_${Date.now()}_${revision.toolCalls.length}`,
                name: getEventToolRawName(s, eventObj) || t('工具调用'),
                inputText: delta,
                status: 'running',
                timestamp: toolStartedAt(s, eventObj),
                scope: 'ontology_revision',
            }];
    }
    else if (delta) {
        revision.toolCalls[index] = {
            ...revision.toolCalls[index],
            inputText: (revision.toolCalls[index].inputText || '') + delta,
            status: 'running',
        };
    }
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (ontologyRevisionTool && (eventType === 'tool_result' || eventType === 'tool_end')) {
    const revision = ensureOntologyRevision(s);
    revision.toolPending = false;
    const eventToolId = getEventToolId(s, eventObj);
    const toolName = getEventToolRawName(s, eventObj);
    // 同上：带 id 就只认 id，认不到就往下走「新建一张卡」，不按名字抢别人的。
    const index = resolveToolCardIndex(revision.toolCalls, eventToolId, toolName);
    const output = eventObj.output ?? eventObj.result;
    if (index >= 0) {
        revision.toolCalls[index] = {
            ...revision.toolCalls[index],
            output,
            status: obj.error ? 'error' : 'success',
        };
    }
    else {
        revision.toolCalls = [...revision.toolCalls, {
                id: eventToolId,
                name: toolName || t('工具调用'),
                output,
                status: obj.error ? 'error' : 'success',
                timestamp: toolStartedAt(s, eventObj),
                scope: 'ontology_revision',
            }];
    }
    if (Array.isArray(eventObj.citations)) {
        s.allCitations = [...s.allCitations, ...(eventObj.citations as CitationItem[])];
    }
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (ontologyRevisionTool && eventType === 'subagent_event') {
    const revision = ensureOntologyRevision(s);
    revision.toolPending = false;
    if (applySubagentEvent(revision.toolCalls, eventObj)) {
        if (Array.isArray(eventObj.citations)) {
            s.allCitations = [...s.allCitations, ...(eventObj.citations as CitationItem[])];
        }
        appendOrUpdate(s, true, s.allCitations);
    }
    return true;
}
if (eventType === 'tool_use' || eventType === 'tool_call_start' || eventType === 'tool_call' || eventType === 'tool_start') {
    const eventToolId = getEventToolId(s, eventObj);
    const existingIndex = eventToolId ? toolCardIndexById(s.toolCalls, eventToolId) : -1;
    const toolInput = eventObj.input ?? eventObj.args ?? eventObj.tool_args ?? eventObj.arguments;
    const rawName = getEventToolRawName(s, eventObj);
    const displayName = getEventToolDisplayName(s, eventObj);
    let activeToolId = eventToolId;
    let activeToolName = rawName;
    if (existingIndex >= 0) {
        const existing = s.toolCalls[existingIndex];
        s.toolCalls[existingIndex] = { ...existing, name: rawName || existing.name, displayName: displayName || existing.displayName, input: toolInput ?? existing.input, status: 'running' };
        activeToolId = s.normalizeToolId(s.toolCalls[existingIndex].id);
        activeToolName = s.toolCalls[existingIndex].name;
    }
    else {
        activeToolId = eventToolId || `tool_${Date.now()}_${s.toolCalls.length}`;
        s.toolCalls.push({ id: activeToolId, name: rawName || t('工具调用'), displayName, input: toolInput, status: 'running', timestamp: toolStartedAt(s, eventObj) });
        s.deferredThinkingText = deferThinkingTextFragmentBeforeTool(s.segments, s.enableThinking, s.deferredThinkingText);
        s.segments.push({ type: 'tool', toolIndex: s.toolCalls.length - 1 });
    }
    const pendingCanvas = findAutoCanvas(activeToolName);
    if (pendingCanvas && canOpenPluginCanvasForChat(s.chatId)) {
        useCanvasStore.getState().openPluginView({
            chatId: s.chatId,
            slug: pendingCanvas.slug,
            canvasId: pendingCanvas.id,
            toolId: activeToolId,
            toolName: activeToolName,
            title: canvasTabTitle(pendingCanvas, toolInput),
            status: 'loading',
        });
    }
    appendOrUpdate(s, true);
    return true;
}
if (eventType === 'tool_call_delta') {
    const eventToolId = getEventToolId(s, eventObj);
    const delta = typeof eventObj.arguments_delta === 'string' ? eventObj.arguments_delta : '';
    const index = eventToolId ? toolCardIndexById(s.toolCalls, eventToolId) : -1;
    if (index < 0) {
        s.toolCalls.push({
            id: eventToolId || `tool_${Date.now()}_${s.toolCalls.length}`,
            name: getEventToolRawName(s, eventObj) || t('工具调用'),
            inputText: delta,
            status: 'running',
            timestamp: Date.now(),
        });
        s.deferredThinkingText = deferThinkingTextFragmentBeforeTool(s.segments, s.enableThinking, s.deferredThinkingText);
        s.segments.push({ type: 'tool', toolIndex: s.toolCalls.length - 1 });
    }
    else if (delta) {
        s.toolCalls[index] = {
            ...s.toolCalls[index],
            inputText: (s.toolCalls[index].inputText || '') + delta,
            status: 'running',
        };
    }
    appendOrUpdate(s, true);
    return true;
}
return false;
}