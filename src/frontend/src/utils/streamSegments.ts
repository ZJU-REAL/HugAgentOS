import type { MessageSegment } from '../types';

export interface DeferredThinkingTextFragment {
  content: string;
  originalIndex: number;
}

const SINGLE_HAN_CHARACTER = /^\p{Script=Han}$/u;

/** 是否是「单个汉字」碎片——流式 defer 与历史重建共用同一判定。 */
export const isSingleHanCharacter = (text: string): boolean => SINGLE_HAN_CHARACTER.test(text);

/**
 * Thinking-capable models occasionally emit the first character of their next
 * sentence in the same model round as a tool call. When that happens between
 * two execution phases, committing the character immediately produces:
 *
 *   tool run → "数" → tool run → "仓确认……"
 *
 * Hold only the narrow, unambiguous shape observed in production: one Han
 * character after an earlier process segment. Longer narration remains exactly
 * where the model emitted it.
 */
export function deferThinkingTextFragmentBeforeTool(
  segments: MessageSegment[],
  enabled: boolean,
  current: DeferredThinkingTextFragment | undefined,
): DeferredThinkingTextFragment | undefined {
  if (!enabled || current || segments.length === 0) return current;

  const lastIndex = segments.length - 1;
  const last = segments[lastIndex];
  const content = last?.type === 'text' ? last.content || '' : '';
  if (!SINGLE_HAN_CHARACTER.test(content)) return current;

  const followsProcess = segments
    .slice(0, lastIndex)
    .some((segment) => segment.type === 'tool' || segment.type === 'thinking');
  if (!followsProcess) return current;

  segments.pop();
  return { content, originalIndex: lastIndex };
}

/** Append visible answer text, merging a deferred thinking-mode fragment once. */
export function appendStreamTextSegment(
  segments: MessageSegment[],
  text: string,
  deferred: DeferredThinkingTextFragment | undefined,
): DeferredThinkingTextFragment | undefined {
  if (!text) return deferred;

  const content = deferred ? deferred.content + text : text;
  const lastIndex = segments.length - 1;
  const last = segments[lastIndex];
  if (last?.type === 'text') {
    // Replace the object instead of mutating a segment already exposed through
    // Zustand. React 19 may still be rendering that earlier snapshot.
    segments[lastIndex] = { ...last, content: (last.content || '') + content };
  } else {
    segments.push({ type: 'text', content });
  }
  return undefined;
}

/**
 * Structured-reasoning providers can deliver the tail of a reasoning sentence
 * after the first answer token. Keep that late delta in the preceding thinking
 * block without inserting a new chronological boundary through the answer.
 */
export function appendThinkingContentBeforeTrailingText(
  segments: MessageSegment[],
  content: string,
): boolean {
  if (!content || segments[segments.length - 1]?.type !== 'text') return false;

  const thinkingIndex = segments.length - 2;
  const thinking = segments[thinkingIndex];
  if (thinking?.type !== 'thinking') return false;

  segments[thinkingIndex] = {
    ...thinking,
    content: (thinking.content || '') + content,
  };
  return true;
}

/**
 * 硬规则：工具卡/思考块不允许落在最终答案之后。把最后一个文本段之后的段
 * 整体挪到它前面（保持相对顺序）。流收尾与历史重建共用，保证刷新前后一致。
 */
export function liftTrailingSegmentsAboveFinalText(segments: MessageSegment[]): void {
  const lastTextIdx = segments.map((s) => s.type).lastIndexOf('text');
  if (lastTextIdx !== -1 && lastTextIdx < segments.length - 1) {
    const trailing = segments.splice(lastTextIdx + 1);
    segments.splice(lastTextIdx, 0, ...trailing);
  }
}

/** Restore a held fragment at its original position if no later body arrived. */
export function restoreDeferredThinkingTextFragment(
  segments: MessageSegment[],
  deferred: DeferredThinkingTextFragment | undefined,
): undefined {
  if (deferred) {
    const index = Math.min(Math.max(deferred.originalIndex, 0), segments.length);
    segments.splice(index, 0, { type: 'text', content: deferred.content });
  }
  return undefined;
}
