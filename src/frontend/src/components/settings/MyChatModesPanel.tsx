import { useCallback, useEffect, useState } from 'react';
import {
  Button, Empty, Form, Input, Modal, Space, Spin, Tag, Typography, message,
} from 'antd';
import { PlusOutlined, DeleteOutlined, EditOutlined } from '@ant-design/icons';
import {
  apiRequest, createMyChatMode, deleteMyChatMode, listMyChatModes, unwrapData, updateMyChatMode,
  type ChatModeDetail,
} from '../../api';
import { useChatModeStore } from '../../stores';
import { ChatModeCapabilityFields } from '../common/ChatModeFormFields';
import { FormGrid, ToggleField } from '../common/FormLayout';
import { COMPACT_MODAL_PROPS } from '../common/modalLayout';
import {
  CHAT_MODE_DEFAULTS, chatModeEffortLabel, useChatModeCapabilities,
  type ChatModeCapabilityOptions,
} from '../common/chatModeForm';
import { t } from '../../i18n';

const { Text, Paragraph } = Typography;

/**
 * 设置中心「模式选择」：用户自建**私有**模式。
 *
 * 私有 = 只有本人能看见能用，也不进管理端的列表。可选能力与官方模式同一套清单
 * （/v1/chat-modes/options/mine），只是作用域收到本人：全量、不按启停过滤、含市场里
 * 还没装的项（选中后保存时自动装到本人名下）。
 *
 * 入口由 can_manage_chat_modes 权限位控制（在 Config「用户/团队/角色权限」里开）。
 */

export function MyChatModesPanel() {
  const [rows, setRows] = useState<ChatModeDetail[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<ChatModeDetail | null>(null);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm();
  const refreshModes = useChatModeStore((s) => s.fetchModes);

  const capabilities = useChatModeCapabilities(async () => {
    const wrapped = await apiRequest<unknown>('/v1/chat-modes/options/mine');
    return (unwrapData<ChatModeCapabilityOptions | null>(wrapped) || {}) as ChatModeCapabilityOptions;
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await listMyChatModes());
    } catch (e) {
      message.error(t('加载我的模式失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  const openEditor = (row: ChatModeDetail | null) => {
    setCreating(row === null);
    setEditing(row);
    form.setFieldsValue(row ?? CHAT_MODE_DEFAULTS);
  };

  const close = () => { setEditing(null); setCreating(false); };

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (creating) {
        await createMyChatMode(values);
        message.success(t('模式已创建'));
      } else if (editing) {
        await updateMyChatMode(editing.id, values);
        message.success(t('模式已保存'));
      }
      close();
      await load();
      // 让对话框上方那枚模式位立刻看到改动，不用刷新页面
      await refreshModes(true);
    } catch (e) {
      message.error(t('保存失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setSaving(false);
    }
  };

  const remove = (row: ChatModeDetail) => {
    Modal.confirm({
      title: t('删除模式「{name}」？', { name: row.name }),
      content: t('正在用这个模式的对话会回落到标准模式，历史记录不受影响。'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteMyChatMode(row.id);
          message.success(t('已删除'));
          await load();
          await refreshModes(true);
        } catch (e) {
          message.error(t('删除失败：{msg}', { msg: (e as Error).message }));
        }
      },
    });
  };

  return (
    <div className="jx-settings-section">
      <div className="jx-modeMine-head">
        <Paragraph type="secondary" style={{ fontSize: 13, margin: 0, flex: 1 }}>
          {t('模式决定一段对话能用哪些工具、技能与插件，配哪段专属提示词，以及默认思考多深。这里建的模式只有你自己能看见和使用。')}
        </Paragraph>
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => openEditor(null)}>
          {t('新建模式')}
        </Button>
      </div>

      {loading ? (
        <Spin />
      ) : rows.length === 0 ? (
        <Empty
          description={t('还没有自定义模式')}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      ) : (
        <div className="jx-modeMine-list">
          {rows.map((row) => (
            <div className="jx-modeMine-item" key={row.id}>
              <div className="jx-modeMine-body">
                <Space size={6}>
                  <Text strong>{row.name}</Text>
                  {!row.enabled && <Tag>{t('已停用')}</Tag>}
                  {row.effort_locked && <Tag color="orange">{t('锁定强度')}</Tag>}
                </Space>
                <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                  {row.description || t('（没有填写说明）')}
                </Text>
                <Space size={4} style={{ marginTop: 4 }} wrap>
                  <Tag>{row.tool_scope === 'all' ? t('不收窄') : t('MCP {n}', { n: row.mcp_server_ids.length })}</Tag>
                  {row.tool_scope === 'restricted' && <Tag>{t('技能 {n}', { n: row.skill_ids.length })}</Tag>}
                  {row.tool_scope === 'restricted' && <Tag>{t('插件 {n}', { n: row.plugin_ids.length })}</Tag>}
                  {row.tool_scope === 'restricted' && row.code_exec_enabled && <Tag color="geekblue">{t('代码执行')}</Tag>}
                  <Tag>{chatModeEffortLabel(row.default_effort)}</Tag>
                </Space>
              </div>
              <Space size={4}>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEditor(row)}>{t('编辑')}</Button>
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => remove(row)} />
              </Space>
            </div>
          ))}
        </div>
      )}

      <Modal
        open={editing !== null || creating}
        title={creating ? t('新建模式') : t('编辑模式「{name}」', { name: editing?.name || '' })}
        onCancel={close}
        onOk={submit}
        confirmLoading={saving}
        width={760}
        destroyOnHidden
        {...COMPACT_MODAL_PROPS}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <FormGrid cols={3}>
            <Form.Item name="name" label={t('模式名称')} rules={[{ required: true, message: t('请填写模式名称') }]}>
              <Input placeholder={t('例如：我的研报模式')} maxLength={100} />
            </Form.Item>
            <Form.Item name="description" label={t('一句话说明')} className="jx-formGrid-span2" tooltip={t('会显示在对话框上方的模式下拉里，提醒自己这个模式是干嘛的。')}>
              <Input placeholder={t('例如：只带检索，输出带出处的要点清单')} maxLength={500} />
            </Form.Item>
          </FormGrid>

          <ChatModeCapabilityFields form={form} scope="private" capabilities={capabilities} />

          {/* 私有模式只有手写正文：分类是管理端「提示词管理」的东西，普通用户看不到
              那边的内容，给个分类下拉等于让人绑一个自己看不见的黑盒。后端同样拒收。 */}
          <Form.Item
            name="prompt_text"
            label={t('专属提示词')}
            tooltip={t('这个模式下替换掉默认的系统提示词。留空则沿用默认装配，只按上面的工具面收窄。')}
          >
            <Input.TextArea rows={4} placeholder={t('例如：你是研报助手。先检索再作答，输出结构化要点并标注出处。')} />
          </Form.Item>

          <ToggleField name="enabled" label={t('启用')} description={t('停用后它不出现在模式下拉里，但配置留着。')} />
        </Form>
      </Modal>

    </div>
  );
}

export default MyChatModesPanel;
