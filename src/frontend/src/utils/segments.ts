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
/**
 * Separate persisted reasoning from visible text without breaking the visible
 * response into one Markdown block per tool phase.
 */
function parseHistoricalContent(content: string): {
  thinkingContents: string[];
  visibleContent: string;
} {
  const parts = content.split('</think>');
  if (parts.length === 1) {
    return { thinkingContents: [], visibleContent: content.trim() };
  }

  const thinkingContents: string[] = [];
  let visibleContent = '';
  parts.forEach((part, idx) => {
    if (idx === parts.length - 1) {
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

export function buildHistorySegments(
  content: string,
  toolCalls?: ChatMessage['toolCalls']
): { segments: MessageSegment[] | undefined; cleanContent: string } {
  // Both reasoning protocols are normalized in persisted content:
  // - inline models may emit `reasoning</think>answer` without an opening tag;
  // - structured reasoning events are stored as `<think>reasoning</think>`.
  // Pair the normalized reasoning blocks with tool steps, then render all
  // visible narration as one Markdown block. Persisted character offsets are
  // intentionally ignored: they use a different coordinate system from the
  // live segment renderer and would split the answer into fragments.
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
