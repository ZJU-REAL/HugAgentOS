import { useEffect, useState } from 'react';
import { Button, Form, Segmented, Select, Tag, message } from 'antd';
import { ArrowLeftOutlined, PlusOutlined, EditOutlined, UserOutlined, TeamOutlined } from '@ant-design/icons';
import { useAgentStore, type UserAgentItem } from '../../stores/agentStore';
import { listMyTeamsForProjects } from '../../api';
import type { TeamForProjectCreation } from '../../types';
import { AgentFormFields } from './AgentFormFields';
import { getRandomIconUrl } from './AgentPanel';
import { OntologyBuildValidationModal } from '../common/OntologyBuildValidationModal';
import { getOntologyBuildFailure, type OntologyBuildFailure } from '../../utils/apiError';
import { t } from '../../i18n';

interface AgentCreatePageProps {
  onBack: () => void;
  onCreated: () => void;
  agent?: UserAgentItem | null; // null/undefined = create mode, provided = edit mode
}

export function AgentCreatePage({ onBack, onCreated, agent }: AgentCreatePageProps) {
  const { createAgent, updateAgent, fetchAgents, fetchAvailableResources, availableResources } = useAgentStore();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [buildFailure, setBuildFailure] = useState<OntologyBuildFailure | null>(null);
  const isEdit = !!agent;
  // Creation scope: personal / team (team is only open for teams where you are owner/admin)
  const [scope, setScope] = useState<'personal' | 'team'>('personal');
  const [teamId, setTeamId] = useState<string | undefined>(undefined);
  const [managerTeams, setManagerTeams] = useState<TeamForProjectCreation[]>([]);
  const [heroIconUrl] = useState(() =>
    isEdit && agent ? getRandomIconUrl(agent.agent_id || agent.name) : getRandomIconUrl(String(Date.now()))
  );

  useEffect(() => {
    // In create mode, load "teams where I am owner/admin" for team-scope selection
    if (isEdit) return;
    listMyTeamsForProjects().then(setManagerTeams).catch(() => { /* ignore */ });
  }, [isEdit]);

  useEffect(() => {
    fetchAvailableResources();
    if (agent) {
      const ec = agent.extra_config || {};
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
        shared_context: !!ec.shared_context,
      });
    } else {
      form.resetFields();
      form.setFieldsValue({ max_iters: 10, shared_context: false, ontology_tags: [] });
    }
  }, [agent, fetchAvailableResources, form]);

  async function handleSubmit() {
    try {
      const values = await form.validateFields();
      // Merge shared_context into extra_config
      const { shared_context, ...rest } = values;
      const existingExtra = (isEdit ? agent?.extra_config : {}) || {};
      const extra_config: Record<string, unknown> = {
        ...existingExtra,
        shared_context: !!shared_context,
      };
      const payload: Partial<UserAgentItem> = { ...rest, extra_config };
      // Create a team sub-agent: include team_id (the backend uses it to set owner_type=team and verify management permission)
      if (!isEdit && scope === 'team') {
        if (!teamId) { message.error(t('请选择要归属的团队')); return; }
        payload.team_id = teamId;
      }

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
      const ontologyFailure = getOntologyBuildFailure(error);
      if (ontologyFailure) {
        setBuildFailure(ontologyFailure);
      } else {
        message.error(error instanceof Error ? error.message : (isEdit ? t('更新失败') : t('创建失败')));
      }
    } finally {
      setSaving(false);
    }
  }

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
          <div style={{ marginBottom: 16 }}>
            <Segmented
              value={scope}
              onChange={(v) => setScope(v as 'personal' | 'team')}
              options={[
                { label: t('个人'), value: 'personal', icon: <UserOutlined /> },
                { label: t('团队'), value: 'team', icon: <TeamOutlined />, disabled: managerTeams.length === 0 },
              ]}
            />
            {scope === 'team' && (
              <Select
                style={{ width: 260, marginLeft: 12 }}
                placeholder={t('选择要归属的团队')}
                value={teamId}
                onChange={setTeamId}
                showSearch
                optionFilterProp="label"
                options={managerTeams.map((tm) => ({ value: tm.team_id, label: tm.name }))}
              />
            )}
            <div style={{ marginTop: 6, color: 'var(--color-text-tertiary)', fontSize: 12 }}>
              {scope === 'personal'
                ? t('个人子智能体仅自己可见可用。')
                : t('团队子智能体对该团队全体成员可见可用，由团队 owner/admin 管理。')}
              {managerTeams.length === 0
                && t('（你不是任何团队的 owner/admin，暂不能创建团队子智能体）')}
            </div>
          </div>
        )}
        {isEdit && agent && (
          <div style={{ marginBottom: 16 }}>
            {agent.owner_type === 'team'
              ? <Tag icon={<TeamOutlined />} color="purple">{t('团队子智能体')}</Tag>
              : agent.owner_type === 'admin'
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
