import { useCallback, useEffect, useState } from 'react';
import {
  Button, Empty, Form, Input, InputNumber, Modal, Select, Space, Spin, Switch, Tag, Typography, message,
} from 'antd';
import { PlusOutlined, DeleteOutlined, EditOutlined } from '@ant-design/icons';
import {
  createMyChatMode, deleteMyChatMode, listMyChatModes, updateMyChatMode,
  type ChatModeDetail,
} from '../../api';
import { useChatModeStore } from '../../stores';
import { API_BASE } from '../../utils/adminApi';
import { MarketPickerModal, type MarketPickItem } from '../common/MarketPickerModal';
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

type Option = { value: string; label: string };

interface CapabilityItem { id: string; name?: string; description?: string; disabled_globally?: boolean }
interface CapabilityOptions {
  mcp?: CapabilityItem[];
  skills?: CapabilityItem[];
  plugins?: CapabilityItem[];
  agents?: CapabilityItem[];
  market_skills?: CapabilityItem[];
  market_agents?: CapabilityItem[];
  market_plugins?: CapabilityItem[];
  prompt_kinds?: Array<{ key: string; label: string; builtin: boolean }>;
}

const toOpt = (r: CapabilityItem): Option => ({
  value: r.id,
  label: `${r.name || r.id}${r.disabled_globally ? '（全局已停用）' : ''}`,
});

const EFFORT_OPTIONS = [
  { value: 'fast', label: '快速（不思考）' },
  { value: 'medium', label: '思考·中' },
  { value: 'high', label: '思考·高' },
  { value: 'max', label: '思考·超高' },
];

export function ChatModesPanel() {
  const [rows, setRows] = useState<ChatModeDetail[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<ChatModeDetail | null>(null);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm();
  const refreshModes = useChatModeStore((s) => s.fetchModes);

  // 候选能力走模式自己的清单端点（/options/mine）：**全量**，不按启停过滤——在模式里
  // 勾一个当前对自己关掉的技能，意思就是"只在这个模式里用"。市场里没装的也一并列出，
  // 保存时后端按需安装到本人名下。
  const [mcpOptions, setMcpOptions] = useState<Option[]>([]);
  const [skillOptions, setSkillOptions] = useState<Option[]>([]);
  const [pluginOptions, setPluginOptions] = useState<Option[]>([]);
  const [agentOptions, setAgentOptions] = useState<Option[]>([]);
  // 市场清单不进下拉（几百上千项灌进 Select 滚不动也搜不清）——单独存，走弹窗挑选
  const [marketSkills, setMarketSkills] = useState<MarketPickItem[]>([]);
  const [marketAgents, setMarketAgents] = useState<MarketPickItem[]>([]);
  const [marketPlugins, setMarketPlugins] = useState<MarketPickItem[]>([]);
  const [marketPicker, setMarketPicker] = useState<null | 'skill_ids' | 'agent_ids' | 'plugin_ids'>(null);
  // 挑过的市场项的显示名（id 是 market:slug，Select 里没有对应 option 就只会显示裸 id）
  const [marketLabels, setMarketLabels] = useState<Record<string, string>>({});

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
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/v1/chat-modes/options/mine`, { credentials: 'include' });
        const d = ((await res.json())?.data || {}) as CapabilityOptions;
        setMcpOptions((d.mcp || []).map(toOpt));
        setPluginOptions((d.plugins || []).map(toOpt));
        setSkillOptions((d.skills || []).map(toOpt));
        setAgentOptions((d.agents || []).map(toOpt));
        setMarketSkills((d.market_skills || []).map((i) => ({ id: i.id, name: i.name, description: i.description })));
        setMarketAgents((d.market_agents || []).map((i) => ({ id: i.id, name: i.name, description: i.description })));
        setMarketPlugins((d.market_plugins || []).map((i) => ({ id: i.id, name: i.name, description: i.description })));
      } catch { /* 拿不到就退化成自由输入 */ }
    })();
  }, []);

  const openEditor = (row: ChatModeDetail | null) => {
    setCreating(row === null);
    setEditing(row);
    form.setFieldsValue(row ?? {
      name: '',
      slug: '',
      description: '',
      enabled: true,
      tool_scope: 'restricted',
      mcp_server_ids: [],
      skill_ids: [],
      plugin_ids: [],
      agent_ids: [],
      manual_invoke_enabled: true,
      code_exec_enabled: false,
      max_iters: null,
      default_effort: 'fast',
      effort_locked: false,
      prompt_text: '',
    });
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

  const restricted = Form.useWatch('tool_scope', form) === 'restricted';
  const watchedSkillIds: string[] = Form.useWatch('skill_ids', form) || [];
  const watchedAgentIds: string[] = Form.useWatch('agent_ids', form) || [];
  const watchedPluginIds: string[] = Form.useWatch('plugin_ids', form) || [];
  /** 下拉 options = 已装项 + 已挑进表单的市场项（否则市场项只显示裸 market:slug） */
  const withMarketPicks = (base: Option[], ids: string[]): Option[] => [
    ...base,
    ...ids.filter((id) => id.startsWith('market:') && !base.some((o) => o.value === id))
      .map((id) => ({ value: id, label: marketLabels[id] || id.slice('market:'.length) })),
  ];
  const addFromMarket = (field: 'skill_ids' | 'agent_ids' | 'plugin_ids') => (picked: MarketPickItem[]) => {
    if (!picked.length) return;
    setMarketLabels((m) => ({
      ...m,
      ...Object.fromEntries(picked.map((i) => [i.id, `${i.name || i.id}（市场）`])),
    }));
    const cur: string[] = form.getFieldValue(field) || [];
    form.setFieldValue(field, Array.from(new Set([...cur, ...picked.map((i) => i.id)])));
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
                  <Tag>{t(EFFORT_OPTIONS.find(o => o.value === row.default_effort)?.label || row.default_effort)}</Tag>
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
        width={640}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item name="name" label={t('模式名称')} rules={[{ required: true, message: t('请填写模式名称') }]}>
            <Input placeholder={t('例如：我的研报模式')} maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label={t('一句话说明')} extra={t('会显示在对话框上方的模式下拉里，提醒自己这个模式是干嘛的。')}>
            <Input placeholder={t('例如：只带检索，输出带出处的要点清单')} maxLength={500} />
          </Form.Item>

          <Form.Item name="tool_scope" label={t('工具面')}>
            <Select
              options={[
                { value: 'all', label: t('不收窄（和标准模式一样，用你已有的全部能力）') },
                { value: 'restricted', label: t('只装配下面列出的能力') },
              ]}
            />
          </Form.Item>
          {restricted && (
            <>
              <Form.Item name="mcp_server_ids" label={t('MCP 工具')} extra={t('市场里的 MCP 工具需要配置凭据或授权，请先在能力中心安装，装好后会出现在此清单。')}>
                <Select
                  mode={mcpOptions.length ? 'multiple' : 'tags'}
                  options={mcpOptions}
                  optionFilterProp="label"
                  allowClear
                  placeholder={t('选择这个模式装配的 MCP 工具')}
                />
              </Form.Item>
              <Form.Item
                name="skill_ids"
                label={t('技能')}
                extra={(
                  <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setMarketPicker('skill_ids')}>
                    {t('从市场选择技能…')}
                  </Button>
                )}
              >
                <Select
                  mode={skillOptions.length ? 'multiple' : 'tags'}
                  options={withMarketPicks(skillOptions, watchedSkillIds)}
                  optionFilterProp="label"
                  allowClear
                  placeholder={t('选择这个模式装配的技能')}
                />
              </Form.Item>
              <Form.Item
                name="agent_ids"
                label={t('子智能体')}
                extra={(
                  <>
                    {t('这个模式下可被委派的子智能体；留空则只有本轮被 @ 呼唤的那个能入场。')}
                    <Button type="link" size="small" style={{ padding: '0 0 0 6px' }} onClick={() => setMarketPicker('agent_ids')}>
                      {t('从市场选择子智能体…')}
                    </Button>
                  </>
                )}
              >
                <Select
                  mode={agentOptions.length ? 'multiple' : 'tags'}
                  options={withMarketPicks(agentOptions, watchedAgentIds)}
                  optionFilterProp="label"
                  allowClear
                  placeholder={t('选择这个模式可用的子智能体')}
                />
              </Form.Item>
              <Form.Item
                name="plugin_ids"
                label={t('插件')}
                extra={(
                  <>
                    {t('需要密钥的插件请先在能力中心安装，装好后出现在此清单。')}
                    <Button type="link" size="small" style={{ padding: '0 0 0 6px' }} onClick={() => setMarketPicker('plugin_ids')}>
                      {t('从市场选择插件…')}
                    </Button>
                  </>
                )}
              >
                <Select
                  mode={pluginOptions.length ? 'multiple' : 'tags'}
                  options={withMarketPicks(pluginOptions, watchedPluginIds)}
                  optionFilterProp="label"
                  allowClear
                  placeholder={t('选择这个模式装配的插件')}
                />
              </Form.Item>
              <Form.Item name="manual_invoke_enabled" label={t('允许显式呼唤')} valuePropName="checked" extra={t('开启后，你本轮用 / 技能、@子智能体、插件呼唤的能力仍可临时用上，不受上面清单限制。')}>
                <Switch />
              </Form.Item>
              <Form.Item
                name="code_exec_enabled"
                label={t('保留代码执行')}
                valuePropName="checked"
                extra={t('开启后这个模式仍能执行代码、读写文件；关闭则纯检索问答，不执行代码。')}
              >
                <Switch />
              </Form.Item>
              <Form.Item name="max_iters" label={t('迭代上限')} extra={t('留空表示不额外收紧。最小 2。')}>
                <InputNumber min={2} style={{ width: 180 }} placeholder={t('不限制')} />
              </Form.Item>
            </>
          )}

          <Form.Item name="default_effort" label={t('默认思考强度')} extra={t('选中这个模式时，右侧的思考强度跟着切到这一档。')}>
            <Select options={EFFORT_OPTIONS.map(o => ({ ...o, label: t(o.label) }))} style={{ width: 220 }} />
          </Form.Item>
          <Form.Item name="effort_locked" label={t('锁定思考强度')} valuePropName="checked" extra={t('锁定后这个模式下改不了强度。')}>
            <Switch />
          </Form.Item>

          {/* 私有模式只有手写正文：分类是管理端「提示词管理」的东西，普通用户看不到
              那边的内容，给个分类下拉等于让人绑一个自己看不见的黑盒。后端同样拒收。 */}
          <Form.Item
            name="prompt_text"
            label={t('专属提示词')}
            extra={t('这个模式下替换掉默认的系统提示词。留空则沿用默认装配，只按上面的工具面收窄。')}
          >
            <Input.TextArea rows={5} placeholder={t('例如：你是研报助手。先检索再作答，输出结构化要点并标注出处。')} />
          </Form.Item>

          <Form.Item name="enabled" label={t('启用')} valuePropName="checked" extra={t('停用后它不出现在模式下拉里，但配置留着。')}>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <MarketPickerModal
        open={marketPicker !== null}
        title={
          marketPicker === 'skill_ids' ? t('从市场选择技能')
            : marketPicker === 'agent_ids' ? t('从市场选择子智能体')
              : t('从市场选择插件')
        }
        items={
          marketPicker === 'skill_ids' ? marketSkills
            : marketPicker === 'agent_ids' ? marketAgents
              : marketPlugins
        }
        selected={
          marketPicker === 'skill_ids' ? watchedSkillIds
            : marketPicker === 'agent_ids' ? watchedAgentIds
              : watchedPluginIds
        }
        onClose={() => setMarketPicker(null)}
        onAdd={addFromMarket(marketPicker ?? 'plugin_ids')}
      />
    </div>
  );
}

export default ChatModesPanel;
