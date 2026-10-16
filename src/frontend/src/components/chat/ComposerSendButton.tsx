import { AnimatePresence, motion } from 'motion/react';
import { DUR } from '../../utils/motionTokens';
import { t } from '../../i18n';

export function ComposerSendButton({ stopping, disabled, onSend, onStop }: {
  stopping: boolean;
  disabled: boolean;
  onSend: () => void;
  onStop?: () => void;
}) {
  return <button type="button" className="jx-sendBtn" disabled={!stopping && disabled}
    aria-label={stopping ? t('中止') : t('发送')} onClick={() => stopping ? onStop?.() : onSend()}>
    <AnimatePresence mode="wait" initial={false}>
      <motion.img key={stopping ? 'stop' : 'send'} src={stopping ? '/home/stop.svg' : '/home/send.svg'}
        alt="" className="jx-sendIcon"
        initial={{ scale: 0.6, opacity: 0, rotate: -90 }}
        animate={{ scale: 1, opacity: 1, rotate: 0 }}
        exit={{ scale: 0.6, opacity: 0, rotate: 90 }}
        transition={{ duration: DUR.fast, ease: 'easeOut' }} />
    </AnimatePresence>
  </button>;
}
