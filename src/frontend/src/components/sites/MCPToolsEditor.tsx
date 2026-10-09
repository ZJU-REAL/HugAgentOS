import { Alert, Button, Form, Input, Modal, Select, Space } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import type { Application, MCPToolDefinition } from './applicationApi';
import { isMcpField } from './mcpToolFields';
import { t } from '../../i18n';

const identifier = /^[a-z][a-z0-9_]{0,47}$/;
const reserved = new Set(['id', 'version', 'created_at', '_request_key']);

/** Edit a complete tool set; opening an existing MCP never replaces it with a sample. */
export function MCPToolsEditor({ app, busy, onClose, onSave }: {
  app: Application; busy: boolean; onClose: () => void;
  onSave: (tools: MCPToolDefinition[]) => Promise<void>;
}) {
  const [form] = Form.useForm<{ tools: MCPToolDefinition[] }>();
  const tools = Form.useWatch('tools', form) as MCPToolDefinition[] | undefined;
  const tableNames = Object.keys(app.tables);
  const newTool = (): MCPToolDefinition => {
    const table = tableNames[0] || '';
    return { name: '', description: '', table, fields: app.tables[table]?.columns.filter((column) => isMcpField(column.name)).map((column) => column.name) || [], filters: [] };
  };
  return <Modal open width={680} title={t('定义 MCP 工具')} okText={t('保存')} cancelText={t('取消')}
    confirmLoading={busy} closable={!busy} maskClosable={!busy} cancelButtonProps={{ disabled: busy }}
    onCancel={onClose} onOk={() => form.submit()} wrapClassName="jx-sites-manageModal">
    <Alert type="info" showIcon message={t('MCP 只提供已定义字段的查询；凭据更新后旧凭据立即失效')} />
    <Form form={form} name="mcp-tools" layout="vertical" disabled={busy}
      initialValues={{ tools: app.tools.length ? app.tools : [newTool()] }}
      onFinish={(values) => void onSave(values.tools)}>
      <Form.List name="tools" rules={[{
        validator: async (_, values: MCPToolDefinition[]) => {
          if (!values?.length || values.length > 30) throw new Error(t('请保留 1 至 30 个 MCP 工具'));
          if (new Set(values.map((tool) => tool.name)).size !== values.length) throw new Error(t('工具名称不能重复'));
        },
      }]}>
        {(fields, { add, remove }, { errors }) => <>
          {fields.map((field, index) => {
            const columns = (app.tables[tools?.[field.name]?.table || '']?.columns || []).filter((column) => isMcpField(column.name));
            return <div className="jx-sites-toolEditor" key={field.key}>
              <Space className="jx-sites-toolEditorHead">
                <strong>{t('工具')} {index + 1}</strong>
                <Button type="text" danger icon={<DeleteOutlined />} disabled={busy || fields.length === 1}
                  aria-label={t('删除工具')} onClick={() => remove(field.name)}>{t('删除')}</Button>
              </Space>
              <Form.Item name={[field.name, 'name']} label={t('工具名称')} rules={[
                { required: true, message: t('请输入工具名称') },
                { pattern: identifier, message: t('工具名称须为小写字母开头，支持数字和下划线，最多 48 位') },
                { validator: async (_, value: string) => { if (reserved.has(value)) throw new Error(t('该名称为保留名称')); } },
              ]}><Input maxLength={48} /></Form.Item>
              <Form.Item name={[field.name, 'description']} label={t('工具说明')}
                rules={[{ required: true, whitespace: true, message: t('请输入工具说明') }]}>
                <Input.TextArea rows={2} maxLength={1000} />
              </Form.Item>
              <Form.Item name={[field.name, 'table']} label={t('选择数据表')} rules={[{ required: true }]}>
                <Select options={tableNames.map((name) => ({ value: name, label: name }))}
                  onChange={() => {
                    form.setFieldValue(['tools', field.name, 'fields'], []);
                    form.setFieldValue(['tools', field.name, 'filters'], []);
                  }} />
              </Form.Item>
              <Form.Item name={[field.name, 'fields']} label={t('返回字段')}
                rules={[{ required: true, type: 'array', min: 1, message: t('请至少选择一个返回字段') }]}>
                <Select mode="multiple" options={columns.map((column) => ({ value: column.name, label: column.name }))} />
              </Form.Item>
              <Form.Item name={[field.name, 'filters']} label={t('可用过滤字段')}>
                <Select mode="multiple" options={columns.filter((column) => column.type !== 'json')
                  .map((column) => ({ value: column.name, label: column.name }))} />
              </Form.Item>
            </div>;
          })}
          <Form.ErrorList errors={errors} />
          <Button block icon={<PlusOutlined />} disabled={busy || fields.length >= 30} onClick={() => add(newTool())}>
            {t('添加工具')}
          </Button>
        </>}
      </Form.List>
    </Form>
  </Modal>;
}
