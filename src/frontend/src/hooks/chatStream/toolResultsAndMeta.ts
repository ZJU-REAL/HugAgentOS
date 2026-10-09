import { t } from '../../i18n';
import { useCanvasStore,useChatStore,useUIStore } from '../../stores';
import type { CitationItem,ToolCall } from '../../types';
import { activateStructuredReasoning,rearmInlineReasoning } from './messageReducer';
import { applySubagentEvent,canOpenPluginCanvasForChat,canvasTabTitle,findAutoCanvas,maybeRefreshCatalogAfterTool } from './runtime';
import type { ChatStreamState } from './state';

import { appendLateStructuredThinkContent,appendThinkContent,autoOpenOntologySidebar,ensureOntologyGovernance,ensureOntologyRevision,findToolCallIndex,getEventToolDisplayName,getEventToolId,getEventToolRawName,toolStartedAt } from './messageReducer';
import { appendOrUpdate } from './messageStore';
export function reduceToolResultsAndMeta(s: ChatStreamState,eventObj: Record<string, unknown>,obj: Record<string, unknown>, eventType: string): boolean {
if (eventType === 'tool_result' || eventType === 'tool_end') {
    const toolIndex = findToolCallIndex(s, eventObj);
    const status: ToolCall['status'] = obj.error || eventObj.status === 'error'
        ? 'error'
        : eventObj.status === 'interrupted'
            ? 'interrupted'
            : 'success';
    const output = eventObj.output ?? eventObj.result;
    let resultDisplayName: string | undefined;
    if (typeof obj.subagent_name === 'string' && obj.subagent_name.trim()) {
        resultDisplayName = t('调用智能体：{name}', { name: obj.subagent_name.trim() });
    }
    // 服务端算好的本次调用耗时。卡片收尾后显示的是这个定值，不再靠两次
    // 本地取时相减——历史重放时那两个时刻早就不在了。
    const toolDurationMs = typeof eventObj.duration_ms === 'number' ? eventObj.duration_ms : undefined;
    let confirmToolName = '';
    if (toolIndex >= 0) {
        const existing = s.toolCalls[toolIndex];
        confirmToolName = existing.name;
        s.toolCalls[toolIndex] = { ...existing, output: output ?? existing.output, status, ...(resultDisplayName ? { displayName: resultDisplayName } : {}), ...(toolDurationMs !== undefined ? { durationMs: toolDurationMs } : {}) };
    }
    else {
        confirmToolName = getEventToolRawName(s, eventObj) || t('工具调用');
        s.toolCalls.push({ id: getEventToolId(s, eventObj) || `tool_${Date.now()}_${s.toolCalls.length}`, name: confirmToolName, displayName: resultDisplayName || getEventToolDisplayName(s, eventObj), output, status, timestamp: toolStartedAt(s, eventObj), ...(toolDurationMs !== undefined ? { durationMs: toolDurationMs } : {}) });
        s.segments.push({ type: 'tool', toolIndex: s.toolCalls.length - 1 });
    }
    maybeRefreshCatalogAfterTool(confirmToolName, status || 'success');
    const completedCanvas = findAutoCanvas(confirmToolName);
    // An interrupted run leaves the tab in its loading state rather than
    // committing a half-finished payload to the canvas.
    if (completedCanvas && status !== 'interrupted' && canOpenPluginCanvasForChat(s.chatId)) {
        const completedTool = toolIndex >= 0 ? s.toolCalls[toolIndex] : s.toolCalls[s.toolCalls.length - 1];
        const toolId = s.normalizeToolId(completedTool?.id);
        const canvas = useCanvasStore.getState();
        const patch = {
            chatId: s.chatId,
            slug: completedCanvas.slug,
            canvasId: completedCanvas.id,
            toolId,
            toolName: confirmToolName,
            title: canvasTabTitle(completedCanvas, completedTool?.input),
            status,
            output: output ?? completedTool?.output,
            ...(status === 'error'
                ? { error: String(eventObj.error || t('加载失败')) }
                : { error: undefined }),
        } as const;
        const targetMatches = canvas.activeView === 'plugin'
            && canvas.pluginTarget?.chatId === s.chatId
            && canvas.pluginTarget.canvasId === completedCanvas.id
            && (!canvas.pluginTarget.toolId || canvas.pluginTarget.toolId === toolId);
        if (targetMatches)
            canvas.updatePluginView(patch);
        else
            canvas.openPluginView(patch);
    }
    // Arrival of choose_design's tool_result = the pick is complete (clicked/skipped/
    // timed out). Whether this stream is live or a replay (replay re-emits design_pick
    // events, but the pick result only shows up in this tool_result), dismiss the pick
    // card on it to prevent zombies.
    if (confirmToolName === 'choose_design') {
        useUIStore.getState().setPendingDesignPick(s.chatId, null);
    }
    if (Array.isArray(eventObj.citations))
        s.allCitations = [...s.allCitations, ...(eventObj.citations as CitationItem[])];
    rearmInlineReasoning(s);
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'subagent_event') {
    // The sub-agent's internal streaming sub-steps — attached under the call_subagent tool card.
    if (applySubagentEvent(s.toolCalls, eventObj)) {
        appendOrUpdate(s, true, s.allCitations);
    }
    return true;
}
if (eventType === 'plan_update') {
    // The main agent updated its lightweight plan checklist (update_plan tool).
    // Full-state replace of the plan bar above the input; the agent keeps
    // executing in this same turn, so nothing changes in the message flow.
    //
    // update_plan suppresses its tool_call/tool_result events (no tool card),
    // so the <think> stripper's usual "re-arm after tool result" never fires
    // here — re-arm it now, or the next model iteration's opener-less
    // reasoning (hybrid models omit <think> after tools) leaks into body text.
    rearmInlineReasoning(s);
    const rawSteps = Array.isArray(eventObj.steps) ? eventObj.steps : [];
    const steps = rawSteps
        .map((s) => {
        const o = s as Record<string, unknown>;
        const title = typeof o?.title === 'string' ? o.title.trim() : '';
        const status = o?.status === 'in_progress' || o?.status === 'completed'
            ? o.status : 'pending';
        return title ? { title, status: status as 'pending' | 'in_progress' | 'completed' } : null;
    })
        .filter((s): s is {
        title: string;
        status: 'pending' | 'in_progress' | 'completed';
    } => !!s);
    if (steps.length > 0) {
        useChatStore.getState().setPlanProgress(s.chatId, {
            source: 'agent',
            title: typeof eventObj.title === 'string' ? eventObj.title : '',
            steps,
            updatedAt: Date.now(),
        });
    }
    return true;
}
if (eventType === 'ontology_activation') {
    const governance = ensureOntologyGovernance(s, eventObj);
    const activation = {
        pack_id: typeof eventObj.pack_id === 'string' ? eventObj.pack_id : undefined,
        workflow_id: typeof eventObj.workflow_id === 'string' ? eventObj.workflow_id : undefined,
        workflow_name: typeof eventObj.workflow_name === 'string' ? eventObj.workflow_name : undefined,
        source: typeof eventObj.source === 'string' ? eventObj.source : 'text',
        asset_kind: typeof eventObj.asset_kind === 'string' ? eventObj.asset_kind : undefined,
        asset_id: typeof eventObj.asset_id === 'string' ? eventObj.asset_id : undefined,
        review_level: typeof eventObj.review_level === 'string' ? eventObj.review_level : undefined,
    };
    const activationKey = `${activation.pack_id || ''}:${activation.workflow_id || ''}`;
    if (!governance.activations.some((item) => `${item.pack_id || ''}:${item.workflow_id || ''}` === activationKey)) {
        governance.activations = [...governance.activations, activation];
    }
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'ontology_gate') {
    const governance = ensureOntologyGovernance(s, eventObj);
    governance.gates = [...governance.gates, {
            decision: typeof eventObj.decision === 'string' ? eventObj.decision : 'pass',
            tool_name: typeof eventObj.tool_name === 'string' ? eventObj.tool_name : undefined,
            matched_rule_ids: Array.isArray(eventObj.matched_rule_ids)
                ? eventObj.matched_rule_ids.filter((item): item is string => typeof item === 'string')
                : [],
            violations: Array.isArray(eventObj.violations)
                ? eventObj.violations.filter((item): item is string => typeof item === 'string')
                : [],
            denial_count: typeof eventObj.denial_count === 'number' ? eventObj.denial_count : undefined,
            circuit_breaker: eventObj.circuit_breaker === true,
        }];
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'ontology_review') {
    const governance = ensureOntologyGovernance(s, eventObj);
    const status = typeof eventObj.status === 'string' ? eventObj.status : '';
    const level = typeof eventObj.level === 'string' ? eventObj.level : 'checkpoint';
    const committeeSize = typeof eventObj.committee_size === 'number'
        ? eventObj.committee_size
        : 3;
    governance.review = {
        ...governance.review,
        status: status === 'started' ? 'running' : status,
        level,
        committee_size: committeeSize,
        count: typeof eventObj.review_count === 'number' ? eventObj.review_count : governance.review.count,
        verdict: typeof eventObj.verdict === 'string' ? eventObj.verdict : governance.review.verdict,
        revised: typeof eventObj.revised === 'boolean' ? eventObj.revised : governance.review.revised,
        latency_ms: typeof eventObj.latency_ms === 'number' ? eventObj.latency_ms : governance.review.latency_ms,
        owner: typeof eventObj.review_owner === 'string' ? eventObj.review_owner : governance.review.owner,
        candidate_answer: typeof eventObj.candidate_answer === 'string'
            ? eventObj.candidate_answer
            : governance.review.candidate_answer,
        manual_review: eventObj.manual_review && typeof eventObj.manual_review === 'object'
            ? eventObj.manual_review as typeof governance.review.manual_review
            : governance.review.manual_review,
        violations: Array.isArray(eventObj.violations)
            ? eventObj.violations as Array<Record<string, unknown>>
            : governance.review.violations,
        affected_claims: Array.isArray(eventObj.affected_claims)
            ? eventObj.affected_claims as typeof governance.review.affected_claims
            : governance.review.affected_claims,
        evidence: Array.isArray(eventObj.evidence)
            ? eventObj.evidence.filter((item): item is string => typeof item === 'string')
            : governance.review.evidence,
        feedback: Array.isArray(eventObj.feedback)
            ? eventObj.feedback.filter((item): item is string => typeof item === 'string')
            : governance.review.feedback,
        new_tools: Array.isArray(eventObj.new_tools)
            ? eventObj.new_tools.filter((item): item is string => typeof item === 'string')
            : governance.review.new_tools,
        new_citation_count: typeof eventObj.new_citation_count === 'number'
            ? eventObj.new_citation_count
            : governance.review.new_citation_count,
    };
    if (status === 'started')
        autoOpenOntologySidebar(s);
    if (status === 'completed') {
        const candidate = governance.review.candidate_answer || '';
        if (candidate || governance.revision) {
            const revision = ensureOntologyRevision(s);
            revision.status = 'completed';
            revision.toolPending = false;
            if (candidate)
                revision.content = candidate;
        }
    }
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'thinking' || eventType === 'thought') {
    // Structured reasoning channel (e.g. DeepSeek v4
    // `reasoning_content`): thinking is delivered via this SSE
    // event, not embedded in `content` as <think>...</think>.
    // Disable the embed-tag parser so subsequent content chunks
    // are not treated as buffered thinking.
    if (eventObj.structured_reasoning === true) activateStructuredReasoning(s);
    if (obj.delta) {
        s.structuredReasoning = true;
        if (s.thinkingPhaseActive && s.parseBuffer) {
            // Buffered during the thinking phase → it is reasoning, not
            // body text. Keep it in the thinking channel. (Previously this
            // flushed to text, leaking reasoning for models that mix the
            // structured channel with inline <think> content.)
            appendThinkContent(s, s.parseBuffer, true);
            s.parseBuffer = '';
        }
        s.thinkingPhaseActive = false;
    }
    const followsVisibleAnswerText = s.segments[s.segments.length - 1]?.type === 'text';
    const thinkContent = (obj.content || obj.text || obj.delta || '') as string;
    if (thinkContent) {
        if (followsVisibleAnswerText)
            appendLateStructuredThinkContent(s, thinkContent);
        else
            appendThinkContent(s, thinkContent, !!obj.delta);
        appendOrUpdate(s, true);
    }
    return true;
}
return false;
}