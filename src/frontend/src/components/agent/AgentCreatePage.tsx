import { useEffect, useState } from 'react';
import {
  ArrowLeftOutlined,
  EditOutlined,
  PlusOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, Form, Tag, message } from 'antd';

import { t } from '../../i18n';
import { useAgentStore } from '../../stores/agentStore';
import type { UserAgentItem } from '../../stores/agentStore';
import { getOntologyBuildFailure } from '../../utils/apiError';
import type { OntologyBuildFailure } from '../../utils/apiError';
import { OntologyBuildValidationModal } from '../common/OntologyBuildValidationModal';
import { AgentFormFields } from './AgentFormFields';
import { getRandomIconUrl } from './AgentPanel';

interface AgentCreatePageProps {
  onBack: () => void;
  onCreated: () => void;
  agent?: UserAgentItem | null;
}

export function AgentCreatePage({ onBack, onCreated, agent }: AgentCreatePageProps) {
  const {
    createAgent,
    updateAgent,
    fetchAgents,
    fetchAvailableResources,
    availableResources,
  } = useAgentStore();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [buildFailure, setBuildFailure] = useState<OntologyBuildFailure | null>(null);
  const isEdit = !!agent;
  const [heroIconUrl] = useState(() => (
    isEdit && agent
      ? getRandomIconUrl(agent.agent_id || agent.name)
      : getRandomIconUrl(String(Date.now()))
  ));

  useEffect(() => {
    void fetchAvailableResources();
    if (agent) {
      const extraConfig = agent.extra_config || {};
      form.setFieldsValue({
        name: agent.name,
        description: agent.description,
        system_prompt: agent.system_prompt,
        welcome_message: agent.welcome_message,
        mcp_server_ids: agent.mcp_server_ids || [],
        skill_ids: agent.skill_ids || [],
        plugin_ids: agent.plugin_ids || [],
        ontology_tags: agent.ontology_tags || [],
        max_iters: agent.max_iters ?? 10,
        shared_context: !!extraConfig.shared_context,
      });
    } else {
      form.resetFields();
      form.setFieldsValue({ max_iters: 10, shared_context: false, ontology_tags: [] });
    }
  }, [agent, fetchAvailableResources, form]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const { shared_context, ...rest } = values;
      const existingExtra = (isEdit ? agent?.extra_config : {}) || {};
      const payload: Partial<UserAgentItem> = {
        ...rest,
        extra_config: { ...existingExtra, shared_context: !!shared_context },
      };
      setSaving(true);
      if (isEdit) {
        await updateAgent(agent!.agent_id, payload);
        message.success(t('已更新'));
      } else {
        await createAgent(payload);
        message.success(t('已创建'));
      }
      await fetchAgents();
      onCreated();
    } catch (error: unknown) {
      if (error && typeof error === 'object' && 'errorFields' in error) return;
      const failure = getOntologyBuildFailure(error);
      if (failure) setBuildFailure(failure);
      else message.error(error instanceof Error ? error.message : (isEdit ? t('更新失败') : t('创建失败')));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="jx-agentCreatePage">
      <div className="jx-agentCreatePage-head">
        <button type="button" className="jx-agentCreatePage-back" onClick={onBack}>
          <ArrowLeftOutlined />
          <span>{t('返回子智能体列表')}</span>
        </button>
        <div className="jx-agentCreatePage-hero">
          <div className="jx-agentCreatePage-heroIcon">
            <img src={heroIconUrl} alt="" width={32} height={32} style={{ display: 'block' }} />
          </div>
          <div className="jx-agentCreatePage-heroBody">
            <h2 className="jx-agentCreatePage-title">{isEdit ? t('编辑智能体') : t('创建智能体')}</h2>
            <p className="jx-agentCreatePage-subtitle">{t('配置智能体名称、角色设定、绑定工具与技能')}</p>
          </div>
        </div>
      </div>
      <div className="jx-agentCreatePage-card">
        {!isEdit && (
          <div style={{ marginBottom: 16, color: 'var(--color-text-tertiary)', fontSize: 12 }}>
            <Tag icon={<UserOutlined />}>{t('个人')}</Tag>
            {t('个人子智能体仅自己可见可用。')}
          </div>
        )}
        {isEdit && agent && (
          <div style={{ marginBottom: 16 }}>
            {agent.owner_type === 'admin'
              ? <Tag color="gold">{t('系统内置')}</Tag>
              : <Tag icon={<UserOutlined />}>{t('个人子智能体')}</Tag>}
          </div>
        )}
        <Form form={form} layout="vertical">
          <AgentFormFields availableResources={availableResources} />
        </Form>
        <div className="jx-agentCreatePage-actions">
          <Button onClick={onBack}>{t('取消')}</Button>
          <Button
            type="primary"
            icon={isEdit ? <EditOutlined /> : <PlusOutlined />}
            loading={saving}
            onClick={() => void handleSubmit()}
          >
            {isEdit ? t('保存更改') : t('创建智能体')}
          </Button>
        </div>
      </div>
      <OntologyBuildValidationModal failure={buildFailure} onClose={() => setBuildFailure(null)} />
    </div>
  );
}
