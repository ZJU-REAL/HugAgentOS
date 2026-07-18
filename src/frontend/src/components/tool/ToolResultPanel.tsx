import { useRef } from 'react';
import { CloseOutlined, ToolOutlined, BulbOutlined } from '@ant-design/icons';
import { TOOL_ICONS } from '../../utils/constants';
import { useChatStore, useUIStore } from '../../stores';
import { renderToolOutputBody } from './ToolOutputRenderer';
import { ToolTimelinePanel } from './ToolTimelinePanel';
import { ThinkingDetailPanel } from '../chat/ThinkingDetailPanel';
import { t } from '../../i18n';

export function ToolResultPanel() {
  const { toolResultPanel, setToolResultPanel } = useChatStore();
  const { setDetailModal } = useUIStore();
  const trpBodyRef = useRef<HTMLDivElement | null>(null);
  const showScrollbar = () => trpBodyRef.current?.classList.add('show-scrollbar');
  const hideScrollbar = () => trpBodyRef.current?.classList.remove('show-scrollbar');

  if (!toolResultPanel) return null;

  const isTimeline = toolResultPanel.toolName === '__progress_timeline__';
  const isThinking = toolResultPanel.toolName === '__thinking_detail__';

  const headerIcon = isTimeline
    ? <ToolOutlined style={{ fontSize: 18, color: 'rgba(67,56,202,.80)' }} />
    : isThinking
    ? <BulbOutlined style={{ fontSize: 18, color: 'rgba(100,116,139,.88)' }} />
    : <img className="jx-trp-icon" src={TOOL_ICONS[toolResultPanel.toolName] || '/icons/knowledge.png'} alt="" />;

  const renderBody = () => {
    if (isTimeline) {
      const data = toolResultPanel.output as { message: any; toolCalls: any[] };
      return <ToolTimelinePanel message={data.message} toolCalls={data.toolCalls} />;
    }
    if (isThinking) {
      const data = toolResultPanel.output as { content: string; isActive: boolean };
      return <ThinkingDetailPanel content={data.content} isActive={data.isActive} />;
    }
    return renderToolOutputBody(toolResultPanel.toolName, toolResultPanel.output, setDetailModal);
  };

  return (
    <div className="jx-toolResultPanel" onMouseEnter={showScrollbar} onMouseLeave={hideScrollbar}>
      <div className="jx-trp-header">
        <div className="jx-trp-headerRow">
          <div className="jx-trp-headerLeft">
            {headerIcon}
            <span className="jx-trp-title">{toolResultPanel.displayName}</span>
          </div>
          <button className="jx-trp-close" onClick={() => setToolResultPanel(null)} aria-label={t('关闭面板')}>
            <CloseOutlined />
          </button>
        </div>
        {toolResultPanel.summary && <div className="jx-trp-summary">{toolResultPanel.summary}</div>}
      </div>
      {/* key 重挂：切换不同工具的结果时内容淡入（顺带重置滚动位置） */}
      <div className="jx-trp-body jx-trp-body--switch" key={toolResultPanel.key} ref={trpBodyRef}>
        {renderBody()}
      </div>
    </div>
  );
}
