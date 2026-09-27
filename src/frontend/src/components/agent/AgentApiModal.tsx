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
      {!agent.is_enabled && <Alert type="warning" showIcon title={t('此智能体已停用，API 调用会被拒绝；仍可管理密钥和查看记录。')} />}
      <Tabs destroyOnHidden items={[
        { key: 'keys', label: 'API-Key', children: <AgentApiKeys agentId={agent.agent_id} canCreate={agent.is_enabled} /> },
        { key: 'guide', label: t('调用方式'), children: <AgentApiGuide /> },
        { key: 'calls', label: t('调用记录'), children: <AgentApiCalls agentId={agent.agent_id} /> },
      ]} />
    </div>}
  </Modal>;
}
