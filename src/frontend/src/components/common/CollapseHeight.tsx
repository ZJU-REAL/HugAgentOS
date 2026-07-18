// Boilerplate for a boolean-toggled height expand/collapse (height: 0 <-> auto + opacity).
// For cases needing directional displacement / custom variants / mode="wait", still write motion.div directly;
// for cases achievable with pure CSS and always rendered, prefer .jx-expandWrap.
import { AnimatePresence, motion } from 'motion/react';
import type { CSSProperties, ReactNode } from 'react';
import { DUR, EASE } from '../../utils/motionTokens';

export function CollapseHeight({
  show, duration = DUR.normal, className, style, children, initial = false, motionKey,
}: {
  show: boolean;
  duration?: number;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
  /** Whether to play the enter animation on first mount (default false: history replay is silent) */
  initial?: boolean;
  motionKey?: string;
}) {
  return (
    <AnimatePresence initial={initial}>
      {show && (
        <motion.div
          key={motionKey}
          className={className}
          style={{ overflow: 'hidden', ...style }}
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration, ease: EASE.standard }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
