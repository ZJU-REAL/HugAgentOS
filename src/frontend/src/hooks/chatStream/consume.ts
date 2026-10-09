import { processSseBlock } from './framing';
import {
listChatJobs
} from '../../api';
import { useChatStore } from '../../stores';
import type { ChatItem } from '../../types';
import { newMessageUid } from '../../utils/messageIdentity';
import {
liftTrailingSegmentsAboveFinalText,
restoreDeferredThinkingTextFragment
} from '../../utils/streamSegments';
import { normalizeToolId as normalizeToolIdValue } from '../../utils/toolMatching';
import { _streamActivity,_seenRuns,isRunCancelledByUser,refreshContextAfterCompaction,type ChatStreamOptions,type ChatStreamOutcome } from './runtime';
import type { ChatStreamState } from './state';
import { readRunTransport, StreamDisconnectedError } from './transport';

import { adoptPersistedBubble,appendTextSeg,appendThinkContent,finalizeRunningTools,findOwnBubble,reclassifyImplicitThinking } from './messageReducer';
import { appendOrUpdate,cancelStreamingUpdate,flushStreamingUpdate } from './messageStore';
export async function processChatStream(resp: Response, opts: ChatStreamOptions): Promise<ChatStreamOutcome> {
const s = {} as ChatStreamState;
s.chatId = opts.chatId;
s.enableThinking = opts.enableThinking;
s.pendingNotice = opts.pendingNotice;
s.onEvent = opts.onEvent;
if (!resp.body)
    throw new Error('empty response body');
s.runId = useChatStore.getState().activeRuns[s.chatId]?.runId || '';
s.eventOffset = useChatStore.getState().activeRuns[s.chatId]?.lastOffset || 0;
s.decoder = new TextDecoder('utf-8');
s.sseBuffer = '';
s.full = '';
s.streamEnded = false;
s.toolCalls = [];
s.thinking = [];
s.segments = [];
s.ontologyGovernance = undefined;
s.evolutionSummary = undefined;
s.metaMessageId = undefined;
s.metaFollowUps = [];
s.queuedRun = undefined;
s.allCitations = [];
s.metaWorkspaceFiles = null;
s.metaDurationMs = null;
s.compactionPending = false;
s.parseBuffer = '';
s.deferredThinkingText = undefined;
s.toolPending = false;
s.ontologySidebarAutoOpened = false;
s.aborted = false;
s.thinkingPhaseActive = s.enableThinking;
s.structuredReasoning = false;
s.implicitThinkSegIdxs = new Set<number>();
s.sawThinkCloseTag = false;
s.normalizeToolId = normalizeToolIdValue;
s.bubbleUid = newMessageUid();
s.bubbleStartedAt = Date.now();
s.lastEventTs = Date.now();
s.seed = opts.seedFrom;
if (s.seed) {
    s.bubbleUid = s.seed.uid;
    s.bubbleStartedAt = s.seed.ts;
    s.metaMessageId = s.seed.messageId;
    s.full = s.seed.content || '';
    s.toolCalls = (s.seed.toolCalls || []).map((tool) => ({ ...tool }));
    s.thinking.push(...(s.seed.thinking || []).map((block) => ({
        content: block.content,
        timestamp: block.timestamp ?? s.bubbleStartedAt,
    })));
    s.segments.push(...(s.seed.segments || []));
    // 基态里已有正文：思考阶段早就过了，剥离器等下一个 <think> 再重新武装。
    if (s.full)
        s.thinkingPhaseActive = false;
}
s.pendingStreamingUpdate = null;
s.streamingUpdateTimer = null;
s.lastStreamingCommitAt = 0;
s.hookApi = {
    appendText: (txt: string) => appendTextSeg(s, txt),
    hasText: () => s.full.length > 0,
    refresh: () => {
        appendOrUpdate(s, true, s.allCitations);
        flushStreamingUpdate(s);
    },
};
appendOrUpdate(s, true);
s.thrown = null;
_streamActivity.set(s.chatId, Date.now());
try {
    await readRunTransport(resp, s, opts.signal, (value) => {
        _streamActivity.set(s.chatId, Date.now());
        s.sseBuffer += s.decoder.decode(value, { stream: true });
        const blocks = s.sseBuffer.split(/\r?\n\r?\n/);
        s.sseBuffer = blocks.pop() || '';
        for (const block of blocks) {
            processSseBlock(s, block);
            if (s.streamEnded) break;
        }
    }, !opts.onEvent);
    const tail = s.sseBuffer.trim();
    if (tail && !s.streamEnded)
        processSseBlock(s, tail);
}
catch (e) {
    if ((e as {
        name?: string;
    })?.name === 'AbortError')
        s.aborted = true;
    else
        s.thrown = e;
}
// 流已经收尾（正常结束 / 中止 / 异常）→ 摘掉活性标记，别让看门狗对着一条已死的流继续对账
_streamActivity.delete(s.chatId);
// Losing a connection is not a task outcome. Keep the reducer's live state.
if (!s.streamEnded && s.runId && !(s.aborted && isRunCancelledByUser(s.runId))) {
    cancelStreamingUpdate(s);
    appendOrUpdate(s, true);
    flushStreamingUpdate(s);
    _seenRuns.delete(s.runId);
    if (s.thrown) throw s.thrown;
    if (!s.aborted) throw new StreamDisconnectedError();
    return { settled: false, full: s.full, bubbleUid: s.bubbleUid, metaMessageId: s.metaMessageId,
             metaFollowUps: s.metaFollowUps, aborted: true, queuedRun: s.queuedRun };
}
// ── Unified wind-down: whether normal end/abort/exception, the bubble must leave the streaming state ──
// 识图状态必须在这里兜底清掉：中止或异常时那个 status=done 事件永远不会到，
// 留着的话下一轮会一直显示「图像理解中」。
useChatStore.getState().setVisionReading(s.chatId, 0);
finalizeRunningTools(s);
if (s.parseBuffer) {
    if (s.thinkingPhaseActive && s.sawThinkCloseTag) {
        appendThinkContent(s, s.parseBuffer, true);
    }
    else {
        // 整条流从未出现 </think>：残余缓冲是正文，不是思考（结构化 reasoning
        // 模型无思考输出的场景；旧行为会把它并进思考块）
        appendTextSeg(s, s.parseBuffer);
    }
    s.parseBuffer = '';
}
// 兜底：老后端/回放流没有首轮协议标记时，流结束仍无任何 think 标签 → 重归类
reclassifyImplicitThinking(s);
s.deferredThinkingText = restoreDeferredThinkingTextFragment(s.segments, s.deferredThinkingText);
// 收尾统一规则：工具卡/思考块不留在最终答案之后（与历史重建一致，刷新前后不跳变）
liftTrailingSegmentsAboveFinalText(s.segments);
// 收尾直接写 store（不走 appendOrUpdate），所以必须先撤掉还挂着的合并写定时器 ——
// 否则它会在收尾之后补一帧 isStreaming:true，把气泡永久钉在"生成中"。
cancelStreamingUpdate(s);
s.isMd = /\n|```|\*\*|^\s*#\s/m.test(s.full);
useChatStore.getState().updateStore((prev) => {
    const c = prev.chats[s.chatId];
    const msgs = adoptPersistedBubble(s, [...(c?.messages || [])]);
    const idx = findOwnBubble(s, msgs);
    if (idx >= 0) {
        msgs[idx] = {
            ...msgs[idx],
            content: s.full,
            isMarkdown: s.isMd,
            toolCalls: s.toolCalls.length > 0 ? s.toolCalls : undefined,
            thinking: s.thinking.length > 0 ? s.thinking : undefined,
            ontologyGovernance: s.ontologyGovernance,
            segments: s.segments.length > 0 ? s.segments : undefined,
            citations: s.allCitations.length > 0 ? s.allCitations : undefined,
            followUpQuestions: s.metaFollowUps.length > 0 ? s.metaFollowUps : undefined,
            messageId: s.metaMessageId,
            workspaceFiles: s.metaWorkspaceFiles,
            isStreaming: false,
            inFlight: undefined,
            durationMs: s.metaDurationMs ?? (Date.now() - s.bubbleStartedAt),
        };
    }
    const nextChat: ChatItem = { ...(c as ChatItem), messages: msgs, updatedAt: Date.now() };
    return { chats: { ...prev.chats, [s.chatId]: nextChat }, order: [s.chatId, ...(prev.order || []).filter((x) => x !== s.chatId)] };
});
if (s.compactionPending && !s.aborted && !s.thrown) {
    const previousCheckpointId = useChatStore.getState().contextCompactions[s.chatId]?.checkpointId || '';
    void refreshContextAfterCompaction(s.chatId, previousCheckpointId, s.bubbleStartedAt, s.metaMessageId);
}
// Settle the plan bar: if this turn produced an agent plan, mark it done so
// the bar renders as settled (it clears on the next send).
//
// 例外 —— 工作流模式把活丢给了后台作业：那一轮在 `run_job(wait=false)` 之后立刻收尾，
// 计划清单却只走到「提交作业」那一步。照旧标 done 的话，计划栏会在 1/6 步上挂一个绿色
// 对勾（"已完成"），而作业其实才刚开始跑；此后的进度播报轮不动计划，于是它就永远停在
// 第 0/1 步——这正是用户看到的"计划模式最后不会更新"。作业还活着就先别收尾，让计划栏
// 保持真实的进行中状态，由作业跑完那一轮的交付把清单收尾（唤醒提示词已要求收尾）。
{
    const _pp = useChatStore.getState().planProgress[s.chatId];
    if (_pp && _pp.source === 'agent' && !_pp.done) {
        const _allSettled = _pp.steps.every((s) => s.status === 'completed' || s.status === 'failed');
        // 只有工作流会话才可能有后台作业（run_job 只在工作流模式下注册），别给普通对话
        // 的每一轮末尾平白加一次请求。
        const _workflowChat = useChatStore.getState().store.chats[s.chatId]?.workflowChat === true;
        let _jobLive = false;
        if (!_allSettled && _workflowChat) {
            try {
                const jobs = await listChatJobs(s.chatId);
                _jobLive = jobs.some((j) => j.status === 'running' || j.status === 'pending');
            }
            catch {
                _jobLive = false; // 查不到就按原来的方式收尾，别让计划栏无限期挂着
            }
        }
        if (!_jobLive) {
            useChatStore.getState().setPlanProgress(s.chatId, { ..._pp, done: true, updatedAt: Date.now() });
        }
    }
}
if (s.thrown)
    throw s.thrown;
return { settled: true, full: s.full, bubbleUid: s.bubbleUid, metaMessageId: s.metaMessageId, metaFollowUps: s.metaFollowUps, aborted: s.aborted, queuedRun: s.queuedRun };
}