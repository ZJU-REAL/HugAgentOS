import { useState } from 'react';
import { BulbOutlined, LoadingOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { useCollapseHeight } from '../../hooks/useCollapseHeight';

interface ThinkingStepRowProps {
  content: string;
  active: boolean;
}

/**
 * Compact "thinking" step rendered inside the ToolRunShell timeline.
 * Style mirrors ToolCallRow so the row blends into the step list.
 */
export function ThinkingStepRow({ content, active }: ThinkingStepRowProps) {
  const [expanded, setExpanded] = useState(false);
  // 同 ToolCallRow：开过就不再卸载，否则收起是瞬间的没有动画。
  const [everExpanded, setEverExpanded] = useState(false);
  const collapseRef = useCollapseHeight(expanded);
  const hasContent = !!content.trim();
  const canExpand = hasContent;
  const toggle = () => {
    if (!canExpand) return;
    setEverExpanded(true);
    setExpanded((v) => !v);
  };

  return (
    <div className="jx-tcr">
      <div
        className={`jx-tcr-header${expanded ? ' jx-tcr-header--open' : ''}`}
        role={canExpand ? 'button' : undefined}
        tabIndex={canExpand ? 0 : undefined}
        onClick={toggle}
        onKeyDown={(e) => {
          if (canExpand && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <span className="jx-tcr-status">
          {active
            ? <LoadingOutlined spin className="jx-tcr-icon jx-tcr-icon--running" />
            : <BulbOutlined className="jx-tcr-icon jx-tcr-icon--step" />
          }
        </span>
        <span className="jx-tcr-label">
          <span className={`jx-tcr-prefix${active ? ' jx-tcr-prefix--shimmer' : ''}`}>
            {active ? t('正在思考…') : t('思考')}
          </span>
        </span>
        {canExpand && (
          <span className={`jx-tcr-arrow${expanded ? ' jx-tcr-arrow--open' : ''}`} />
        )}
      </div>
      {hasContent && (
        <div
          ref={collapseRef}
            className={`jx-collapse${expanded ? ' jx-collapse--open' : ''}`}
        >
          <div className="jx-tcr-body">
            {(expanded || everExpanded) && (
              <div className={`jx-thinkingDetailBody${active ? ' jx-thinkingDetailBody--streaming' : ''}`}>
                {content}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
