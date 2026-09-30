import { AnimatePresence, motion } from 'motion/react';
import type { ReactNode } from 'react';
import { EASE, SLIDE_EASE } from '../../utils/motionTokens';
export function SlidePanel({ show, panelKey, children, className, x = 24, duration = 0.25 }: {
  show: boolean; panelKey: string; children: ReactNode; className?: string; x?: number; duration?: number;
}) {
  return (
    <AnimatePresence>
      {show && (
        <motion.div
          key={panelKey}
          className={className}
          initial={{ opacity: 0, x }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x, transition: { duration: duration * 0.7, ease: EASE.exit } }}
          transition={{ duration, ease: SLIDE_EASE }}
          /* display:contents cannot be used — it generates no box, so opacity/transform
           * all stop working. This participates in the .jx-mainRow layout as a real flex
           * child; width comes from the optional slot class or the inner panel. */
          style={{ display: 'flex', flex: 'none', height: '100%', minWidth: 0 }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
