import { ExclamationCircleOutlined, LoadingOutlined, PauseCircleOutlined } from '@ant-design/icons';
import { Modal } from 'antd';
import { t } from '../../i18n';
import { useSubagentIdentity } from '../../hooks/useSubagentIdentity';
import { useCanvasStore } from '../../stores/canvasStore';
import type { ToolCall } from '../../types';
import { subagentStatus } from '../../utils/subagentView';
import { AgentIcon } from '../agent/AgentIcon';
import { ElapsedTimer } from '../common';
import type { ToolMessageIdentity } from './ToolMessageContext';

export function SubagentStatus({ tool, isStreaming }: { tool: ToolCall; isStreaming?: boolean }) {
  const status = subagentStatus(tool, isStreaming);
  if (status === 'running') return <LoadingOutlined spin className="jx-tcr-icon jx-tcr-icon--running" aria-label={t('正在执行')} />;
  if (status === 'error') return <ExclamationCircleOutlined aria-label={t('执行失败')} />;
  if (status === 'interrupted') return <PauseCircleOutlined aria-label={t('已中断')} />;
  return null;
}

export function SubagentCallRow({ tool, isStreaming, identity }: {
  tool: ToolCall;
  isStreaming?: boolean;
  identity: ToolMessageIdentity & { messageUid: string };
}) {
  const agent = useSubagentIdentity(tool);
  const running = subagentStatus(tool, isStreaming) === 'running';
  const open = () => {
    const state = useCanvasStore.getState();
    const action = () => state.openSubagent({
      chatId: identity.chatId, messageUid: identity.messageUid, toolId: tool.id!,
      agent: { agent_id: agent.agent_id, name: agent.name, avatar: agent.avatar },
    });
    const active = state.tabs.find((tab) => tab.id === state.activeTabId);
    if (active?.kind === 'file' && active.dirty) {
      Modal.confirm({ title: t('有未保存的修改'), content: t('离开后编辑内容将丢失，确定继续？'),
        okText: t('放弃修改'), cancelText: t('取消'), okButtonProps: { danger: true }, onOk: action });
    } else action();
  };
  return (
    <div className="jx-tcr">
      <button type="button" className="jx-tcr-header jx-subagentCall" onClick={open}
        aria-label={t('查看智能体「{name}」的执行过程', { name: agent.name })}>
        <span className="jx-tcr-status"><SubagentStatus tool={tool} isStreaming={isStreaming} /></span>
        <AgentIcon agent={agent} size={22} />
        <span className="jx-tcr-label"><span className={running ? 'jx-tcr-prefix jx-tcr-prefix--shimmer' : 'jx-tcr-prefix'}>{agent.name}</span></span>
        {running && tool.timestamp ? <ElapsedTimer startTs={tool.timestamp} className="jx-tcr-timer" /> : null}
        {!running && tool.durationMs != null ? <span className="jx-tcr-timer">{t('用时 {sec}秒', { sec: (tool.durationMs / 1000).toFixed(1) })}</span> : null}
        <span className="jx-subagentCall-arrow" aria-hidden="true">›</span>
      </button>
    </div>
  );
}
