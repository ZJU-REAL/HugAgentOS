import { AnimatePresence, motion } from 'motion/react';
import { useStallDetector } from '../../hooks/useStallDetector';
import { EASE } from '../../utils/motionTokens';

interface StreamWaitIndicatorProps {
  signature: string | number;
  forceWait?: boolean;
  suppressed?: boolean;
  stallMs?: number;
  anchorTs?: number;
}

const STATE_MOTION = {
  initial: { opacity: 0, y: 4 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: 4 },
  transition: { duration: 0.18, ease: EASE.standard },
} as const;

/** Show streaming dots during output; silent waits add no text or timer. */
export function StreamWaitIndicator({
  signature,
  forceWait,
  suppressed,
  stallMs = 2500,
  anchorTs,
}: StreamWaitIndicatorProps) {
  const stall = useStallDetector(signature, stallMs, anchorTs, !suppressed);
  if (suppressed) return null;

  return (
    <AnimatePresence mode="wait" initial={false}>
      {!forceWait && !stall.waiting ? (
        <motion.span key="dots" className="jx-streamingIndicator" aria-hidden="true" {...STATE_MOTION}>
          <span className="jx-streamingDot" />
          <span className="jx-streamingDot" />
          <span className="jx-streamingDot" />
        </motion.span>
      ) : null}
    </AnimatePresence>
  );
}
