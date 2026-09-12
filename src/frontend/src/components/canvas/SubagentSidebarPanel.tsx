import { useCallback, useEffect, useRef, useState } from 'react';
import { getToolCallResult } from '../../api';
import { t } from '../../i18n';
import { useCanvasStore, useChatStore, useUIStore } from '../../stores';
import type { SubagentPanelTarget } from '../../stores/canvasStore';
import type { ChatMessage, ToolCall } from '../../types';
import { subagentDisplayText, subagentStatus } from '../../utils/subagentView';
import { AgentIcon } from '../agent/AgentIcon';
import { CitationMarkdownBlock } from '../citation';
import { ElapsedTimer } from '../common';
import { SubagentStatus } from '../tool/SubagentCallRow';
import { useSubagentIdentity } from '../../hooks/useSubagentIdentity';
import { ThinkingInline } from '../chat/ThinkingInline';
import { ToolRunShell } from '../tool/ToolRunShell';
import { ToolProgressInline } from '../tool/ToolProgressInline';
import { groupExecutionRuns } from '../../utils/executionRuns';
import { ToolMessageContext } from '../tool/ToolMessageContext';
import { CanvasTabBar } from './CanvasTabBar';

export function SubagentSidebarPanel() {
  const target = useCanvasStore((state) => state.subagentTarget);
  const message = useChatStore((state) => target
    ? state.store.chats[target.chatId]?.messages.find((item) => item.uid === target.messageUid)
    : undefined);
  const tool = message?.toolCalls?.find((item) => item.id === target?.toolId);
  return (
    <aside className="jx-rightSidebar jx-rightSidebar--subagent" aria-label={t('子智能体')}>
      <CanvasTabBar />
      {target && message && tool ? <SubagentDetail key={`${target.messageUid}:${target.toolId}`} target={target} message={message} tool={tool} /> : (
        <div className="jx-rightSidebar-empty">{t('暂无智能体执行记录')}</div>
      )}
    </aside>
  );
}

function SubagentDetail({ target, message, tool }: { target: SubagentPanelTarget; message: ChatMessage; tool: ToolCall }) {
  const agent = useSubagentIdentity(tool);
  const running = subagentStatus(tool, message.isStreaming) === 'running';
  const body = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const applyOutput = useChatStore((state) => state.applyToolCallOutput);
  const loadOutput = useCallback(async () => {
    if (!message.messageId || !tool.id) return;
    setLoading(true);
    setFailed(false);
    try {
      const detail = await getToolCallResult(target.chatId, message.messageId, tool.id);
      applyOutput(target.chatId, message.messageId, tool.id, detail.result);
    } catch { setFailed(true); }
    finally { setLoading(false); }
  }, [target.chatId, message.messageId, tool.id, applyOutput]);
  useEffect(() => {
    if (tool.outputTruncated && !tool.outputLoaded) void loadOutput();
  }, [tool.outputTruncated, tool.outputLoaded, loadOutput]);
  useEffect(() => {
    if (!following.current) return;
    const frame = requestAnimationFrame(() => {
      if (body.current) body.current.scrollTop = body.current.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [tool, running]);

  const dispatchProcessVisible = useUIStore((state) => state.dispatchProcessVisible);
  const transcript: ChatMessage = {
    ...message, uid: `${message.uid}:${tool.id}`, ts: tool.timestamp ?? message.ts, isStreaming: running,
    toolCalls: [], segments: [], content: '', isMarkdown: true,
  };
  for (const step of tool.subSteps || []) {
    if (step.kind === 'tool') {
      transcript.segments!.push({ type: 'tool', toolIndex: transcript.toolCalls!.length });
      transcript.toolCalls!.push({ id: step.toolId, name: step.name || 'tool', displayName: step.displayName,
        input: step.input, inputText: step.inputText, output: step.output, status: step.status });
    } else {
      transcript.segments!.push({ type: step.kind === 'content' ? 'text' : 'thinking', content: step.text || '' });
    }
  }
  const result = subagentDisplayText(tool.output, tool.subagentName || agent.name);
  if (result) {
    const last = transcript.segments!.at(-1);
    // The final tool response can wrap the already-streamed answer; replace its
    // final text segment rather than printing the same answer twice.
    if (last?.type === 'text' && last.content?.trim() && result.includes(last.content.trim())) last.content = result;
    else transcript.segments!.push({ type: 'text', content: result });
  }
  const { runs, suppressedIdx } = groupExecutionRuns(transcript, dispatchProcessVisible);
  const runByAnchor = new Map(runs.map((run) => [run.anchor, run]));
  return (
    <div className="jx-rightSidebar-body jx-subagentDetail" ref={body} onScroll={() => {
      const element = body.current;
      if (element) following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 72;
    }}>
      <header className="jx-subagentDetail-head">
        <AgentIcon agent={agent} size={32} />
        <strong>{agent.name}</strong>
        <SubagentStatus tool={tool} isStreaming={message.isStreaming} />
        {running && tool.timestamp ? <ElapsedTimer startTs={tool.timestamp} /> : null}
        {!running && tool.durationMs != null ? <span>{t('用时 {sec}秒', { sec: (tool.durationMs / 1000).toFixed(1) })}</span> : null}
      </header>
      <div className="jx-msg assistant">
        <ToolMessageContext.Provider value={null}>
          {transcript.segments!.map((segment, index) => {
            const key = `${transcript.uid}:${index}`;
            const run = runByAnchor.get(index);
            if (run) return dispatchProcessVisible
              ? <ToolRunShell key={key} steps={run.steps} isStreaming={running} />
              : <ToolProgressInline key={key} message={transcript} toolCalls={run.tools} />;
            if (suppressedIdx.has(index)) return null;
            if (segment.type === 'thinking') return <ThinkingInline key={key} content={segment.content || ''}
              isActive={running && !transcript.segments!.slice(index + 1).some((item) => item.type === 'text')} />;
            if (segment.type !== 'text' || !segment.content) return null;
            return <div key={key} className={`jx-bubble jx-md${running && index === transcript.segments!.length - 1 ? ' streaming' : ''}`}>
              <CitationMarkdownBlock className="jx-msgText" text={segment.content} isMarkdown
                citations={message.citations || []} messageIsStreaming={running} />
            </div>;
          })}
        </ToolMessageContext.Provider>
      </div>
      {loading && <p role="status">{t('正在加载完整结果…')}</p>}
      {failed && <button type="button" onClick={() => void loadOutput()}>{t('加载完整结果失败，请重试')}</button>}
    </div>
  );
}
