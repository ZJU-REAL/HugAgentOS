import { useState } from 'react';
import type { CSSProperties, Ref } from 'react';

/** Matches `jxShimmerBand` / `jxShimmerGlint` in styles/chat/turn-status.css. */
const SHIMMER_MS = 2800;

interface ShimmerTextProps {
  text: string;
  /** Scan the label while the step it names is running. */
  active: boolean;
  className: string;
  /** Modifier added while scanning (positions the band, see turn-status.css). */
  activeClassName: string;
  ref?: Ref<HTMLSpanElement>;
}

/**
 * A label with a light band sweeping across it. The band is a masked copy of
 * the text that slides one way while the copy inside slides back, so the glyphs
 * stay put and only `transform` animates — it runs on the compositor and stays
 * smooth while the stream keeps the main thread busy. (Animating a
 * `background-clip: text` gradient repaints every frame and stutters.)
 */
export function ShimmerText({ text, active, className, activeClassName, ref }: ShimmerTextProps) {
  // Phase-lock every scan to one wall clock, so a label that mounts mid-scan
  // continues the sweep instead of restarting it on every hand-over.
  const [delay] = useState(() => `-${Math.round(performance.now() % SHIMMER_MS)}ms`);
  if (!active) return <span ref={ref} className={className}>{text}</span>;
  return (
    <span
      ref={ref}
      className={`${className} ${activeClassName}`}
      style={{ '--jx-shimmer-delay': delay } as CSSProperties}
    >
      {text}
      <span className="jx-shimmer-band" aria-hidden="true">
        <span className="jx-shimmer-glint">{text}</span>
      </span>
    </span>
  );
}
