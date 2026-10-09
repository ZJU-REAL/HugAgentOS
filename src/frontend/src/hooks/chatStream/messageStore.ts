import { useChatStore } from '../../stores';
import type { ChatItem,ChatMessage,CitationItem } from '../../types';
import { newMessageUid } from '../../utils/messageIdentity';
import {
liftTrailingSegmentsAboveFinalText,
restoreDeferredThinkingTextFragment
} from '../../utils/streamSegments';
import type { ChatStreamState } from './state';

import { adoptPersistedBubble,appendTextSeg,appendThinkContent,findOwnBubble,reclassifyImplicitThinking } from './messageReducer';
export function commitUpdate(s: ChatStreamState, streaming: boolean, cits?: CitationItem[], persistedMessageId?: string) {
    useChatStore.getState().updateStore((prev) => {
        const c = prev.chats[s.chatId];
        const msgs = adoptPersistedBubble(s, [...(c?.messages || [])]);
        // While the model hasn't produced any real content yet (MiniMax may buffer the whole
        // turn), show the placeholder notice instead of an empty bubble. The placeholder never
        // enters full/segments, so it is never persisted.
        const body = (!s.full && streaming && s.toolCalls.length === 0 && s.thinking.length === 0
            ? (s.pendingNotice || '')
            : s.full);
        const isMd = body.includes('\n') || body.includes('```') || body.includes('**') || /^\s*#\s/m.test(body);
        const updatedMsg: Partial<ChatMessage> & {
            content: string;
            isMarkdown: boolean;
            isStreaming: boolean;
        } = {
            content: body,
            isMarkdown: isMd,
            toolCalls: s.toolCalls.length > 0 ? [...s.toolCalls] : undefined,
            thinking: s.thinking.length > 0 ? [...s.thinking] : undefined,
            evolution: s.evolutionSummary,
            ontologyGovernance: s.ontologyGovernance ? {
                ...s.ontologyGovernance,
                activations: [...s.ontologyGovernance.activations],
                gates: [...s.ontologyGovernance.gates],
                review: { ...s.ontologyGovernance.review },
                revision: s.ontologyGovernance.revision
                    ? {
                        ...s.ontologyGovernance.revision,
                        thinking: [...s.ontologyGovernance.revision.thinking],
                        toolCalls: [...s.ontologyGovernance.revision.toolCalls],
                    }
                    : undefined,
            }
                : undefined,
            segments: s.segments.length > 0 ? [...s.segments] : undefined,
            isStreaming: streaming,
            inFlight: streaming && s.runId ? { eventOffset: s.eventOffset } : undefined,
            toolPending: streaming && s.toolPending,
            // Persisted activity stamp — anchors the "正在准备调用工具…" timer so
            // it survives a session switch / refresh remount (see useStallDetector).
            // 取服务端下发的时刻而非本地钟：另一台设备接上同一轮时读到的是同一个值。
            lastActivityTs: s.lastEventTs,
        };
        if (persistedMessageId)
            updatedMsg.messageId = persistedMessageId;
        if (!streaming) {
            updatedMsg.durationMs = s.metaDurationMs ?? (Date.now() - s.bubbleStartedAt);
            updatedMsg.inFlight = undefined;
        }
        if (cits !== undefined)
            updatedMsg.citations = cits.length > 0 ? cits : undefined;
        if (s.metaFollowUps.length > 0)
            updatedMsg.followUpQuestions = s.metaFollowUps;
        const idx = findOwnBubble(s, msgs);
        if (idx >= 0) {
            msgs[idx] = { ...msgs[idx], ...updatedMsg };
        }
        else {
            msgs.push({ role: 'assistant', uid: s.bubbleUid, ts: s.bubbleStartedAt, ...updatedMsg });
        }
        // Don't bump updatedAt / reorder on every SSE chunk — otherwise when two chats stream
        // simultaneously, the sidebar's updatedAt sort keeps lifting each to the top in turn and
        // the list starts bouncing. The initiator already moved the chat to the front, and the
        // final update at stream end bumps it once more; the in-between just needs to stay stable.
        const nextChat: ChatItem = { ...(c as ChatItem), messages: msgs };
        return { chats: { ...prev.chats, [s.chatId]: nextChat }, order: prev.order };
    });
}
export function streamingCommitDelay(s: ChatStreamState) { return Math.min(250, Math.max(50, Math.round(s.full.length / 1000))); }
export function cancelStreamingUpdate(s: ChatStreamState) {
    if (s.streamingUpdateTimer != null) {
        clearTimeout(s.streamingUpdateTimer);
        s.streamingUpdateTimer = null;
    }
    s.pendingStreamingUpdate = null;
}
export function flushStreamingUpdate(s: ChatStreamState) {
    if (s.streamingUpdateTimer != null) {
        clearTimeout(s.streamingUpdateTimer);
        s.streamingUpdateTimer = null;
    }
    const pending = s.pendingStreamingUpdate;
    s.pendingStreamingUpdate = null;
    if (!pending)
        return;
    s.lastStreamingCommitAt = Date.now();
    commitUpdate(s, true, pending.cits, pending.persistedMessageId);
}
export function appendOrUpdate(s: ChatStreamState, streaming: boolean, cits?: CitationItem[], persistedMessageId?: string) {
    if (!streaming) {
        // 终态先把待写的增量丢掉：它携带的是同一份可变状态的旧快照，
        // 而下面这次写入本来就带着最新的全量内容。
        cancelStreamingUpdate(s);
        s.lastStreamingCommitAt = Date.now();
        commitUpdate(s, false, cits, persistedMessageId);
        return;
    }
    // 参数按"最后给出的非空值"合并：合并窗口内多次调用只有一次落盘，
    // 但引用列表 / message_id 这类附带信息不能被后来的裸调用抹掉。
    s.pendingStreamingUpdate = {
        cits: cits !== undefined ? cits : s.pendingStreamingUpdate?.cits,
        persistedMessageId: persistedMessageId ?? s.pendingStreamingUpdate?.persistedMessageId,
    };
    if (s.streamingUpdateTimer != null)
        return;
    const delay = streamingCommitDelay(s);
    const elapsed = Date.now() - s.lastStreamingCommitAt;
    // 首帧与空闲后的第一帧立即出，不给用户"迟迟不吐字"的观感。
    if (elapsed >= delay) {
        flushStreamingUpdate(s);
        return;
    }
    s.streamingUpdateTimer = setTimeout(() => flushStreamingUpdate(s), delay - elapsed);
}
export function applySteerBoundary(s: ChatStreamState, eventObj: Record<string, unknown>) {
    // Finish any half-buffered inline-reasoning token before freezing the
    // current assistant bubble. The next model iteration starts a fresh bubble.
    if (s.parseBuffer) {
        if (s.thinkingPhaseActive && s.sawThinkCloseTag)
            appendThinkContent(s, s.parseBuffer, true);
        else
            appendTextSeg(s, s.parseBuffer);
        s.parseBuffer = '';
    }
    reclassifyImplicitThinking(s);
    s.deferredThinkingText = restoreDeferredThinkingTextFragment(s.segments, s.deferredThinkingText);
    // 冻结气泡前收尾：工具卡/思考块不留在最终答案之后（与历史重建同一规则）
    liftTrailingSegmentsAboveFinalText(s.segments);
    const previousAssistantMessageId = typeof eventObj.previous_assistant_message_id === 'string'
        ? eventObj.previous_assistant_message_id
        : undefined;
    const nextAssistantMessageId = typeof eventObj.next_assistant_message_id === 'string'
        ? eventObj.next_assistant_message_id
        : undefined;
    const steerMessageId = typeof eventObj.message_id === 'string'
        ? eventObj.message_id
        : undefined;
    const steerMessage = typeof eventObj.message === 'string' ? eventObj.message.trim() : '';
    const hasAssistantOutput = s.full.length > 0
        || s.toolCalls.length > 0
        || s.thinking.length > 0
        || s.segments.length > 0;
    if (hasAssistantOutput) {
        appendOrUpdate(s, false, s.allCitations, previousAssistantMessageId);
    }
    let steerMessageTs = Date.now();
    useChatStore.getState().updateStore((prev) => {
        const chat = prev.chats[s.chatId];
        if (!chat)
            return prev;
        const messages = [...chat.messages];
        let assistantIndex = messages.findIndex((item) => item.uid === s.bubbleUid);
        if (!hasAssistantOutput && assistantIndex >= 0) {
            messages.splice(assistantIndex, 1);
            assistantIndex -= 1;
        }
        const existingUserIndex = steerMessageId
            ? messages.findIndex((item) => item.messageId === steerMessageId)
            : -1;
        if (existingUserIndex >= 0) {
            steerMessageTs = messages[existingUserIndex].ts;
        }
        else if (steerMessage) {
            steerMessageTs = Math.max(Date.now(), s.bubbleStartedAt + 1);
            const userMessage: ChatMessage = {
                role: 'user',
                content: steerMessage,
                isMarkdown: false,
                uid: newMessageUid(),
                ts: steerMessageTs,
                messageId: steerMessageId,
            };
            messages.splice(assistantIndex >= 0 ? assistantIndex + 1 : messages.length, 0, userMessage);
        }
        return {
            ...prev,
            chats: {
                ...prev.chats,
                [s.chatId]: { ...chat, messages, updatedAt: Date.now() },
            },
        };
    });
    // The queue card has now become a real chronological user message.
    useChatStore.getState().setQueuedMessage(s.chatId, null);
    s.full = '';
    s.toolCalls = [];
    s.thinking.length = 0;
    s.segments.length = 0;
    s.ontologyGovernance = undefined;
    s.evolutionSummary = undefined;
    s.metaMessageId = nextAssistantMessageId;
    s.metaFollowUps = [];
    s.metaDurationMs = null;
    s.allCitations = [];
    s.metaWorkspaceFiles = null;
    s.parseBuffer = '';
    s.deferredThinkingText = undefined;
    s.toolPending = false;
    s.implicitThinkSegIdxs.clear();
    s.sawThinkCloseTag = false;
    s.thinkingPhaseActive = s.enableThinking && !s.structuredReasoning;
    s.bubbleUid = newMessageUid();
    s.bubbleStartedAt = Math.max(Date.now(), steerMessageTs + 1);
    appendOrUpdate(s, true);
}
