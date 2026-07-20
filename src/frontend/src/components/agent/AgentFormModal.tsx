import { useEffect, useState } from 'react';
import { Modal, Form, message } from 'antd';
import { useAgentStore, type UserAgentItem } from '../../stores/agentStore';
import { AgentFormFields } from './AgentFormFields';
import { OntologyBuildValidationModal } from '../common/OntologyBuildValidationModal';
import { getOntologyBuildFailure, type OntologyBuildFailure } from '../../utils/apiError';
import { t } from '../../i18n';

interface AgentFormModalProps {
  open: boolean;
  agent: UserAgentItem | null; // null = create mode
  onClose: () => void;
}

export function AgentFormModal({ open, agent, onClose }: AgentFormModalProps) {
  const { createAgent, updateAgent, fetchAgents, fetchAvailableResources, availableResources } =
    useAgentStore();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [buildFailure, setBuildFailure] = useState<OntologyBuildFailure | null>(null);

  useEffect(() => {
    if (open) {
      fetchAvailableResources();
      if (agent) {
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
          temperature: agent.temperature ?? 0.6,
        });
      } else {
        form.resetFields();
        form.setFieldsValue({ max_iters: 10, temperature: 0.6, ontology_tags: [] });
      }
    }
  }, [open, agent, fetchAvailableResources, form]);

  async function handleOk() {
    try {
      const values = await form.validateFields();
      setSaving(true);
      if (agent) {
        await updateAgent(agent.agent_id, values);
        message.success(t('已更新'));
      } else {
        await createAgent(values);
        message.success(t('已创建'));
      }
      await fetchAgents();
      onClose();
    } catch (error: unknown) {
      if (error && typeof error === 'object' && 'errorFields' in error) return;
      const ontologyFailure = getOntologyBuildFailure(error);
      if (ontologyFailure) {
        setBuildFailure(ontologyFailure);
      } else {
        message.error(error instanceof Error ? error.message : t('操作失败'));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <Modal
        title={agent ? t('编辑智能体') : t('创建智能体')}
        open={open}
        onOk={handleOk}
        onCancel={onClose}
        confirmLoading={saving}
        okText={agent ? t('保存') : t('创建')}
        cancelText={t('取消')}
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <AgentFormFields availableResources={availableResources} />
        </Form>
      </Modal>
      <OntologyBuildValidationModal failure={buildFailure} onClose={() => setBuildFailure(null)} />
    </>
  );
}
