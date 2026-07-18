import { AnimatePresence, motion } from 'motion/react';
import { EASE } from '../../utils/motionTokens';

export interface UploadProgress {
  done: number;
  total: number;
}

/** Thin progress bar under the toolbar: spring follows progress (scaleX, compositor-only, avoiding per-frame layout);
 *  on upload completion, exit first fills the bar, then the whole thing delays 400ms before fading out
 *  (all delegated to AnimatePresence; the component has zero local state). */
export function UploadProgressBar({ progress }: { progress: UploadProgress | null }) {
  const pct = progress && progress.total > 0
    ? Math.round((progress.done / progress.total) * 100)
    : 0;

  return (
    <AnimatePresence>
      {progress && (
        <motion.div
          className="jx-mySpace-uploadBar"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0, transition: { delay: 0.4, duration: 0.25, ease: EASE.exit } }}
        >
          <div className="jx-mySpace-uploadBar-track">
            <motion.div
              className="jx-mySpace-uploadBar-fill"
              initial={{ scaleX: 0 }}
              animate={{ scaleX: pct / 100 }}
              exit={{ scaleX: 1, transition: { duration: 0.2, ease: EASE.standard } }}
              transition={{ type: 'spring', stiffness: 120, damping: 22 }}
            />
          </div>
          <span className="jx-mySpace-uploadBar-text">
            <motion.span
              key={progress.done}
              className="jx-mySpace-uploadBar-num"
              initial={{ y: 6, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              transition={{ duration: 0.15, ease: EASE.brandOut }}
            >
              {progress.done}
            </motion.span>
            /{progress.total}
          </span>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
