import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Typography, message,
} from 'antd';
import { PlusOutlined, ThunderboltOutlined } from '@ant-design/icons';
import {
  assignModelRole,
  createModelProvider,
  deleteModelProvider,
  detectModelContextLength,
  getModelProviderSchemas,
  listModelProviders,
  listModelRoles,
  testModelProvider,
  unassignModelRole,
  updateModelProvider,
  type ModelProviderInput,
  type ModelProviderItem,
  type ModelRoleAssignment,
  type ProviderSchema,
} from '../../api';
import { t } from '../../i18n';
import { useModelCapabilitiesStore } from '../../stores';
import { describeContextProbe } from '../../utils/contextUsage';

const { Text } = Typography;

const TYPE_LABELS: Record<string, string> = {
  chat: t('对话'),
  embedding: t('向量'),
  reranker: t('重排'),
};

/**
 * The "Settings -> System Management -> Model Service" panel (model onboarding delegated to CE).
 *
 * A streamlined version for single-instance admins: provider create/edit/delete/test + role assignment.
 * Enterprise fields such as gateway grouping / weights / pricing are not exposed here (EE goes through the Config console).
 */
export function SystemModelPanel() {
  const [providers, setProviders] = useState<ModelProviderItem[]>([]);
  const [roles, setRoles] = useState<ModelRoleAssignment[]>([]);
  const [schemas, setSchemas] = useState<ProviderSchema[]>([]);
  const [loading, setLoading] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ModelProviderItem | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [assigningRole, setAssigningRole] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);
  // Explains where a detected window came from (or why nothing was found), kept next to
  // the input so the number is never a bare figure the operator has to take on faith.
  const [probeHint, setProbeHint] = useState('');
  const [form] = Form.useForm();
  const userModelSwitchEnabled = useModelCapabilitiesStore(
    (s) => s.capabilities.user_model_switch_enabled,
  );
  const selectableModels = useModelCapabilitiesStore(
    (s) => s.capabilities.user_selectable_models,
  );
  const selectedModelProviderId = useModelCapabilitiesStore((s) => s.selectedModelProviderId);
  const setSelectedModelProviderId = useModelCapabilitiesStore((s) => s.setSelectedModelProviderId);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [ps, rs] = await Promise.all([listModelProviders(), listModelRoles()]);
      setProviders(ps);
      setRoles(rs);
      void useModelCapabilitiesStore.getState().fetchCapabilities();
    } catch (e) {
      message.error(t('加载模型配置失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    getModelProviderSchemas().then(setSchemas).catch(() => setSchemas([]));
  }, [reload]);

  const providerOptions = useMemo(
    () => schemas.map((s) => ({ value: s.id, label: s.label || s.id })),
    [schemas],
  );

  const openCreate = () => {
    setEditing(null);
    setProbeHint('');
    form.resetFields();
    form.setFieldsValue({ provider: 'openai_compatible', provider_type: 'chat', is_active: true });
    setEditorOpen(true);
  };

  const openEdit = (p: ModelProviderItem) => {
    setEditing(p);
    setProbeHint(
      p.extra_config?.context_length_source
        ? t('当前值由系统自动探测填入，尚未人工确认；保存即视为确认。')
        : '',
    );
    form.setFieldsValue({
      display_name: p.display_name,
      provider: p.provider,
      provider_type: p.provider_type,
      base_url: p.base_url,
      api_key: '', // leave empty = no change (the masked value is never filled back)
      model_name: p.model_name,
      is_active: p.is_active,
      context_length: (p.extra_config?.context_length as number | undefined) ?? undefined,
      supports_reasoning_effort: Boolean(p.extra_config?.supports_reasoning_effort),
      supports_vision: Boolean(p.extra_config?.supports_vision),
    });
    setEditorOpen(true);
  };

  /**
   * Ask the backend to read the model's real context window off the vendor.
   *
   * Only fills the form — the operator still reviews the number and saves it, which is
   * what makes it safe to accept lower-confidence sources (a name-based guess) here.
   */
  const handleDetectContext = useCallback(async () => {
    const values = form.getFieldsValue();
    if (!values.model_name) {
      message.warning(t('请先填写模型名'));
      return;
    }
    setDetecting(true);
    try {
      const result = await detectModelContextLength({
        provider: values.provider || 'openai_compatible',
        provider_type: values.provider_type || 'chat',
        base_url: values.base_url || '',
        api_key: values.api_key || '',
        model_name: values.model_name,
        provider_id: editing?.provider_id,
      });
      setProbeHint(describeContextProbe(result));
      if (result.context_length > 0) {
        form.setFieldsValue({ context_length: result.context_length });
        message.success(t('已探测到上下文窗口 {n} token', { n: String(result.context_length) }));
      } else {
        message.warning(t('未能自动探测到上下文窗口，请手工填写'));
      }
    } catch (e) {
      message.error(t('探测失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setDetecting(false);
    }
  }, [form, editing]);

  const handleSave = async () => {
    const values = await form.validateFields();
    const extra: Record<string, unknown> = { ...(editing?.extra_config ?? {}) };
    if (values.context_length) extra.context_length = Number(values.context_length);
    else delete extra.context_length;
    // 仅 chat 类型有效；未勾选时删除该键，避免残留在非 chat 供应商上
    if (values.provider_type === 'chat' && values.supports_reasoning_effort) {
      extra.supports_reasoning_effort = true;
    } else {
      delete extra.supports_reasoning_effort;
    }
    if (values.provider_type === 'chat' && values.supports_vision) {
      extra.supports_vision = true;
    } else {
      delete extra.supports_vision;
    }
    const payload: Partial<ModelProviderInput> = {
      display_name: values.display_name,
      provider: values.provider,
      provider_type: values.provider_type,
      base_url: values.base_url || '',
      model_name: values.model_name,
      is_active: values.is_active,
      extra_config: extra,
    };
    if (values.api_key) payload.api_key = values.api_key;
    setSaving(true);
    try {
      if (editing) {
        await updateModelProvider(editing.provider_id, payload);
        message.success(t('模型供应商已更新'));
      } else {
        await createModelProvider({ api_key: '', ...payload } as ModelProviderInput);
        message.success(t('模型供应商已添加并通过连通性校验'));
      }
      setEditorOpen(false);
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (p: ModelProviderItem) => {
    try {
      await deleteModelProvider(p.provider_id);
      message.success(t('已删除'));
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const handleTest = async (p: ModelProviderItem) => {
    setTestingId(p.provider_id);
    try {
      const r = await testModelProvider(p.provider_id);
      if (r.success) {
        message.success(t('连通性正常（{ms}ms）', { ms: String(r.latency_ms) }));
      } else {
        message.error(t('连通性失败：{msg}', { msg: r.error || '' }));
      }
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setTestingId(null);
    }
  };

  const handleAssign = async (roleKey: string, providerId: string | null) => {
    setAssigningRole(roleKey);
    try {
      if (providerId) await assignModelRole(roleKey, providerId);
      else await unassignModelRole(roleKey);
      message.success(t('角色分配已更新'));
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setAssigningRole(null);
    }
  };

  const roleColumns = [
    {
      title: t('角色'),
      dataIndex: 'role_key',
      render: (v: string, r: ModelRoleAssignment) => (
        <Space direction="vertical" size={0}>
          <Text>{t((r.label as string) || v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>
        </Space>
      ),
    },
    {
      title: t('供应商'),
      dataIndex: 'provider_id',
      render: (v: string | null, r: ModelRoleAssignment) => (
        <Select
          size="small"
          style={{ minWidth: 220 }}
          value={v || undefined}
          placeholder={t('未分配')}
          allowClear
          loading={assigningRole === r.role_key}
          options={providers
            .filter(
              (p) =>
                (!r.type || p.provider_type === r.type) &&
                // 角色还可能要求能力位（视觉桥要求 supports_vision）。只按用途筛的话，
                // 纯文本模型也会出现在视觉桥的下拉里，配错要到对话中途才暴露。
                (!r.requires_capability ||
                  Boolean(p.extra_config?.[r.requires_capability]) ||
                  // 已指派的那个即使现在不合格也保留，否则界面会显示成未分配
                  p.provider_id === r.provider_id),
            )
            .map((p) => ({ value: p.provider_id, label: `${p.display_name} (${p.model_name})` }))}
          notFoundContent={
            r.requires_capability
              ? t('没有符合条件的模型：该角色只能指派已勾选「支持读图（多模态）」的供应商')
              : undefined
          }
          onChange={(pid) => void handleAssign(r.role_key, (pid as string) ?? null)}
        />
      ),
    },
  ];

  return (
    <div className="jx-sysPanel">
      <div className="jx-sysPanel-toolbar">
        <Text type="secondary">
          {t('接入 OpenAI 兼容或各厂商模型端点；保存时会做连通性校验。对话能力至少需要一个 chat 供应商并指派 main_agent 角色。')}
        </Text>
        <Space>
          <Button onClick={() => void reload()} loading={loading}>{t('刷新')}</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t('添加模型')}</Button>
        </Space>
      </div>

      <Table<ModelProviderItem>
        size="small"
        rowKey="provider_id"
        loading={loading}
        dataSource={providers}
        pagination={false}
        columns={[
          { title: t('名称'), dataIndex: 'display_name' },
          {
            title: t('类型'),
            dataIndex: 'provider_type',
            width: 80,
            render: (v: string) => <Tag>{TYPE_LABELS[v] || v}</Tag>,
          },
          { title: t('模型名'), dataIndex: 'model_name' },
          {
            title: t('状态'),
            dataIndex: 'last_test_status',
            width: 90,
            render: (v: string | null, p) => {
              if (!p.is_active) return <Tag>{t('停用')}</Tag>;
              if (v === 'success') return <Tag color="success">{t('正常')}</Tag>;
              if (v === 'failed') return <Tag color="error">{t('异常')}</Tag>;
              return <Tag color="default">{t('未测试')}</Tag>;
            },
          },
          {
            title: t('操作'),
            width: 200,
            render: (_: unknown, p) => (
              <Space size="small">
                <Button
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={testingId === p.provider_id}
                  onClick={() => void handleTest(p)}
                >
                  {t('测试')}
                </Button>
                <Button size="small" onClick={() => openEdit(p)}>{t('编辑')}</Button>
                <Popconfirm title={t('确认删除该供应商？')} onConfirm={() => void handleDelete(p)}>
                  <Button size="small" danger>{t('删除')}</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <h4 className="jx-sysPanel-subtitle">{t('角色指派')}</h4>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t('把供应商指派给系统角色（主智能体 / 摘要 / 向量等）；未指派的角色对应能力不可用。')}
      </Text>
      <Table<ModelRoleAssignment>
        size="small"
        rowKey="role_key"
        loading={loading}
        dataSource={roles}
        pagination={false}
        columns={roleColumns}
        style={{ marginTop: 8 }}
      />

      <h4 className="jx-sysPanel-subtitle">{t('模型选择')}</h4>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t('选择新对话默认使用的模型；同一个选择也会显示在对话输入框中，可随时切换。')}
      </Text>
      <div style={{ marginTop: 8 }}>
        <Select
          style={{ width: '100%', maxWidth: 420 }}
          value={selectedModelProviderId || undefined}
          placeholder={userModelSwitchEnabled ? t('请选择对话模型') : t('当前账号未开放模型切换权限')}
          disabled={!userModelSwitchEnabled || selectableModels.length === 0}
          options={selectableModels.map((model) => ({
            value: model.provider_id,
            label: `${model.display_name} (${model.model_name || model.provider})`,
          }))}
          onChange={(providerId) => setSelectedModelProviderId(providerId)}
        />
      </div>

      <Modal
        title={editing ? t('编辑模型供应商') : t('添加模型供应商')}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={() => void handleSave()}
        confirmLoading={saving}
        destroyOnClose
        width={520}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="display_name" label={t('显示名称')} rules={[{ required: true }]}>
            <Input placeholder={t('如：DeepSeek 官方')} maxLength={100} />
          </Form.Item>
          <Space.Compact block>
            <Form.Item name="provider" label={t('厂商 / 协议')} style={{ flex: 1 }} rules={[{ required: true }]}>
              <Select options={providerOptions} showSearch optionFilterProp="label" />
            </Form.Item>
            <Form.Item name="provider_type" label={t('用途')} style={{ width: 120, marginLeft: 8 }} rules={[{ required: true }]}>
              <Select
                options={[
                  { value: 'chat', label: TYPE_LABELS.chat },
                  { value: 'embedding', label: TYPE_LABELS.embedding },
                  { value: 'reranker', label: TYPE_LABELS.reranker },
                ]}
              />
            </Form.Item>
          </Space.Compact>
          <Form.Item name="base_url" label="base_url">
            <Input placeholder="https://api.deepseek.com" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            extra={editing ? t('留空表示不修改现有密钥') : undefined}
            rules={editing ? [] : []}
          >
            <Input.Password placeholder={editing ? '••••••••' : 'sk-...'} autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="model_name" label={t('模型名')} rules={[{ required: true }]}>
            <Input placeholder="deepseek-chat" />
          </Form.Item>
          <Space.Compact block>
            <Form.Item
              name="context_length"
              label={t('上下文窗口（token，可选）')}
              style={{ flex: 1 }}
              tooltip={t('模型真实的上下文长度，用于历史裁剪与自动压缩阈值。留空保存时系统会尝试自动探测（自建 vLLM 等会直接上报），探不到才需要手工填写。')}
              extra={probeHint || undefined}
            >
              <Input
                type="number"
                placeholder="131072"
                addonAfter={
                  <Button
                    type="link"
                    size="small"
                    loading={detecting}
                    onClick={handleDetectContext}
                    style={{ padding: 0, height: 'auto' }}
                  >
                    {t('自动探测')}
                  </Button>
                }
              />
            </Form.Item>
            <Form.Item name="is_active" label={t('启用')} valuePropName="checked" style={{ width: 90, marginLeft: 8 }}>
              <Switch />
            </Form.Item>
          </Space.Compact>
          <Form.Item noStyle shouldUpdate={(prev, cur) => prev.provider_type !== cur.provider_type}>
            {({ getFieldValue }) => getFieldValue('provider_type') === 'chat' && (
              <Form.Item
                label={t('支持多档思考强度（reasoning_effort）')}
                name="supports_reasoning_effort"
                valuePropName="checked"
                tooltip={t('开启后，前端「思考强度」选项里会出现「思考·高 / 思考·超高」两档，并通过 chat_template_kwargs.reasoning_effort 传给上游。需要上游模型本身认 reasoning_effort 字段（如 Qwen3 多档、GPT-OSS、Claude thinking 等），否则可能 4xx。普通 DeepSeek/Qwen 关闭即可。')}
              >
                <Switch />
              </Form.Item>
            )}
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, cur) => prev.provider_type !== cur.provider_type}>
            {({ getFieldValue }) => getFieldValue('provider_type') === 'chat' && (
              <Form.Item
                label={t('支持读图（多模态）')}
                name="supports_vision"
                valuePropName="checked"
                tooltip={t('该模型本身能直接看图（如 qwen-vl / GLM-4V / gpt-4o / gemini）。开启后图片会原样送给模型；关闭时，若已为「图像理解（视觉桥）」角色指派了多模态模型，图片会先被转写成文字证据再送入。纯文本模型（DeepSeek、Qwen 文本版等）保持关闭。')}
              >
                <Switch />
              </Form.Item>
            )}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
