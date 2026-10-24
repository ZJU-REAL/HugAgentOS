import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import { AgentMark, type AgentMarkState } from '../common/AgentMark';

interface AgentCursorProps {
  /** `data-agent-slot` of the row the agent is working on right now. */
  slotKey: string;
  state: AgentMarkState;
  /** Play the brand intro on first appearance — only when nothing else has shown it yet. */
  intro?: boolean;
}

/**
 * The one brand mark a streaming reply carries.
 *
 * Rows that can host the agent (the startup row, a tool-less run of thinking or
 * compaction, the tail row under the text) leave an empty `[data-agent-slot]`;
 * the mark glides to whichever slot owns the current step, so a reply reads as
 * one agent moving from task to task rather than a spinner per row. A run with
 * tools has no slot — its lit tool icons are the signal — so while one owns the
 * reply the mark is not rendered, and it lands back in place (no intro) on the
 * next slot. Rendered as an out-of-flow sibling of those rows, so it never takes
 * part in the message's layout.
 */
export function AgentCursor({ slotKey, state, intro = false }: AgentCursorProps) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const markRef = useRef<HTMLSpanElement>(null);
  const placedRef = useRef(false);

  const place = useCallback(() => {
    const anchor = anchorRef.current;
    const mark = markRef.current;
    const scope = anchor?.parentElement;
    if (!anchor || !mark || !scope) return;
    const slot = scope.querySelector<HTMLElement>(`[data-agent-slot="${CSS.escape(slotKey)}"]`);
    if (!slot) {
      mark.classList.add('jx-agentCursor-mark--hidden');
      placedRef.current = false;
      return;
    }
    const from = anchor.getBoundingClientRect();
    const to = slot.getBoundingClientRect();
    // The first placement lands in place; every move after that glides.
    mark.classList.toggle('jx-agentCursor-mark--glide', placedRef.current);
    mark.style.transform = `translate(${to.left - from.left}px, ${to.top - from.top}px)`;
    mark.classList.remove('jx-agentCursor-mark--hidden');
    placedRef.current = true;
  }, [slotKey]);

  // Measure when the target changes; any later movement of the slot comes with
  // the reply changing size, which the observer below catches.
  useLayoutEffect(() => {
    place();
  }, [place]);

  // Text streaming in, images loading, the window resizing, a sibling row
  // expanding — all of them resize the reply.
  useEffect(() => {
    const scope = anchorRef.current?.parentElement;
    if (!scope || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => place());
    observer.observe(scope);
    return () => observer.disconnect();
  }, [place]);

  return (
    <span ref={anchorRef} className="jx-agentCursor" aria-hidden="true">
      <span ref={markRef} className="jx-agentCursor-mark jx-agentCursor-mark--hidden">
        <AgentMark state={state} intro={intro} />
      </span>
    </span>
  );
}
