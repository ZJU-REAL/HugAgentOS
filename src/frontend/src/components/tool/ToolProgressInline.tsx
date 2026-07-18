import { RightOutlined } from '@ant-design/icons';
import { TOOL_NAME_OVERRIDES } from '../../utils/constants';
import { useChatStore } from '../../stores';
import { BrandLoader, ElapsedTimer } from '../common';
import { computeEffectiveStatus } from './renderers/utils';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';

interface ToolProgressInlineProps {
  message: ChatMessage;
  /** Segment-level tool calls (subset) — if provided, only these are shown */
  toolCalls?: NonNullable<ChatMessage['toolCalls']>;
}

/**
 * Single-line inline summary for tool calls when dispatchProcessVisible is off.
 * Shows a pulse dot + tool names + ">" arrow. Clicking opens the Canvas timeline.
 */
export function ToolProgressInline({ message, toolCalls }: ToolProgressInlineProps) {
  const { toolDisplayNames, toolResultPanel, setToolResultPanel } = useChatStore();
  const tools = toolCalls ?? message.toolCalls ?? [];
  if (tools.length === 0) return null;

  // Converted via computeEffectiveStatus (when the message isn't streaming, running is treated as success) —
  // reading tool.status directly would leave a leftover running state spinning forever after an abort/abnormal interruption.
  const isRunning = (tool: (typeof tools)[number]) =>
    computeEffectiveStatus(tool, message.isStreaming) === 'running';
  const anyRunning = tools.some(isRunning);
  const runningTs = tools
    .filter(t => isRunning(t) && typeof t.timestamp === 'number')
    .map(t => t.timestamp as number);
  const startTs = runningTs.length > 0 ? Math.min(...runningTs) : message.ts;
  const names = tools
    .map(t => t.displayName || TOOL_NAME_OVERRIDES[t.name] || toolDisplayNames[t.name] || t.name)
    .filter((v, i, a) => a.indexOf(v) === i)   // dedupe
    .slice(0, 3);
  const label = names.join('、') + (tools.length > 3 ? t('等{n}项', { n: tools.length }) : '');

  const panelKey = `__progress_timeline__-${message.ts}`;
  const isOpen = toolResultPanel?.key === panelKey;

  const handleClick = () => {
    if (isOpen) {
      setToolResultPanel(null);
    } else {
      setToolResultPanel({
        key: panelKey,
        toolName: '__progress_timeline__',
        displayName: t('工具调用'),
        output: { message, toolCalls: tools },
      });
    }
  };

  return (
    <div className="jx-inlineSummary" role="button" tabIndex={0} onClick={handleClick}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleClick(); } }}>
      <BrandLoader done={!anyRunning} label={anyRunning ? t('正在调用工具') : t('工具调用完成')} />
      <span className="jx-inlineSummaryText">
        {anyRunning ? t('正在调用 {label}', { label }) : t('已调用 {label}', { label })}
      </span>
      {anyRunning && <ElapsedTimer startTs={startTs} className="jx-inlineSummaryTimer" />}
      <RightOutlined className="jx-inlineSummaryArrow" />
    </div>
  );
}
