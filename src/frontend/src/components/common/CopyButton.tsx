import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, message } from 'antd';
import type { ButtonProps } from 'antd';
import { CheckOutlined, CopyOutlined } from '@ant-design/icons';
import { motion } from 'motion/react';
import { t } from '../../i18n';
import { copyToClipboard } from '../../utils/clipboard';

interface CopyButtonProps extends Omit<ButtonProps, 'icon' | 'onClick'> {
  /** Text to copy; if a function is passed, it is evaluated on click */
  text?: string | (() => string);
  /** Custom copy action (for a fallback path / custom toast); returning false means failure and does not enter the ✓ state */
  onCopy?: () => boolean | Promise<boolean>;
  children?: ReactNode;
}

/** Copy button: on success the icon morphs to a green ✓ for 2s (key remount + backOut pop-in), then reverts */
export function CopyButton({ text, onCopy, children, ...buttonProps }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  // Do not play the morph animation on first mount; only allow it after a real copy action (prevents all icons popping together when a list renders)
  const [hasCopied, setHasCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const handleClick = async () => {
    let ok = true;
    if (onCopy) {
      ok = await onCopy();
    } else {
      const value = typeof text === 'function' ? text() : (text ?? '');
      ok = await copyToClipboard(value);
      if (ok) message.success(t('已复制'));
      else message.error(t('复制失败，请手动复制'));
    }
    if (!ok) return;
    setHasCopied(true);
    setCopied(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Button
      {...buttonProps}
      onClick={handleClick}
      icon={(
        <motion.span
          key={copied ? 'check' : 'copy'}
          initial={hasCopied ? { scale: 0.5, opacity: 0 } : false}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.15, ease: 'backOut' }}
          style={{ display: 'inline-flex' }}
        >
          {copied ? <CheckOutlined style={{ color: 'var(--color-success)' }} /> : <CopyOutlined />}
        </motion.span>
      )}
    >
      {children}
    </Button>
  );
}
