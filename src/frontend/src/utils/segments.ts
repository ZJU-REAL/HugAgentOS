import type { ChatMessage, MessageSegment } from '../types';

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
  const pushText = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    visible += (visible ? '\n\n' : '') + trimmed;
    const last = segments[segments.length - 1];
    if (last?.type === 'text') last.content = `${last.content}\n\n${trimmed}`;
    else segments.push({ type: 'text', content: trimmed });
  };
  parts.forEach((part, idx) => {
    const isLast = idx === parts.length - 1;
    if (isLast) { pushText(part); return; }
    const openTagIdx = part.indexOf('<think>');
    if (openTagIdx >= 0) {
      // <think> 之前的内容是上一轮的可见正文——过去这里被直接丢弃（问题15）
      pushText(part.slice(0, openTagIdx));
      const thinkContent = part.slice(openTagIdx + 7);
      if (thinkContent.trim()) segments.push({ type: 'thinking', content: thinkContent });
    } else if (part.trim()) {
      segments.push({ type: 'thinking', content: part });
    }
  });
  return visible;
}

export function buildHistorySegments(
  content: string,
  toolCalls?: ChatMessage['toolCalls']
): { segments: MessageSegment[] | undefined; cleanContent: string } {
  // ── 新历史（带 contentOffset）：按流式原顺序把文本与工具卡片交错还原 ──
  // contentOffset = 工具卡片出现时正文累计串的字符偏移。逐段切片，段内再按
  // </think> 拆 thinking / text。刷新后的历史与实时流式展示保持一致（问题15）。
  const hasOffsets = Array.isArray(toolCalls)
    && toolCalls.length > 0
    && toolCalls.every((tc) => typeof tc.contentOffset === 'number');
  if (hasOffsets) {
    const segments: MessageSegment[] = [];
    let cursor = 0;
    let visibleAll = '';
    toolCalls!.forEach((tc, i) => {
      const off = Math.min(Math.max(tc.contentOffset as number, cursor), content.length);
      const visible = segmentTextSlice(content.slice(cursor, off), segments);
      if (visible) visibleAll += (visibleAll ? '\n\n' : '') + visible;
      cursor = off;
      segments.push({ type: 'tool', toolIndex: i });
    });
    const finalVisible = segmentTextSlice(content.slice(cursor), segments);
    if (finalVisible) visibleAll += (visibleAll ? '\n\n' : '') + finalVisible;
    return {
      segments: segments.length > 0 ? segments : undefined,
      // cleanContent 维持"最终可见正文"语义：取最后一个文本段；没有则用全部可见文本
      cleanContent: finalVisible || visibleAll,
    };
  }

  const parts = content.split('</think>');
  const toolCount = toolCalls?.length ?? 0;

  // No </think> at all: no thinking blocks, directly tools + body text
  if (parts.length === 1) {
    const segments: MessageSegment[] = [];
    if (toolCount > 0) toolCalls!.forEach((_, i) => segments.push({ type: 'tool', toolIndex: i }));
    const text = content.trim();
    if (text) segments.push({ type: 'text', content: text });
    return { segments: segments.length > 0 ? segments : undefined, cleanContent: text };
  }

  const segments: MessageSegment[] = [];
  const thinkingParts = parts.slice(0, -1);
  const finalText = parts[parts.length - 1].trim();

  thinkingParts.forEach((part, idx) => {
    const openTagIdx = part.indexOf('<think>');
    const thinkContent = openTagIdx >= 0 ? part.slice(openTagIdx + 7) : part;
    if (thinkContent.trim()) segments.push({ type: 'thinking', content: thinkContent });
    if (idx < toolCount) segments.push({ type: 'tool', toolIndex: idx });
  });

  if (finalText) segments.push({ type: 'text', content: finalText });
  for (let i = thinkingParts.length; i < toolCount; i++) {
    segments.push({ type: 'tool', toolIndex: i });
  }

  return {
    segments: segments.length > 0 ? segments : undefined,
    cleanContent: finalText,
  };
}
