import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { Ref } from 'react';
import { DUR } from '../../utils/motionTokens';

/** Matches `jxTurnStatusShine` in styles/chat/turn-status.css. */
const SHIMMER_MS = 2800;
/** The outgoing label is dropped once its exit (--motion-duration-fast) has played. */
const EXIT_MS = DUR.fast * 1000;

interface RollingTextProps {
  text: string;
  /** Scan the label while the step it names is running. */
  shimmer?: boolean;
  /** Roll between labels; history renders pass false so nothing replays. */
  animate?: boolean;
  /** Let the width follow the label so whatever sits after it slides instead of jumping. */
  smoothWidth?: boolean;
  /** Label this one takes over from (e.g. the startup line it replaces): rolls in from it on mount. */
  rollFrom?: string;
  className?: string;
}

/**
 * A one-line label that rolls up when its text changes: the old label leaves
 * upwards while the new one rises in, so switching from one tool to the next
 * reads as a hand-over rather than a cut.
 */
export function RollingText({ text, shimmer = false, animate = true, smoothWidth = false, rollFrom, className }: RollingTextProps) {
  const [shown, setShown] = useState(() => (animate && rollFrom ? rollFrom : text));
  const [generation, setGeneration] = useState(0);
  const [leaving, setLeaving] = useState<{ text: string; generation: number } | null>(null);

  if (text !== shown) {
    setLeaving(animate ? { text: shown, generation } : null);
    setShown(text);
    setGeneration(generation + 1);
  }

  useEffect(() => {
    if (!leaving) return;
    const id = window.setTimeout(() => setLeaving(null), EXIT_MS);
    return () => window.clearTimeout(id);
  }, [leaving]);

  const wrapRef = useRef<HTMLSpanElement>(null);
  const currentRef = useRef<HTMLSpanElement>(null);
  const lastWidth = useRef<number | null>(null);
  useLayoutEffect(() => {
    const wrap = wrapRef.current;
    const current = currentRef.current;
    if (!wrap || !current) return;
    const to = current.getBoundingClientRect().width;
    const from = lastWidth.current;
    lastWidth.current = to;
    if (!smoothWidth || !animate || from === null || from === to) {
      wrap.style.width = '';
      return;
    }
    wrap.classList.remove('jx-rollText--sizing');
    wrap.style.width = `${from}px`;
    void wrap.offsetWidth;
    wrap.classList.add('jx-rollText--sizing');
    wrap.style.width = `${to}px`;
  }, [shown, smoothWidth, animate]);

  return (
    <span ref={wrapRef} className={`jx-rollText${className ? ` ${className}` : ''}`}>
      {leaving && (
        <span key={`out-${leaving.generation}`} className="jx-rollText-item jx-rollText-item--out" aria-hidden="true">
          {leaving.text}
        </span>
      )}
      <RollItem
        key={generation}
        ref={currentRef}
        text={shown}
        shimmer={shimmer}
        entering={animate && generation > 0}
      />
    </span>
  );
}

function RollItem({ text, shimmer, entering, ref }: {
  text: string;
  shimmer: boolean;
  entering: boolean;
  ref: Ref<HTMLSpanElement>;
}) {
  // Phase-lock every scan to one wall clock, so a label that mounts mid-scan
  // continues the sweep instead of restarting it on every hand-over.
  const [delay] = useState(() => `-${Math.round(performance.now() % SHIMMER_MS)}ms`);
  // The roll moves the outer span and the scan paints the inner one: one
  // element per animation, so neither overrides the other's `animation`.
  return (
    <span ref={ref} className={`jx-rollText-item${entering ? ' jx-rollText-item--in' : ''}`}>
      <span
        className={`jx-rollText-text${shimmer ? ' is-shimmer' : ''}`}
        style={shimmer ? { animationDelay: delay } : undefined}
      >
        {text}
      </span>
    </span>
  );
}
