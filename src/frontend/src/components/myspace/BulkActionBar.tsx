import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'motion/react';
import { SPRING } from '../../utils/motionTokens';
import { t } from '../../i18n';

interface BulkActionBarProps {
  /** Whether to show the floating bar (exit animation handled by AnimatePresence) */
  open: boolean;
  /** Selected count (a small count pop confirms on change) */
  count: number;
  /** Action buttons (including divider / cancel button, composed by the caller as needed) */
  children: ReactNode;
}

/** Bulk action floating bar: Portal is always mounted on body, AnimatePresence wraps the conditional render inside the Portal;
 *  the centering offset (x:'-50%') is written into the motion value, CSS no longer has a transform (two transforms would override each other). */
export function BulkActionBar({ open, count, children }: BulkActionBarProps) {
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="jx-mySpace-bulkBar"
          initial={{ opacity: 0, y: 12, x: '-50%' }}
          animate={{ opacity: 1, y: 0, x: '-50%' }}
          exit={{ opacity: 0, y: 12, x: '-50%' }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
        >
          <motion.span
            key={count}
            className="jx-mySpace-bulkBar-count"
            initial={{ scale: 1.18 }}
            animate={{ scale: 1 }}
            transition={SPRING.pop}
          >
            {t('已选 {n} 项', { n: count })}
          </motion.span>
          <div className="jx-mySpace-bulkBar-divider" />
          {children}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
