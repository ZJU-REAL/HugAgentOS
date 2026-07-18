import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { BrandLoader, ElapsedTimer } from '../common';
import { EASE } from '../../utils/motionTokens';
import { t } from '../../i18n';

interface StreamWaitIndicatorProps {
  /**
   * A signature that changes whenever ANY new content (text token, tool call,
   * thinking delta, segment) arrives for this message. Used purely as a
   * change-detector to reset the "no new content" stall clock.
   */
  signature: string | number;
  /** Backend `tool_pending` signal — if set, show the prepare card at once. */
  forceWait: boolean;
  /**
   * A tool is actively running — the running-tool card already covers this
   * window, so suppress both the dots and the prepare card.
   */
  suppressed: boolean;
  /**
   * Milliseconds of no new content before we treat the stream as "preparing a
   * tool call" and swap the dots for a labelled, timed card. The configured
   * model buffers the entire tool-call JSON server-side before emitting
   * anything, so this gap can be long and otherwise looks like a dead stream.
   */
  stallMs?: number;
  /**
   * Persisted wall-clock ms of the last stream activity. When provided it
   * anchors the stall clock to a remount-stable value, so the timer keeps
   * counting from the real start after a session switch / page refresh
   * instead of resetting to zero. Falls back to local state when absent.
   */
  anchorTs?: number;
}

// Shared motion params for the two-state crossfade (three dots ↔ preparing card). A module-level constant —— this component
// self-triggers a re-render every 500ms, so we don't rebuild the object inside the render body.
const STATE_MOTION = {
  initial: { opacity: 0, y: 4 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: 4 },
  transition: { duration: 0.18, ease: EASE.standard },
} as const;

/**
 * Decides what to show at the tail of a still-streaming text bubble:
 *
 *  - content actively flowing  → three animated dots (generic streaming)
 *  - no new content for stallMs (or backend said `tool_pending`)
 *                              → "Preparing to call a tool… M:SS" card with a live timer
 *
 * The stall path is a frontend-side safety net: it does not depend on the
 * backend's 3s-of-total-silence `tool_pending` heuristic firing (which is
 * fragile when the model dribbles empty deltas while buffering a large
 * tool-call payload), so the user always gets a labelled, timed indicator
 * instead of bare dots during a long "complex tool call" preparation gap.
 */
export function StreamWaitIndicator({
  signature,
  forceWait,
  suppressed,
  stallMs = 2500,
  anchorTs,
}: StreamWaitIndicatorProps) {
  const [now, setNow] = useState(() => Date.now());
  const [lastChange, setLastChange] = useState(now);
  const [prevSig, setPrevSig] = useState(signature);

  // Reset the stall clock whenever new content flows in. Adjusting state
  // during render (React's documented "store previous value" pattern) avoids
  // a setState-in-effect round-trip; `now` is accurate to within the 500ms
  // tick, which is irrelevant against a multi-second stall threshold.
  if (prevSig !== signature) {
    setPrevSig(signature);
    setLastChange(now);
  }

  // Re-evaluate periodically so we cross the stall threshold without needing
  // a new render to be triggered from elsewhere.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, []);

  if (suppressed) return null;

  // Prefer the persisted, remount-stable anchor so the stall clock survives
  // session switches / refreshes; fall back to local `lastChange` without it.
  const effectiveSince = anchorTs ?? lastChange;
  const stalledFor = now - effectiveSince;
  const waiting = forceWait || stalledFor >= stallMs;

  // Two-state crossfade: three dots ↔ "Preparing to call a tool" card. mode="wait" exits before entering,
  // initial={false} prevents the entrance animation from replaying on the first frame during history replay / remount.
  return (
    <AnimatePresence mode="wait" initial={false}>
      {!waiting ? (
        <motion.span key="dots" className="jx-streamingIndicator" aria-hidden="true" {...STATE_MOTION}>
          <span className="jx-streamingDot" />
          <span className="jx-streamingDot" />
          <span className="jx-streamingDot" />
        </motion.span>
      ) : (
        // Time the wait from when content last changed, so the counter reads
        // "this tool call has been preparing for N seconds" rather than the
        // whole-message age.
        <motion.div
          key="wait"
          className="jx-inlineSummary jx-inlineSummary--wait"
          role="status"
          aria-live="polite"
          style={{ cursor: 'default' }}
          {...STATE_MOTION}
        >
          <BrandLoader done={false} label={t('正在准备调用工具')} />
          <span className="jx-inlineSummaryText">{t('正在准备调用工具…')}</span>
          <ElapsedTimer startTs={effectiveSince} className="jx-inlineSummaryTimer" />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
