import { message } from 'antd';
import {
toFileConfirmInfo,
toUserQuestionRequest
} from '../../api';
import { t } from '../../i18n';
import { useBatchStore,useChatStore,useUIStore } from '../../stores';
import type { CitationItem,EvolutionSummary,OntologyGovernanceSummary } from '../../types';
import {
parseContextUsageSnapshot
} from '../../utils/contextUsage';
import { applyDesignPickEvent } from './runtime';
import type { ChatStreamState } from './state';

import { appendArtifactsToStreamToolCalls,appendThinkContent,replaceAnswerText } from './messageReducer';
import { appendOrUpdate } from './messageStore';
export function reduceControls(s: ChatStreamState,eventObj: Record<string, unknown>,obj: Record<string, unknown>, eventType: string): boolean {
if (eventType === 'meta') {
    if (typeof eventObj.message_id === 'string')
        s.metaMessageId = eventObj.message_id;
    if (typeof eventObj.duration_ms === 'number' && eventObj.duration_ms >= 0) {
        s.metaDurationMs = eventObj.duration_ms;
    }
    const contextUsage = parseContextUsageSnapshot(eventObj.context_usage);
    if (contextUsage)
        useChatStore.getState().setContextUsage(s.chatId, contextUsage);
    s.compactionPending = eventObj.compaction_pending === true;
    if (Array.isArray(eventObj.citations) && (eventObj.citations as CitationItem[]).length > 0) {
        s.allCitations = eventObj.citations as CitationItem[];
    }
    if (Array.isArray(eventObj.workspace_files)) {
        s.metaWorkspaceFiles = (eventObj.workspace_files as unknown[])
            .filter((x): x is string => typeof x === 'string' && x.trim().length > 0);
    }
    if (eventObj.evolution_pending && typeof eventObj.evolution_pending === 'object') {
        const pending = eventObj.evolution_pending as Partial<EvolutionSummary>;
        // A watch token, not something to render: the card stays absent
        // until settlement reports what was actually written.
        s.evolutionSummary = {
            state: 'pending',
            message_id: typeof pending.message_id === 'string' ? pending.message_id : undefined,
        };
    }
    if (eventObj.ontology_governance && typeof eventObj.ontology_governance === 'object') {
        const persisted = eventObj.ontology_governance as Partial<OntologyGovernanceSummary>;
        const persistedReview = persisted.review && typeof persisted.review === 'object'
            ? persisted.review
            : {};
        const liveRevision = s.ontologyGovernance?.revision;
        const persistedCandidate = typeof persistedReview.candidate_answer === 'string'
            ? persistedReview.candidate_answer
            : '';
        s.ontologyGovernance = {
            governance_run_id: persisted.governance_run_id,
            activations: Array.isArray(persisted.activations) ? persisted.activations : [],
            gates: Array.isArray(persisted.gates) ? persisted.gates : [],
            review: persistedReview,
            revision: liveRevision
                ? {
                    ...liveRevision,
                    status: 'completed',
                    content: persistedCandidate || liveRevision.content,
                }
                : persistedCandidate
                    ? {
                        status: 'completed',
                        content: persistedCandidate,
                        thinking: [],
                        toolCalls: [],
                    }
                    : undefined,
        };
    }
    appendArtifactsToStreamToolCalls(s, Array.isArray(eventObj.artifacts) ? eventObj.artifacts : []);
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
if (eventType === 'batch_confirm') {
    // batch_runner MCP returned a plan; backend has paused the agent.
    // Open the confirmation modal so the user can review/edit the
    // prompt template before any item executes.
    const planId = typeof eventObj.plan_id === 'string' ? eventObj.plan_id : '';
    if (planId) {
        useBatchStore.getState().setPendingConfirm({
            plan_id: planId,
            total: typeof eventObj.total === 'number' ? eventObj.total : 0,
            source_type: (eventObj.source_type || 'text_list') as 'xlsx' | 'word_files' | 'text_list',
            preview: Array.isArray(eventObj.preview)
                ? (eventObj.preview as Record<string, unknown>[]) : [],
            default_template: typeof eventObj.default_template === 'string'
                ? eventObj.default_template : '',
            placeholder_keys: Array.isArray(eventObj.placeholder_keys)
                ? (eventObj.placeholder_keys as string[]) : [],
            chat_id: typeof eventObj.chat_id === 'string'
                ? eventObj.chat_id : undefined,
            warnings: Array.isArray(eventObj.warnings)
                ? (eventObj.warnings as string[]) : undefined,
        });
    }
    return true;
}
if (eventType === 'file_confirm') {
    // §13: some tool coroutine has suspended awaiting the user's confirmation of a
    // /myspace write. Show the confirm bar; this SSE stream does **not** end — the user's
    // allow/deny goes via an out-of-band POST /file-confirm, the suspended tool resumes in
    // place, and subsequent tool_result/meta still arrive on this same stream.
    if (eventObj.expired) {
        // The backend's confirmation-wait timeout reclaimed **that** pending item: remove
        // only this one confirm_id from the queue (leave the other queued items alone),
        // otherwise a user clicking much later would inevitably hit a dangling confirm_id error.
        const _cid = String(eventObj.confirm_id ?? '');
        if (_cid) {
            useUIStore.getState().resolvePendingConfirm(s.chatId, _cid);
            message.info(t('一项「我的空间」写确认已超时取消，如仍需要请重新发起。'));
        }
        return true;
    }
    const _info = toFileConfirmInfo(eventObj);
    if (_info.confirmId)
        useUIStore.getState().enqueuePendingConfirm(s.chatId, _info);
    return true;
}
if (eventType === 'design_pick') {
    // Site-design pick-one-of-three: the choose_design tool coroutine suspends awaiting
    // the user's pick. Same mechanism as file_confirm (suspend – out-of-band POST – resume
    // on the original stream); the UI uses a separate pick card.
    applyDesignPickEvent(s.chatId, eventObj);
    if (eventObj.expired)
        message.info(t('设计方案选择已超时，助手将自行选择方案继续。'));
    return true;
}
if (eventType === 'user_question') {
    // ask_user_question suspends the current run. The resident composer
    // replaces the ordinary input until the server resolves this exact
    // request; duplicate replay frames are deduped by request_id.
    const request = toUserQuestionRequest(eventObj);
    if (request.requestId && request.questions.length) {
        useUIStore.getState().enqueuePendingUserQuestion(s.chatId, request);
    }
    return true;
}
if (eventType === 'user_question_resolved') {
    // The backend owns the terminal state. A successful answer/cancel
    // POST is only an acknowledgement, so removal happens here (or via
    // the pending-question recovery endpoint after a reconnect).
    const requestId = String(eventObj.request_id ?? '');
    if (requestId) {
        useUIStore.getState().resolvePendingUserQuestion(s.chatId, requestId);
    }
    if (eventObj.outcome === 'timeout') {
        message.info(t('等待回答已超时，助手将采用稳妥的默认方案继续。'));
    }
    return true;
}
if (eventType === 'follow_up') {
    if (Array.isArray(eventObj.follow_up_questions) && eventObj.follow_up_questions.length > 0) {
        s.metaFollowUps = eventObj.follow_up_questions as string[];
        appendOrUpdate(s, true, s.allCitations);
    }
    return true;
}
if (eventType === 'content_replace') {
    const answer = (obj.content || obj.text || '') as string;
    if (s.parseBuffer) {
        if (s.thinkingPhaseActive)
            appendThinkContent(s, s.parseBuffer, true);
        s.parseBuffer = '';
    }
    s.thinkingPhaseActive = false;
    s.structuredReasoning = true;
    replaceAnswerText(s, answer);
    appendOrUpdate(s, true, s.allCitations);
    return true;
}
return false;
}