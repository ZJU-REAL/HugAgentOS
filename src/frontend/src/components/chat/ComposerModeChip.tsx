import type React from 'react';
import { motion } from 'motion/react';
import { CloseOutlined } from '@ant-design/icons';
import { DUR, EASE } from '../../utils/motionTokens';

/** The "you are in plan / batch / loop mode" chip in the composer bar. The chip body is a pure
 *  status indicator; the ✕ badge pinned to its top-right corner is the one way to leave the mode
 *  (the "+" menu only turns modes on, so this ✕ must always be reachable). */
export function ComposerModeChip({
  icon, label, title, closeLabel, onClose,
}: {
  icon: React.ReactNode;
  label: string;
  title: string;
  closeLabel: string;
  onClose: () => void;
}) {
  return (
    <motion.span
      className="jx-composerChip jx-planModeBtn jx-modeChip active"
      role="status"
      title={title}
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: DUR.fast, ease: EASE.brandOut }}
    >
      {icon}
      <span className="jx-composerChip-label">{label}</span>
      <motion.button
        type="button"
        className="jx-modeChip-close"
        whileTap={{ scale: 0.88 }}
        onClick={onClose}
        aria-label={closeLabel}
        title={closeLabel}
      >
        <CloseOutlined />
      </motion.button>
    </motion.span>
  );
}
