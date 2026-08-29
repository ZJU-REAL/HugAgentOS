import type { ChatMessage, MessageSegment, ThinkingBlock } from '../types';
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
 * 把独立存储的思考块按 offset 插回正文原位，还原成历史重建认识的内联形态。
 *
 * 落库时思考进 chat_messages.thinking 独立一列、正文进 content，两者互不挤占
 * 长度上限；展示时思考必须回到它当初出现的那个位置，顺序与实时流式一模一样。
 * 工具卡片的 contentOffset 与正文同坐标系，插入思考后要按插入长度整体右移。
 * offset 相同时思考排在工具卡片之前（落库顺序即如此：先 flush 思考再记卡片位置）。
 */
function restoreThinkingInPlace(
  content: string,
  toolCalls: ChatMessage['toolCalls'],
  thinking: ThinkingBlock[],
): { content: string; toolCalls: ChatMessage['toolCalls'] } {
  const blocks = thinking
    .map((block, order) => ({
      order,
      text: block.content || '',
      offset: typeof block.offset === 'number' && block.offset >= 0
        ? Math.min(block.offset, content.length)
        : content.length,
    }))
    .filter((block) => block.text)
    .sort((a, b) => a.offset - b.offset || a.order - b.order);
  if (blocks.length === 0) return { content, toolCalls };

  const inserts: { at: number; len: number }[] = [];
  let restored = '';
  let cursor = 0;
  blocks.forEach((block) => {
    restored += content.slice(cursor, block.offset);
    const wrapped = `<think>${block.text}</think>`;
    restored += wrapped;
    inserts.push({ at: block.offset, len: wrapped.length });
    cursor = block.offset;
  });
  restored += content.slice(cursor);

  const shifted = toolCalls?.map((tc) => {
    if (typeof tc.contentOffset !== 'number') return tc;
    const delta = inserts.reduce((sum, ins) => (ins.at <= tc.contentOffset! ? sum + ins.len : sum), 0);
    return delta ? { ...tc, contentOffset: tc.contentOffset + delta } : tc;
  });
  return { content: restored, toolCalls: shifted };
}

export function buildHistorySegments(
  rawContent: string,
  rawToolCalls?: ChatMessage['toolCalls'],
  thinking?: ThinkingBlock[]
): { segments: MessageSegment[] | undefined; cleanContent: string } {
  // 思考单独存一列的新消息：先按 offset 插回原位，之后完全走原有重建逻辑，
  // 展示与老的内联存储格式逐字一致。thinking 为空则是老消息，思考仍在 content 里。
  const hasStoredThinking = Array.isArray(thinking) && thinking.length > 0;
  const restored = hasStoredThinking
    ? restoreThinkingInPlace(rawContent, rawToolCalls, thinking!)
    : { content: rawContent, toolCalls: rawToolCalls };
  const content = restored.content;
  const toolCalls = restored.toolCalls;
  // ── 新历史（带 contentOffset）：按流式原顺序把文本与工具卡片交错还原 ──
  // contentOffset = 工具卡片出现时持久化正文累计串（含 <think> 标记）的字符
  // 偏移，与 content 同一坐标系。逐段切片，段内再按 </think> 拆 thinking /
  // text。刷新后的历史与实时流式展示保持一致（问题15）。
  // 附件伪工具卡（attachArtifactsToToolCalls 追加的 artifact_*）没有 offset，
  // 视为「排在正文末尾」，不因它们把整条消息打回堆叠兜底。
  const isArtifactCard = (tc: NonNullable<ChatMessage['toolCalls']>[number]) =>
    typeof tc.id === 'string' && tc.id.startsWith('artifact_');
  const hasOffsets = Array.isArray(toolCalls)
    && toolCalls.length > 0
    && toolCalls.some((tc) => typeof tc.contentOffset === 'number')
    && toolCalls.every((tc) => typeof tc.contentOffset === 'number' || isArtifactCard(tc));
  // 思考单独存一列时每块都带确切位置，没有工具卡片也照样能原位还原——不必退回
  // 「合并正文」的兜底（那条路会把思考全堆到正文前面，位置就乱了）。
  if (hasOffsets || (hasStoredThinking && (toolCalls?.length ?? 0) === 0)) {
    const segments: MessageSegment[] = [];
    let cursor = 0;
    let visibleAll = '';
    (toolCalls ?? []).forEach((tc, i) => {
      const rawOff = typeof tc.contentOffset === 'number' ? tc.contentOffset : content.length;
      const off = Math.min(Math.max(rawOff, cursor), content.length);
      const visible = segmentTextSlice(content.slice(cursor, off), segments);
      if (visible) visibleAll += (visibleAll ? '\n\n' : '') + visible;
      cursor = off;
      segments.push({ type: 'tool', toolIndex: i });
    });
    const finalVisible = segmentTextSlice(content.slice(cursor), segments);
    if (finalVisible) visibleAll += (visibleAll ? '\n\n' : '') + finalVisible;
    // 与实时视图一致的碎片归并：思考型模型偶尔把下一句的首个汉字与工具调用
    // 同轮吐出，实时侧用 deferThinkingTextFragmentBeforeTool 把它挪到工具卡
    // 之后的正文开头——历史重建做同样的归并，刷新前后展示才逐字一致。
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
    // 硬规则：工具卡/思考块不允许落在正文末尾之后——最终答案必须是消息的
    // 最后一段。
    liftTrailingSegmentsAboveFinalText(segments);
    const lastToolIdx = segments.map((s) => s.type).lastIndexOf('tool');
    const tailText = segments
      .slice(lastToolIdx + 1)
      .filter((s) => s.type === 'text')
      .map((s) => s.content || '')
      .join('\n\n');
    return {
      segments: segments.length > 0 ? segments : undefined,
      // cleanContent 维持「最终可见正文」语义：取最后一个工具卡之后的文本；没有则用全部可见文本
      cleanContent: tailText || visibleAll,
    };
  }

  // ── 兜底（无 contentOffset 的旧历史）：reasoning 块与工具卡片配对，可见
  // 正文合并成一个 Markdown 块，避免瞎猜切分点。 ──
  // Both reasoning protocols are normalized in persisted content:
  // - inline models may emit `reasoning</think>answer` without an opening tag;
  // - structured reasoning events are stored as `<think>reasoning</think>`.
  const { thinkingContents, visibleContent } = parseHistoricalContent(content);
  const toolCount = toolCalls?.length ?? 0;
  const segments: MessageSegment[] = [];
  thinkingContents.forEach((thinking, idx) => {
    segments.push({ type: 'thinking', content: thinking });
    if (idx < toolCount) segments.push({ type: 'tool', toolIndex: idx });
  });

  // Any calls left after pairing with reasoning still happened before the
  // final answer, so keep them above the answer instead of dumping them below.
  for (let i = thinkingContents.length; i < toolCount; i++) {
    segments.push({ type: 'tool', toolIndex: i });
  }
  if (visibleContent) segments.push({ type: 'text', content: visibleContent });

  return {
    segments: segments.length > 0 ? segments : undefined,
    cleanContent: visibleContent,
  };
}
