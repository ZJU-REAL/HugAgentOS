import { AnimatePresence, motion } from 'motion/react';
import { UploadOutlined } from '@ant-design/icons';
import { EASE, SPRING } from '../../utils/motionTokens';

interface DropOverlayProps {
  /** Mounted only while dragging; pointer-events:none so it doesn't block clicks on the host area */
  active: boolean;
  /** Hint text (e.g. "Release to upload to the current folder") */
  hint: string;
  /** Modifier class appended after .jx-mySpace-dropOverlay (e.g. jx-projectRail-dropOverlay) */
  className?: string;
  iconSize?: number;
}

/** Drag-and-drop upload highlight layer: used together with useFileDropZone. */
export function DropOverlay({ active, hint, className, iconSize = 24 }: DropOverlayProps) {
  return (
    <AnimatePresence>
      {active && (
        <motion.div
          className={`jx-mySpace-dropOverlay${className ? ` ${className}` : ''}`}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15, ease: EASE.standard }}
        >
          <motion.div
            className="jx-mySpace-dropOverlay-card"
            initial={{ scale: 0.95 }}
            animate={{ scale: 1 }}
            transition={SPRING.panel}
          >
            <UploadOutlined style={{ fontSize: iconSize }} />
            <span>{hint}</span>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
