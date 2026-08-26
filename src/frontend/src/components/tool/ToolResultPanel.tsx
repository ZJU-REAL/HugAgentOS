import { useRef } from 'react';
import { CloseOutlined } from '@ant-design/icons';
import { resolveToolIcon } from '../../utils/toolMeta';
import { useChatStore, useUIStore } from '../../stores';
import { renderToolOutputBody } from './ToolOutputRenderer';
import { t } from '../../i18n';

export function ToolResultPanel() {
  const { toolResultPanel, setToolResultPanel } = useChatStore();
  const { setDetailModal } = useUIStore();
  const trpBodyRef = useRef<HTMLDivElement | null>(null);
  const showScrollbar = () => trpBodyRef.current?.classList.add('show-scrollbar');
  const hideScrollbar = () => trpBodyRef.current?.classList.remove('show-scrollbar');

  if (!toolResultPanel) return null;

  return (
    <div className="jx-toolResultPanel" onMouseEnter={showScrollbar} onMouseLeave={hideScrollbar}>
      <div className="jx-trp-header">
        <div className="jx-trp-headerRow">
          <div className="jx-trp-headerLeft">
            <img className="jx-trp-icon" src={resolveToolIcon(toolResultPanel.toolName)} alt="" />
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
        {renderToolOutputBody(toolResultPanel.toolName, toolResultPanel.output, setDetailModal)}
      </div>
    </div>
  );
}
