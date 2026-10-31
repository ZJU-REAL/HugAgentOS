import { ExclamationCircleOutlined, LoadingOutlined, PauseCircleOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { useSubagentIdentity } from '../../hooks/useSubagentIdentity';
import type { ToolCall } from '../../types';
import { subagentStatus } from '../../utils/subagentView';
import { AgentIcon } from '../agent/AgentIcon';
import { openSubagentTab } from './openSubagentTab';
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
  const open = () => openSubagentTab(tool, agent, identity);
  return (
    <div className="jx-tcr">
      <button type="button" className="jx-tcr-header jx-subagentCall" onClick={open}
        aria-label={t('查看智能体「{name}」的执行过程', { name: agent.name })}>
        <span className="jx-tcr-status"><SubagentStatus tool={tool} isStreaming={isStreaming} /></span>
        <AgentIcon agent={agent} size={22} />
        <span className="jx-tcr-label"><span className={running ? 'jx-tcr-prefix jx-tcr-prefix--shimmer' : 'jx-tcr-prefix'}>{agent.name}</span></span>
        <span className="jx-tcr-arrow" aria-hidden="true" />
      </button>
    </div>
  );
}
