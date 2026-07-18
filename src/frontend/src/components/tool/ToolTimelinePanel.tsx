import { useState } from 'react';
import { RightOutlined } from '@ant-design/icons';
import { TOOL_NAME_OVERRIDES } from '../../utils/constants';
import { useChatStore, useUIStore } from '../../stores';
import { renderToolOutputBody } from './ToolOutputRenderer';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';

interface ToolTimelinePanelProps {
  message: ChatMessage;
  toolCalls: NonNullable<ChatMessage['toolCalls']>;
}

/**
 * Timeline view rendered inside the Canvas (ToolResultPanel) body.
 * Each tool call is a step; clicking a completed step shows its output below.
 */
export function ToolTimelinePanel({ message, toolCalls }: ToolTimelinePanelProps) {
  const { toolDisplayNames } = useChatStore();
  const { setDetailModal } = useUIStore();
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);

  const coerceOutput = (raw: unknown): unknown => {
    if (typeof raw !== 'string') return raw;
    try { return JSON.parse(raw); } catch { return raw; }
  };

  return (
    <div className="jx-toolTimeline">
      {toolCalls.map((tool, idx) => {
        const rawStatus = tool.status ?? 'success';
        const effectiveStatus: 'running' | 'success' | 'error' =
          rawStatus === 'error' ? 'error'
          : rawStatus === 'running' && !message.isStreaming ? 'success'
          : rawStatus === 'running' ? 'running'
          : 'success';

        const displayName = tool.displayName || TOOL_NAME_OVERRIDES[tool.name] || toolDisplayNames[tool.name] || tool.name;
        const summary = effectiveStatus === 'running' ? t('执行中...') : effectiveStatus === 'error' ? t('执行失败') : t('已完成');
        const isExpanded = expandedIdx === idx;
        const canExpand = effectiveStatus !== 'running' && !!tool.output;

        return (
          <div key={idx}>
            <div
              className={`jx-toolTimelineStep${isExpanded ? ' active' : ''}`}
              onClick={() => { if (canExpand) setExpandedIdx(isExpanded ? null : idx); }}
            >
              <span className={`jx-toolTimelineDot jx-toolTimelineDot--${effectiveStatus}`} />
              <div className="jx-toolTimelineInfo">
                <div className="jx-toolTimelineName">{displayName}</div>
                <div className="jx-toolTimelineSummary">{summary}</div>
              </div>
              {canExpand && <RightOutlined className="jx-toolTimelineChevron" rotate={isExpanded ? 90 : 0} />}
            </div>
            {/* 常驻渲染 + grid-rows 高度动画（与 ToolCallRow 展开体同构） */}
            {!!tool.output && (
              <div className={`jx-expandWrap${isExpanded ? ' jx-expandWrap--open' : ''}`}>
                <div className="jx-toolTimelineBody">
                  {isExpanded && renderToolOutputBody(tool.name, coerceOutput(tool.output), setDetailModal)}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
