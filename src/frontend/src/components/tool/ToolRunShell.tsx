import { useMemo, useState } from 'react';
import { CloseOutlined } from '@ant-design/icons';
import type { ToolCall } from '../../types';
import { AgentMark } from '../common/AgentMark';
import { TurnClock } from '../common/TurnClock';
import { RollingText } from '../common/RollingText';
import { ToolCallRow } from './ToolCallRow';
import { ThinkingStepRow } from './ThinkingStepRow';
import { CompactionStepRow } from './CompactionStepRow';
import { ToolKindStack } from './ToolKindStack';
import { compactionLabel, type CompactionProgress } from '../../utils/compactionProgress';
import { computeEffectiveStatus } from './renderers/utils';
import { t } from '../../i18n';
import { getDoneActionLabel, getRunningActionLabel } from '../../utils/toolActionLabel';
import { resolveToolDisplayName } from '../../utils/toolMeta';
import { agentStateForPhase, runPhase, waitLabel } from '../../utils/turnActivity';
import { useChatStore } from '../../stores';
import { useCollapseHeight } from '../../hooks/useCollapseHeight';

/**
 * A single entry in the shell timeline. Tool calls, thinking blocks, and
 * context compactions all render as steps in stream order, so the user
 * sees one combined "agent run" card instead of several inline indicators.
 */
export type ShellStep =
  | { kind: 'tool'; tool: ToolCall; key: string }
  | { kind: 'thinking'; content: string; active: boolean; key: string }
  | { kind: 'compaction'; progress: CompactionProgress; key: string };

interface ToolRunShellProps {
  /** Mixed steps (tool calls + thinking) belonging to one contiguous batch. */
  steps: ShellStep[];
  isStreaming?: boolean;
  /**
   * This run is the last thing in a streaming reply: once its tools finish it
   * stays open ("analysing results") until text or the end of the stream
   * arrives, instead of declaring itself done while the model is still busy.
   */
  isTail?: boolean;
  /** Ms since the reply last showed activity; paces hand-over and wait labels. */
  idleMs?: number;
  /**
   * The reply's travelling agent mark is parked on this tool-less run: the slot
   * carries this id and stays empty. Without it the run draws its own mark.
   */
  agentSlotKey?: string;
  /** Line this run takes over from (the startup row it replaces); its title rolls in from it. */
  takeOverFrom?: string;
  /** Start of the turn; while this run is live, the whole-turn clock trails its title. */
  turnStartTs?: number;
}

/**
 * Minimal collapsible shell wrapping a contiguous batch of steps.
 *
 * 始终折叠成一行，点击才展开——运行中也不例外。那一行依次是：这一批用到的工具种类
 * 图标、实际动作（"正在执行命令" → "运行了命令"）和耗时。切换工具时标题上下滚动、
 * 图标原地点亮，不做硬切；运行状态只靠点亮的图标和标题扫光表达。只有没有工具的一批
 * （纯思考、纯压缩）才在行首留智能体标记的槽位。
 */
export function ToolRunShell({ steps, isStreaming, isTail, idleMs = 0, agentSlotKey, takeOverFrom, turnStartTs }: ToolRunShellProps) {
  // 过程默认收起，跑起来也不自动展开——折叠那一行的文案已经说明了在做什么。
  const [open, setOpen] = useState(false);
  const toolDisplayNames = useChatStore((s) => s.toolDisplayNames);
  const collapseRef = useCollapseHeight(open);

  const tools = useMemo(() => steps.flatMap((s) => (s.kind === 'tool' ? [s.tool] : [])), [steps]);
  // 有工具的一行只留工具图标：图标本身就在说"在做什么"，前面再挂一枚智能体标记是重复。
  const hasTools = tools.length > 0;
  const toolStatuses = useMemo(() => tools.map((tool) => computeEffectiveStatus(tool, isStreaming)), [tools, isStreaming]);
  const compactions = steps.flatMap(s => s.kind === 'compaction' ? [s.progress] : []);
  const lastCompaction = compactions[compactions.length - 1];
  const phase = runPhase(steps, { isStreaming: !!isStreaming, isTail, idleMs, anyToolRunning: toolStatuses.includes('running') });
  const running = phase !== 'done';

  // MCP 连接器的名字是会话期动态下发的，先解析好再交给文案表——没登记在表里的
  // 工具就靠这个名字自报家门。
  const labeled = tools.map((tc) => ({
    name: tc.name,
    displayName: resolveToolDisplayName(tc, toolDisplayNames),
  }));
  const runningTool = labeled.find((_, i) => toolStatuses[i] === 'running');
  const lastTool = labeled[labeled.length - 1];
  const title = phase === 'compaction' ? t('正在压缩上下文')
    : phase === 'tool' ? getRunningActionLabel(runningTool!)
    : phase === 'thinking' ? t('正在思考')
    // 两次调用之间那一下空档仍报上一个工具，否则会闪一下"已完成"。
    : phase === 'handoff' ? (lastTool ? getRunningActionLabel(lastTool) : t('正在思考'))
    : phase === 'gap' ? waitLabel(idleMs, t('正在分析结果…'))
    : lastCompaction ? t(compactionLabel(lastCompaction))
    : getDoneActionLabel(labeled);

  // 批量作业（run_job）会把一轮卡在同一个工具里几十分钟——不把里面那张卡的实时进度
  // 顶到这一行，用户除了刷新页面没有别的办法判断它到底跑没跑完。
  const runningNote = running
    ? tools.find((tc, i) => toolStatuses[i] === 'running' && tc.progressNote)?.progressNote || ''
    : '';

  const failedCompaction = !running && !!lastCompaction && ['failed', 'interrupted'].includes(lastCompaction.status);
  // 只有尾部那一批里最后一步的思考算"正在思考"，早先的思考块在展开里已经是过去时。
  const lastStepKey = steps[steps.length - 1]?.key;

  // History / non-streaming renders are static: the `--static` modifier kills
  // the shell + step-row + title entrance animations (see tool.css), so
  // switching chats or reloading never replays the whole run card.
  return (
    <div className={`jx-trs jx-trs--turnStatus${hasTools ? ' jx-trs--tools' : ''}${isStreaming ? '' : ' jx-trs--static'}`}>
      <button
        type="button"
        className={`jx-trs-head jx-turnStatusRow${open ? ' jx-trs-head--open' : ''}`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {!hasTools && (
          <span className="jx-agentSlot" data-agent-slot={agentSlotKey} aria-hidden="true">
            {failedCompaction ? <CloseOutlined className="jx-agentSlot-failed" />
              : agentSlotKey ? null
              : <AgentMark
                  state={agentStateForPhase(phase)}
                  className={!running && isStreaming ? 'jx-anim-statusIn' : undefined}
                />}
          </span>
        )}
        <ToolKindStack tools={tools} statuses={toolStatuses} live={!!isStreaming} />
        <span className="jx-trs-summary">
          <RollingText
            text={title}
            shimmer={running}
            animate={!!isStreaming}
            smoothWidth
            rollFrom={takeOverFrom}
            className="jx-trs-title jx-turnStatusText"
          />
          {running && turnStartTs != null && <TurnClock startTs={turnStartTs} />}
          {runningNote && <span className="jx-trs-note">{runningNote}</span>}
        </span>
        <span className={`jx-trs-chev${open ? ' jx-trs-chev--open' : ''}`} aria-hidden="true" />
      </button>

      <div
        ref={collapseRef}
        className={`jx-trs-bodyWrap${open ? ' jx-trs-bodyWrap--open' : ''}`}
      >
        <div className="jx-trs-body">
          {steps.map((step) => {
            if (step.kind === 'tool') {
              return <ToolCallRow key={step.key} tool={step.tool} isStreaming={isStreaming} />;
            }
            if (step.kind === 'compaction') {
              return <CompactionStepRow key={step.key} progress={step.progress} />;
            }
            const active = isTail === undefined ? step.active : phase === 'thinking' && step.key === lastStepKey;
            return <ThinkingStepRow key={step.key} content={step.content} active={active} />;
          })}
        </div>
      </div>
    </div>
  );
}
