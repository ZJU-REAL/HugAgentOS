import { t } from '../../i18n';
import { panelFromPath } from '../../routing/navigation';
import { useCanvasStore,useChatStore } from '../../stores';
import type { ChatMessage } from '../../types';
import { stripMcpToolPrefix } from '../../utils/constants';
import { normalizeArtifactOutput } from '../../utils/fileParser';
import {
appendStreamTextSegment,
appendThinkingContentBeforeTrailingText
} from '../../utils/streamSegments';
import { resolveToolCardIndex } from '../../utils/toolMatching';
import { canAutoOpenCanvas } from './runtime';
import type { ChatStreamState } from './state';

export function ensureOntologyGovernance(s: ChatStreamState, eventObj?: Record<string, unknown>) {
    if (!s.ontologyGovernance) {
        s.ontologyGovernance = {
            governance_run_id: typeof eventObj?.governance_run_id === 'string' ? eventObj.governance_run_id : undefined,
            activations: [],
            gates: [],
            review: {},
        };
    }
    else if (!s.ontologyGovernance.governance_run_id && typeof eventObj?.governance_run_id === 'string') {
        s.ontologyGovernance = { ...s.ontologyGovernance, governance_run_id: eventObj.governance_run_id };
    }
    return s.ontologyGovernance;
}
export function reclassifyImplicitThinking(s: ChatStreamState): boolean {
    if (s.sawThinkCloseTag || s.implicitThinkSegIdxs.size === 0)
        return false;
    for (const i of s.implicitThinkSegIdxs) {
        const seg = s.segments[i];
        if (seg?.type === 'thinking' && seg.content) {
            s.segments[i] = { type: 'text', content: seg.content };
        }
    }
    s.implicitThinkSegIdxs.clear();
    // full 与 thinking 按重归类后的段重建（与 appendTextSeg 的顺序累加语义一致）
    s.full = s.segments.filter((s) => s.type === 'text').map((s) => s.content || '').join('');
    s.thinking.length = 0;
    for (const segment of s.segments) {
        if (segment.type === 'thinking' && segment.content)
            s.thinking.push({ content: segment.content, timestamp: Date.now() });
    }
    return true;
}
export function getPartialTagLen(_s: ChatStreamState, text: string, tag: string): number {
    for (let len = Math.min(tag.length - 1, text.length); len >= 1; len--) {
        if (tag.startsWith(text.slice(text.length - len)))
            return len;
    }
    return 0;
}
export function ensureOntologyRevision(s: ChatStreamState, source?: string) {
    const governance = ensureOntologyGovernance(s);
    if (!governance.revision) {
        governance.revision = {
            status: 'pending',
            source,
            content: '',
            thinking: [],
            toolCalls: [],
        };
    }
    else if (source && !governance.revision.source) {
        governance.revision = { ...governance.revision, source };
    }
    return governance.revision;
}
export function appendRevisionThinking(s: ChatStreamState, content: string) {
    if (!content)
        return;
    const revision = ensureOntologyRevision(s);
    const items = [...revision.thinking];
    const last = items[items.length - 1];
    if (last)
        items[items.length - 1] = { ...last, content: last.content + content };
    else
        items.push({ content, timestamp: Date.now() });
    revision.thinking = items;
}
export function appendRevisionText(s: ChatStreamState, content: string) {
    if (!content)
        return;
    const revision = ensureOntologyRevision(s);
    revision.content += content;
}
export function appendThinkContent(s: ChatStreamState, content: string, isDelta: boolean) {
    if (!content)
        return;
    const lastSeg = s.segments[s.segments.length - 1];
    const lastThink = isDelta && lastSeg?.type === 'thinking' ? lastSeg : null;
    if (lastThink) {
        s.segments[s.segments.length - 1] = {
            ...lastThink,
            content: (lastThink.content || '') + content,
        };
        if (s.thinking.length > 0) {
            const lastThinking = s.thinking[s.thinking.length - 1];
            s.thinking[s.thinking.length - 1] = {
                ...lastThinking,
                content: lastThinking.content + content,
            };
        }
        else {
            s.thinking.push({ content, timestamp: Date.now() });
        }
    }
    else {
        s.segments.push({ type: 'thinking', content });
        s.thinking.push({ content, timestamp: Date.now() });
    }
}
export function appendLateStructuredThinkContent(s: ChatStreamState, content: string) {
    if (!appendThinkingContentBeforeTrailingText(s.segments, content)) {
        appendThinkContent(s, content, true);
        return;
    }
    const lastThinking = s.thinking[s.thinking.length - 1];
    if (lastThinking) {
        s.thinking[s.thinking.length - 1] = {
            ...lastThinking,
            content: lastThinking.content + content,
        };
    }
}
export function appendTextSeg(s: ChatStreamState, text: string) {
    if (!text)
        return;
    s.full += text;
    s.deferredThinkingText = appendStreamTextSegment(s.segments, text, s.deferredThinkingText);
}
export function replaceAnswerText(s: ChatStreamState, text: string) {
    // Keep reasoning and tool chronology, but replace every draft text
    // segment with the committee-reviewed final answer. This event arrives
    // only when the review actually changed the draft.
    s.full = text;
    s.deferredThinkingText = undefined;
    for (let i = s.segments.length - 1; i >= 0; i--) {
        if (s.segments[i].type === 'text')
            s.segments.splice(i, 1);
    }
    if (text)
        s.segments.push({ type: 'text', content: text });
}
export function processTextChunk(s: ChatStreamState, chunk: string) {
    s.parseBuffer += chunk;
    while (s.parseBuffer.length > 0) {
        if (s.thinkingPhaseActive) {
            const openIdx = s.parseBuffer.indexOf('<think>');
            const closeIdx = s.parseBuffer.indexOf('</think>');
            // A redundant open tag while already in the thinking phase: drop the tag itself; text before it is still thinking.
            if (openIdx >= 0 && (closeIdx === -1 || openIdx < closeIdx)) {
                if (openIdx > 0)
                    appendThinkContent(s, s.parseBuffer.slice(0, openIdx), true);
                s.parseBuffer = s.parseBuffer.slice(openIdx + 7);
                continue;
            }
            if (closeIdx === -1) {
                const partialLen = getPartialTagLen(s, s.parseBuffer, '</think>');
                const safeLen = s.parseBuffer.length - partialLen;
                if (safeLen > 0) {
                    appendThinkContent(s, s.parseBuffer.slice(0, safeLen), true);
                    // 该思考内容是"假定"出来的（本轮还没见到任何 think 标签）——记录段索引
                    if (!s.sawThinkCloseTag)
                        s.implicitThinkSegIdxs.add(s.segments.length - 1);
                    s.parseBuffer = s.parseBuffer.slice(safeLen);
                }
                break;
            }
            if (closeIdx > 0)
                appendThinkContent(s, s.parseBuffer.slice(0, closeIdx), true);
            s.parseBuffer = s.parseBuffer.slice(closeIdx + 8);
            s.thinkingPhaseActive = false;
            // 出现真实 </think>：假定成立，此前的隐式思考段确属思考
            s.sawThinkCloseTag = true;
            s.implicitThinkSegIdxs.clear();
        }
        else {
            const openIdx = s.parseBuffer.indexOf('<think>');
            const closeIdx = s.parseBuffer.indexOf('</think>');
            // Orphan close tag (no paired <think>): the model omitted the open tag (common after
            // tool calls, in fast mode, or after a structured reasoning event pinned the phase to
            // body). Everything before the close tag is reasoning, not body text.
            if (closeIdx >= 0 && (openIdx === -1 || closeIdx < openIdx)) {
                if (closeIdx > 0)
                    appendThinkContent(s, s.parseBuffer.slice(0, closeIdx), true);
                s.parseBuffer = s.parseBuffer.slice(closeIdx + 8);
                s.sawThinkCloseTag = true;
                s.implicitThinkSegIdxs.clear();
                continue;
            }
            if (openIdx === -1) {
                const partialLen = Math.max(getPartialTagLen(s, s.parseBuffer, '<think>'), getPartialTagLen(s, s.parseBuffer, '</think>'));
                const safeLen = s.parseBuffer.length - partialLen;
                if (safeLen > 0) {
                    appendTextSeg(s, s.parseBuffer.slice(0, safeLen));
                    s.parseBuffer = s.parseBuffer.slice(safeLen);
                }
                break;
            }
            if (openIdx > 0)
                appendTextSeg(s, s.parseBuffer.slice(0, openIdx));
            s.parseBuffer = s.parseBuffer.slice(openIdx + 7);
            s.thinkingPhaseActive = true;
        }
    }
}
export function getEventToolId(s: ChatStreamState, obj: Record<string, unknown>) { return s.normalizeToolId(obj.id) || s.normalizeToolId(obj.tool_call_id) || s.normalizeToolId(obj.call_id) || s.normalizeToolId(obj.tool_id); }
export function getEventToolRawName(_s: ChatStreamState, obj: Record<string, unknown>) {
    const candidates = [obj.name, obj.tool_name, obj.tool, obj.title];
    for (const candidate of candidates) {
        if (typeof candidate === 'string' && candidate.trim())
            return stripMcpToolPrefix(candidate.trim());
    }
    return undefined;
}
export function getEventToolDisplayName(_s: ChatStreamState, obj: Record<string, unknown>) {
    if (typeof obj.tool_display_name === 'string' && obj.tool_display_name.trim()) {
        // When the backend can't find a Chinese display name it falls back to the raw tool name;
        // an mcp__ prefix means it's just an echo — discard it and let the frontend's
        // TOOL_NAME_OVERRIDES / toolDisplayNames lookup chain take over.
        if (obj.tool_display_name.trim().startsWith('mcp__'))
            return undefined;
        let displayName = obj.tool_display_name.trim();
        if (typeof obj.subagent_name === 'string' && obj.subagent_name.trim()) {
            displayName += `：${obj.subagent_name.trim()}`;
        }
        return displayName;
    }
    return undefined;
}
export function findToolCallIndex(s: ChatStreamState, obj: Record<string, unknown>) { return resolveToolCardIndex(s.toolCalls, getEventToolId(s, obj), getEventToolRawName(s, obj), {
    fallbackToAnyRunning: true,
}); }
export function finalizeRunningTools(s: ChatStreamState) {
    let changed = false;
    s.toolCalls = s.toolCalls.map((tool) => {
        if (tool.status !== 'running')
            return tool;
        changed = true;
        return { ...tool, status: 'interrupted' };
    });
    return changed;
}
export function appendArtifactsToStreamToolCalls(s: ChatStreamState, artifacts: unknown[]) {
    if (!Array.isArray(artifacts) || artifacts.length === 0)
        return false;
    const existingFileIds = new Set<string>();
    for (const tool of s.toolCalls) {
        if (!tool?.output || typeof tool.output !== 'object')
            continue;
        const fileId = (tool.output as Record<string, unknown>).file_id;
        if (typeof fileId === 'string' && fileId.trim())
            existingFileIds.add(fileId.trim());
    }
    let changed = false;
    let latestHtml: {
        file_id: string;
        name: string;
        url: string;
        mime_type?: string;
        size?: number;
        origin?: 'local' | 'cloud';
    } | null = null;
    for (const artifact of artifacts) {
        const output = normalizeArtifactOutput(artifact);
        if (!output)
            continue;
        const fileId = String(output.file_id);
        if (existingFileIds.has(fileId))
            continue;
        existingFileIds.add(fileId);
        s.toolCalls.push({ id: `artifact_${fileId}`, name: t('附件'), output, status: 'success', timestamp: Date.now() });
        s.segments.push({ type: 'tool', toolIndex: s.toolCalls.length - 1 });
        changed = true;
        // Auto-open Canvas when an HTML artifact arrives (Claude-style live preview).
        // Track the last HTML in the batch and open it after the loop.
        const name = String(output.name || '').toLowerCase();
        const mime = String(output.mime_type || '').toLowerCase();
        const isHtml = name.endsWith('.html') || name.endsWith('.htm') || mime === 'text/html';
        if (isHtml) {
            latestHtml = {
                file_id: fileId,
                origin: output.origin === 'local' || output.origin === 'cloud' ? output.origin : undefined,
                name: String(output.name || 'preview.html'),
                url: String(output.url || ''),
                mime_type: typeof output.mime_type === 'string' ? output.mime_type : undefined,
                size: typeof output.size === 'number' ? output.size : undefined,
            };
        }
    }
    if (latestHtml && latestHtml.url && canAutoOpenCanvas()) {
        const canvas = useCanvasStore.getState();
        // Don't steal focus from a different file the user is actively viewing —
        // only auto-open if Canvas is closed or already showing this same artifact.
        if (!canvas.isOpen || !canvas.artifact || canvas.artifact.file_id === latestHtml.file_id) {
            canvas.openCanvas({ ...latestHtml, chat_id: s.chatId });
        }
    }
    return changed;
}
export function toolStartedAt(s: ChatStreamState, eventObj: Record<string, unknown>): number { return typeof eventObj.started_at === 'number' ? eventObj.started_at : s.lastEventTs; }
export function autoOpenOntologySidebar(s: ChatStreamState) {
    if (s.ontologySidebarAutoOpened)
        return;
    s.ontologySidebarAutoOpened = true;
    // A background stream must not replace the panel in the chat the user is
    // currently reading. The result remains available from its message entry.
    if (useChatStore.getState().currentChatId !== s.chatId)
        return;
    if (panelFromPath() !== 'chat')
        return;
    // 移动端不自动弹（整屏覆盖），评审结论仍可从消息里的入口打开。
    if (!canAutoOpenCanvas())
        return;
    useCanvasStore.getState().openOntology({ chatId: s.chatId, messageUid: s.bubbleUid });
}
export function findOwnBubble(s: ChatStreamState, msgs: ChatMessage[]): number { return msgs.findIndex((m) => m.uid === s.bubbleUid); }
export function adoptPersistedBubble(s: ChatStreamState, msgs: ChatMessage[]): ChatMessage[] {
    if (!s.metaMessageId)
        return msgs;
    const persisted = msgs.find((m) => m.role === 'assistant' && m.messageId === s.metaMessageId);
    if (!persisted || persisted.uid === s.bubbleUid)
        return msgs;
    const withoutPlaceholder = msgs.filter((m) => m.uid !== s.bubbleUid);
    s.bubbleUid = persisted.uid;
    return withoutPlaceholder;
}
export function claimPersistedBubble(s: ChatStreamState) {
    useChatStore.getState().updateStore((prev) => {
        const c = prev.chats[s.chatId];
        if (!c)
            return prev;
        const msgs = adoptPersistedBubble(s, c.messages || []);
        if (msgs === c.messages)
            return prev;
        return { ...prev, chats: { ...prev.chats, [s.chatId]: { ...c, messages: msgs } } };
    });
}
/** The model selected a separate reasoning channel, including an empty marker. */
export function activateStructuredReasoning(s: ChatStreamState) {
  s.structuredReasoning = true;
  if (s.parseBuffer) {
    appendTextSeg(s, s.parseBuffer);
    s.parseBuffer = '';
  }
  s.thinkingPhaseActive = false;
  reclassifyImplicitThinking(s);
}

/** A committed tool or plan boundary starts the next inline reasoning phase. */
export function rearmInlineReasoning(s: ChatStreamState) {
    if (!s.enableThinking || s.structuredReasoning) return;
    if (s.parseBuffer) {
        if (s.thinkingPhaseActive) appendThinkContent(s, s.parseBuffer, true);
        else appendTextSeg(s, s.parseBuffer);
        s.parseBuffer = '';
    }
    s.thinkingPhaseActive = true;
}
