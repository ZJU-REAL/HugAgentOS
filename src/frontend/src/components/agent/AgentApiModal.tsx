import { Alert, Modal, Tabs } from 'antd';
import type { UserAgentItem } from '../../stores/agentStore';
import { AgentApiKeys } from './AgentApiKeys';
import { AgentApiGuide } from './AgentApiGuide';
import { AgentApiCalls } from './AgentApiCalls';
import { t } from '../../i18n';
import '../../styles/agent-api.css';

export function AgentApiModal({ agent, open, onClose }: {
  agent: UserAgentItem; open: boolean; onClose: () => void;
}) {
  return <Modal centered open={open} onCancel={onClose} footer={null} destroyOnHidden
    title={t('「{name}」的 API-Key', { name: agent.name })} width={920}
    className="jx-agentApi-modal">
    {open && <div key={agent.agent_id} className="jx-agentApi-body">
      <Alert type="info" showIcon
        title={t('仅可调用当前智能体及该 Key 的 API 会话。')}
        description={t('API 模式不会自动使用个人记忆、我的空间或个人连接器登录；不支持的个人工具与沙箱操作会被拒绝。')} />
      {!agent.is_enabled && <Alert type="info" showIcon title={t('此智能体已停用，但仍可通过 API 单独调用。')} />}
      <Tabs destroyOnHidden items={[
        { key: 'keys', label: 'API-Key', children: <AgentApiKeys agentId={agent.agent_id} /> },
        { key: 'guide', label: t('调用方式'), children: <AgentApiGuide agentId={agent.agent_id} /> },
        { key: 'calls', label: t('调用记录'), children: <AgentApiCalls agentId={agent.agent_id} /> },
      ]} />
    </div>}
  </Modal>;
}
