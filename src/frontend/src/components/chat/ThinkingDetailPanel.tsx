import { useEffect, useRef } from 'react';
import { t } from '../../i18n';

interface ThinkingDetailPanelProps {
  content: string;
  isActive: boolean;
}

/**
 * Full thinking text rendered inside the Canvas (ToolResultPanel) body.
 * Auto-scrolls to bottom when streaming (isActive).
 */
export function ThinkingDetailPanel({ content, isActive }: ThinkingDetailPanelProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isActive && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [content, isActive]);

  return (
    <div
      ref={ref}
      className={`jx-thinkingDetailBody${isActive ? ' jx-thinkingDetailBody--streaming' : ''}`}
    >
      {content || t('暂无内容')}
    </div>
  );
}
