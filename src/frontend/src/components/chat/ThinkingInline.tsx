import { useEffect, useState } from 'react';
import { RightOutlined } from '@ant-design/icons';
import { useOverflowFade } from '../../hooks';
import { scrollElementToBottom } from '../../utils/scroll';
import { BrandLoader } from '../common';
import { t } from '../../i18n';

interface ThinkingInlineProps {
  content: string;
  isActive: boolean;
}

/**
 * Collapsed thinking block shown when dispatchProcessVisible is off.
 *
 * Same quiet one-line header as before; clicking unfolds the reasoning in place
 * instead of pushing a card into the right-hand Canvas. The body is the shared
 * `.jx-thinkingContent` box (bounded height + auto-scroll while streaming), so
 * it reads identically to the thinking block elsewhere in the message.
 */
export function ThinkingInline({ content, isActive }: ThinkingInlineProps) {
  const [open, setOpen] = useState(false);
  const [boxRef, innerRef, overflowing] = useOverflowFade(open);

  useEffect(() => {
    if (open && isActive && boxRef.current) scrollElementToBottom(boxRef.current);
  }, [content, open, isActive, boxRef]);

  const hasContent = !!content.trim();
  const toggle = () => { if (hasContent) setOpen(v => !v); };

  return (
    <div className="jx-inlineFold">
      <div className="jx-inlineSummary" role="button" tabIndex={0} aria-expanded={open} onClick={toggle}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } }}>
        <BrandLoader done={!isActive} label={isActive ? t('正在思考') : t('思考完成')} />
        <span className={`jx-inlineSummaryText${isActive ? ' jx-inlineSummaryText--live' : ''}`}>
          {isActive ? t('正在思考…') : t('思考过程')}
        </span>
        {hasContent && <RightOutlined className="jx-inlineSummaryArrow" rotate={open ? 90 : 0} />}
      </div>

      {hasContent && (
        <div className={`jx-expandWrap${open ? ' jx-expandWrap--open' : ''}`}>
          <div
            ref={boxRef}
            className={`jx-thinkingContent${overflowing ? ' jx-inlineFold-fade' : ''}`}
          >
            <div ref={innerRef}>{content}</div>
          </div>
        </div>
      )}
    </div>
  );
}
