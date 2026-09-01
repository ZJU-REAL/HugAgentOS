import type { ChatMessage, MessageSegment, StoredSegment, ThinkingBlock } from '../types';
import { isSingleHanCharacter, liftTrailingSegmentsAboveFinalText } from './streamSegments';

/**
 * Parse multi-turn thinking blocks from a historical message's content field, rebuilding segments for inline rendering in history.
 *
 * Storage format of content (multi-turn tool calls):
 *   [thinking1]</think>[thinking2]</think>...[thinkingN]</think>[final body text]
 *
 * Correspondence:
 *   thinking1 → tool[0] → thinking2 → tool[1] → ... → thinkingN → final body text
 *
 * - Split by </think>; every segment except the last is a thinking block
 * - Each thinking block is paired with the next tool call (in order)
 * - The last segment is the final body text
 * - If there is no </think>, directly output tool calls + body text
 */
/** 把一段正文切成 thinking / text 段（按 </think> 划分；<think> 前的可见文本不丢弃）。 */
function segmentTextSlice(slice: string, segments: MessageSegment[]): string {
  const parts = slice.split('</think>');
  let visible = '';
  // 上一个思考块刚被「并回前块」时置位：断口两侧本是同一句正文，直接续接，
  // 不能加 \n\n 段落分隔。
  let directJoin = false;
  const pushText = (text: string) => {
    if (directJoin) {
      directJoin = false;
      const last = segments[segments.length - 1];
      if (last?.type === 'text' && text) {
        segments[segments.length - 1] = { ...last, content: `${last.content}${text}` };
        visible += text;
        return;
      }
    }
    const trimmed = text.trim();
    if (!trimmed) return;
    visible += (visible ? '\n\n' : '') + trimmed;
    const last = segments[segments.length - 1];
    if (last?.type === 'text') last.content = `${last.content}\n\n${trimmed}`;
    else segments.push({ type: 'text', content: trimmed });
  };
  const pushThinking = (thinkContent: string) => {
    if (!thinkContent.trim()) return;
    const last = segments[segments.length - 1];
    const prev = segments[segments.length - 2];
    if (last?.type === 'text' && prev?.type === 'thinking') {
      // 与实时一致（appendThinkingContentBeforeTrailingText）：正文已开始后
      // 迟到的思考尾部并回前一个思考块，不在答案中间插一个思考段。
      segments[segments.length - 2] = {
        ...prev,
        content: `${prev.content || ''}${thinkContent}`,
      };
      directJoin = true;
      return;
    }
    segments.push({ type: 'thinking', content: thinkContent });
  };
  parts.forEach((part, idx) => {
    const isLast = idx === parts.length - 1;
    if (isLast) {
      // 未闭合的 <think>：落库被截断、或模型漏发闭合标签。其后全部是思考，
      // 当正文渲染出去就是思考过程泄露到页面上。
      const unclosedIdx = part.indexOf('<think>');
      if (unclosedIdx >= 0) {
        pushText(part.slice(0, unclosedIdx));
        pushThinking(part.slice(unclosedIdx + 7));
        return;
      }
      pushText(part);
      return;
    }
    const openTagIdx = part.indexOf('<think>');
    if (openTagIdx >= 0) {
      // <think> 之前的内容是上一轮的可见正文——不丢弃（问题15）
      pushText(part.slice(0, openTagIdx));
      pushThinking(part.slice(openTagIdx + 7));
    } else {
      pushThinking(part);
    }
  });
  return visible;
}

/**
 * Separate persisted reasoning from visible text without breaking the visible
 * response into one Markdown block per tool phase.
 */
function parseHistoricalContent(content: string): {
  thinkingContents: string[];
  visibleContent: string;
} {
  const parts = content.split('</think>');
  const thinkingContents: string[] = [];
  let visibleContent = '';
  parts.forEach((part, idx) => {
    if (idx === parts.length - 1) {
      // 未闭合的 <think>（截断 / 模型漏发闭合标签）后面全是思考，不是正文。
      const unclosedIdx = part.indexOf('<think>');
      if (unclosedIdx >= 0) {
        visibleContent += part.slice(0, unclosedIdx);
        const tail = part.slice(unclosedIdx + 7).trim();
        if (tail) thinkingContents.push(tail);
        return;
      }
      visibleContent += part;
      return;
    }

    const openTagIdx = part.indexOf('<think>');
    if (openTagIdx >= 0) {
      // Text emitted between two reasoning rounds remains part of the one
      // visible answer. Preserve its original whitespace when joining it.
      visibleContent += part.slice(0, openTagIdx);
      const thinking = part.slice(openTagIdx + 7).trim();
      if (thinking) thinkingContents.push(thinking);
    } else {
      const thinking = part.trim();
      if (thinking) thinkingContents.push(thinking);
    }
  });

  return { thinkingContents, visibleContent: visibleContent.trim() };
}

/**
 * 按落库的段落表还原一条历史消息的展示形态。
 *
 * 正文、思考、工具卡片的先后，在实时流式的那一刻就已经确定，落库时原样记进
 * `metadata.segments`，这里照着渲染即可——不做任何位置反推。段落表里正文片段内联，
 * 思考与工具卡片按下标引用各自的列。
 */
function renderStoredSegments(
  stored: StoredSegment[],
  toolCalls: ChatMessage['toolCalls'],
  thinking: ThinkingBlock[] | undefined,
): { segments: MessageSegment[] | undefined; cleanContent: string } {
  const segments: MessageSegment[] = [];
  const placedTools = new Set<number>();
  let visibleAll = '';

  stored.forEach((entry) => {
    if (entry.type === 'text') {
      // 内联 reasoning 的模型会把 <think> 写进正文流，这里照旧剥掉，
      // 否则推理过程会当作正文渲染出来。
      const visible = segmentTextSlice(entry.text || '', segments);
      if (visible) visibleAll += (visibleAll ? '\n\n' : '') + visible;
      return;
    }
    if (entry.type === 'thinking') {
      const block = thinking?.[entry.index];
      if (block?.content) segments.push({ type: 'thinking', content: block.content });
      return;
    }
    if (toolCalls && entry.index >= 0 && entry.index < toolCalls.length) {
      placedTools.add(entry.index);
      segments.push({ type: 'tool', toolIndex: entry.index });
    }
  });

  // 附件伪工具卡（attachArtifactsToToolCalls 在加载后追加的 artifact_*）不在段落表
  // 里，它们描述的是本条消息的产物，排在最后。
  (toolCalls ?? []).forEach((_, index) => {
    if (!placedTools.has(index)) segments.push({ type: 'tool', toolIndex: index });
  });

  // 与实时视图一致的碎片归并：思考型模型偶尔把下一句的首个汉字与工具调用同轮吐出，
  // 实时侧用 deferThinkingTextFragmentBeforeTool 把它挪到工具卡之后的正文开头——
  // 历史照做同样的归并，刷新前后才逐字一致。
  for (let i = segments.length - 2; i >= 0; i--) {
    const seg = segments[i];
    if (seg.type !== 'text' || !isSingleHanCharacter(seg.content || '')) continue;
    if (segments[i + 1]?.type !== 'tool') continue;
    const followsProcess = segments
      .slice(0, i)
      .some((s) => s.type === 'tool' || s.type === 'thinking');
    if (!followsProcess) continue;
    const next = segments.slice(i + 1).find((s) => s.type === 'text');
    if (!next) continue;
    next.content = (seg.content || '') + (next.content || '');
    segments.splice(i, 1);
  }
  // 硬规则：工具卡/思考块不允许落在正文末尾之后——最终答案必须是消息的最后一段。
  liftTrailingSegmentsAboveFinalText(segments);

  const lastToolIdx = segments.map((s) => s.type).lastIndexOf('tool');
  const tailText = segments
    .slice(lastToolIdx + 1)
    .filter((s) => s.type === 'text')
    .map((s) => s.content || '')
    .join('\n\n');
  return {
    segments: segments.length > 0 ? segments : undefined,
    cleanContent: tailText || visibleAll,
  };
}

/**
 * 还原一条历史消息：有落库的段落表就照它渲染，没有就只给正文。
 *
 * 没有段落表的老消息**不做顺序反推**——展示顺序在当时没有被记下来，猜出来的顺序
 * 只会是错的。这类消息退化成「正文 + 工具卡片」的堆叠展示，正文里内联的 `<think>`
 * 仍会剥掉，避免推理过程漏成正文。
 */
export function buildHistorySegments(
  rawContent: string,
  rawToolCalls?: ChatMessage['toolCalls'],
  thinking?: ThinkingBlock[],
  storedSegments?: StoredSegment[] | null,
): { segments: MessageSegment[] | undefined; cleanContent: string } {
  if (Array.isArray(storedSegments) && storedSegments.length > 0) {
    return renderStoredSegments(storedSegments, rawToolCalls, thinking);
  }
  const { visibleContent } = parseHistoricalContent(rawContent);
  return { segments: undefined, cleanContent: visibleContent };
}
