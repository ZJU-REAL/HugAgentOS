import type { ChatMessage } from '../types';
import type { ShellStep } from '../components/tool/ToolRunShell';

/** Shared grouping for the main transcript and Canvas subagent transcript. */
export function groupExecutionRuns(m: ChatMessage, dispatchProcessVisible: boolean) {
  const segs = m.segments || [];
  const isEmptyText = (i: number): boolean => {
    const segment = segs[i];
    return !!segment && segment.type === 'text' && !(segment.content || '').trim();
  };
  type Run = {
    anchor: number;
    endIdx: number;
    steps: ShellStep[];
    tools: NonNullable<typeof m.toolCalls>;
  };
  const runs: Run[] = [];
  const suppressedIdx = new Set<number>();

  const canStartRun = (idx: number): boolean => {
    const s = segs[idx];
    if (!s) return false;
    if (s.type === 'tool') return true;
    // Only ON mode lets a thinking segment anchor a run; OFF mode
    // keeps standalone ThinkingInline as before.
    if (dispatchProcessVisible && s.type === 'thinking') return true;
    return false;
  };

  let i = 0;
  while (i < segs.length) {
    if (!canStartRun(i)) {
      i++;
      continue;
    }
    const anchor = i;
    const steps: ShellStep[] = [];
    const tools: NonNullable<typeof m.toolCalls> = [] as NonNullable<typeof m.toolCalls>;
    let endIdx = -1;
    while (i < segs.length) {
      const sk = segs[i];
      if (sk.type === 'tool') {
        const t = m.toolCalls?.[sk.toolIndex!];
        if (t) {
          steps.push({ kind: 'tool', tool: t, key: `${m.uid}-seg-${i}` });
          tools.push(t);
        }
        if (i !== anchor) suppressedIdx.add(i);
        endIdx = i;
        i++;
      } else if (sk.type === 'thinking') {
        if (dispatchProcessVisible) {
          const content = sk.content || '';
          const active = !!(m.isStreaming && !segs.slice(i + 1).some(seg => seg.type === 'text'));
          steps.push({ kind: 'thinking', content, active, key: `${m.uid}-seg-${i}` });
          endIdx = i;
        }
        if (i !== anchor) suppressedIdx.add(i);
        i++;
      } else if (isEmptyText(i)) {
        if (i !== anchor) suppressedIdx.add(i);
        i++;
      } else {
        break;
      }
    }
    // OFF mode un-suppresses trailing empty-text so the inline
    // StreamWaitIndicator can host there. ON mode keeps them
    // suppressed — the shell owns the wait state and an empty
    // bubble below it would just add visual noise.
    if (endIdx >= 0 && !dispatchProcessVisible) {
      for (let j = endIdx + 1; j < i; j++) {
        if (isEmptyText(j)) suppressedIdx.delete(j);
      }
    }
    if (steps.length > 0) {
      runs.push({ anchor, endIdx, steps, tools });
    } else {
      // Run with no real steps (only empty text) — un-suppress
      // everything so we don't accidentally swallow useful state.
      for (let j = anchor; j < i; j++) suppressedIdx.delete(j);
    }
  }

  // ON mode owns the wait state inside the shell, so any stray
  // empty-text segments outside a run would just paint an empty
  // bubble. Suppress them.
  if (dispatchProcessVisible) {
    for (let j = 0; j < segs.length; j++) {
      if (isEmptyText(j)) suppressedIdx.add(j);
    }
  }
  return { runs, suppressedIdx };
}
