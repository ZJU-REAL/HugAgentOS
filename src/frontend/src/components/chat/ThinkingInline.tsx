import { RightOutlined } from '@ant-design/icons';
import { useChatStore } from '../../stores';
import { BrandLoader } from '../common';
import { t } from '../../i18n';

interface ThinkingInlineProps {
  content: string;
  thinkKey: string;
  isActive: boolean;
}

/**
 * Single-line inline summary for thinking blocks.
 * Active: animated brand mark (GIF).
 * Done: static slate brand mark — same shape, settled color.
 */
export function ThinkingInline({ content, thinkKey, isActive }: ThinkingInlineProps) {
  const { toolResultPanel, setToolResultPanel } = useChatStore();

  const panelKey = `__thinking_detail__-${thinkKey}`;
  const isOpen = toolResultPanel?.key === panelKey;

  const handleClick = () => {
    if (isOpen) {
      setToolResultPanel(null);
    } else {
      setToolResultPanel({
        key: panelKey,
        toolName: '__thinking_detail__',
        displayName: isActive ? t('正在思考…') : t('思考过程'),
        output: { content, isActive },
      });
    }
  };

  return (
    <div className="jx-inlineSummary" role="button" tabIndex={0} onClick={handleClick}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleClick(); } }}>
      <BrandLoader done={!isActive} label={isActive ? t('正在思考') : t('思考完成')} />
      <span className="jx-inlineSummaryText">
        {isActive ? t('正在思考…') : t('思考过程')}
      </span>
      <RightOutlined className="jx-inlineSummaryArrow" />
    </div>
  );
}
