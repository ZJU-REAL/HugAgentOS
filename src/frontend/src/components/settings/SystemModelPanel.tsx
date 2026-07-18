import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Typography, message,
} from 'antd';
import { PlusOutlined, ThunderboltOutlined } from '@ant-design/icons';
import {
  assignModelRole,
  createModelProvider,
  deleteModelProvider,
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
  const [form] = Form.useForm();

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [ps, rs] = await Promise.all([listModelProviders(), listModelRoles()]);
      setProviders(ps);
      setRoles(rs);
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
    form.resetFields();
    form.setFieldsValue({ provider: 'openai_compatible', provider_type: 'chat', is_active: true });
    setEditorOpen(true);
  };

  const openEdit = (p: ModelProviderItem) => {
    setEditing(p);
    form.setFieldsValue({
      display_name: p.display_name,
      provider: p.provider,
      provider_type: p.provider_type,
      base_url: p.base_url,
      api_key: '', // leave empty = no change (the masked value is never filled back)
      model_name: p.model_name,
      is_active: p.is_active,
      context_length: (p.extra_config?.context_length as number | undefined) ?? undefined,
    });
    setEditorOpen(true);
  };

  const handleSave = async () => {
    const values = await form.validateFields();
    const extra: Record<string, unknown> = { ...(editing?.extra_config ?? {}) };
    if (values.context_length) extra.context_length = Number(values.context_length);
    else delete extra.context_length;
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
          <Text>{(r.label as string) || v}</Text>
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
            .filter((p) => !r.type || p.provider_type === r.type)
            .map((p) => ({ value: p.provider_id, label: `${p.display_name}（${p.model_name}）` }))}
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
            <Form.Item name="context_length" label={t('上下文窗口（token，可选）')} style={{ flex: 1 }}>
              <Input type="number" placeholder="131072" />
            </Form.Item>
            <Form.Item name="is_active" label={t('启用')} valuePropName="checked" style={{ width: 90, marginLeft: 8 }}>
              <Switch />
            </Form.Item>
          </Space.Compact>
        </Form>
      </Modal>
    </div>
  );
}
