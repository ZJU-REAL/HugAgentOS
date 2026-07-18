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
export function buildHistorySegments(
  content: string,
  toolCalls?: ChatMessage['toolCalls']
): { segments: MessageSegment[] | undefined; cleanContent: string } {
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
