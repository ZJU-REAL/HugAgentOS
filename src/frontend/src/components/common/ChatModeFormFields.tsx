import { useState } from 'react';
import { Button, Form, InputNumber, Select, type FormInstance } from 'antd';

import { FormGrid, ToggleField } from './FormLayout';

import { MarketPickerModal, type MarketPickItem } from './MarketPickerModal';
import {
  CHAT_MODE_EFFORT_OPTIONS, type ChatModeCapabilities, type ChatModeOption, type ChatModeScope,
} from './chatModeForm';
import { t } from '../../i18n';

/**
 * Form fields shared by the two chat-mode editors: the user's private modes
 * (settings/MyChatModesPanel) and the official modes in the Config console
 * (config/ChatModesPanel). Both edit the same mode contract — tool surface,
 * capability lists, explicit invocation, code execution, iteration cap,
 * default effort — and differ only in wording and in which options endpoint
 * they read.
 */

type MarketField = 'skill_ids' | 'agent_ids' | 'plugin_ids';

const SCOPE_TEXT = {
  private: {
    allScope: '不收窄（和标准模式一样，用你已有的全部能力）',
    manualInvoke: '开启后，你本轮用 / 技能、@智能体、插件呼唤的能力仍可临时用上，不受上面清单限制。',
    codeExec: '开启后这个模式仍能执行代码、读写文件；关闭则纯检索问答，不执行代码。',
    maxIters: '留空表示不额外收紧。最小 2。',
    effort: '选中这个模式时，右侧的思考强度跟着切到这一档。',
    effortLocked: '锁定后这个模式下改不了强度。',
  },
  official: {
    allScope: '不收窄（跟随能力目录与用户自身授权）',
    manualInvoke: '开启后，用户本轮用 / 技能、@智能体、插件显式呼唤的能力可以临时装配进来，不受上面清单限制。',
    codeExec: '开启后这个模式仍带沙箱代码执行与文件读写工具（受系统配置的代码能力总开关约束）；关闭则纯检索问答，不执行代码——内置极速模式即如此。',
    maxIters: '这个模式的 ReAct 迭代硬上限，留空表示不额外收紧。最小 2——配成 1 会把回答那一轮也掐掉。',
    effort: '用户选中这个模式时，右侧的思考强度跟着切到这一档。',
    effortLocked: '锁定后用户改不了强度（极速模式即如此：那条链路本就不思考）。',
  },
} as const;

const MARKET_TITLE: Record<MarketField, string> = {
  skill_ids: '从市场选择技能',
  agent_ids: '从市场选择智能体',
  plugin_ids: '从市场选择插件',
};

/** Tool surface + capability lists + execution limits (rendered inside the editor's `<Form>`). */
export function ChatModeCapabilityFields({ form, scope, capabilities }: {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- antd FormInstance is untyped in both editors
  form: FormInstance<any>;
  scope: ChatModeScope;
  capabilities: ChatModeCapabilities;
}) {
  const text = SCOPE_TEXT[scope];
  const [marketPicker, setMarketPicker] = useState<MarketField | null>(null);
  // Display names of picked market items: their ids are `market:slug`, and without
  // a matching option the Select would only show the bare id.
  const [marketLabels, setMarketLabels] = useState<Record<string, string>>({});

  const restricted = Form.useWatch('tool_scope', form) === 'restricted';
  const watched: Record<MarketField, string[]> = {
    skill_ids: Form.useWatch('skill_ids', form) || [],
    agent_ids: Form.useWatch('agent_ids', form) || [],
    plugin_ids: Form.useWatch('plugin_ids', form) || [],
  };
  const market: Record<MarketField, MarketPickItem[]> = {
    skill_ids: capabilities.marketSkills,
    agent_ids: capabilities.marketAgents,
    plugin_ids: capabilities.marketPlugins,
  };

  const withMarketPicks = (base: ChatModeOption[], ids: string[]): ChatModeOption[] => [
    ...base,
    ...ids.filter((id) => id.startsWith('market:') && !base.some((o) => o.value === id))
      .map((id) => ({ value: id, label: marketLabels[id] || id.slice('market:'.length) })),
  ];
  const addFromMarket = (field: MarketField, picked: MarketPickItem[]) => {
    if (!picked.length) return;
    setMarketLabels((m) => ({
      ...m,
      ...Object.fromEntries(picked.map((i) => [i.id, t('{name}（市场）', { name: i.name || i.id })])),
    }));
    const cur: string[] = form.getFieldValue(field) || [];
    form.setFieldValue(field, Array.from(new Set([...cur, ...picked.map((i) => i.id)])));
  };

  const capabilitySelect = (options: ChatModeOption[], ids: string[] | null, placeholder: string) => (
    <Select
      mode={options.length ? 'multiple' : 'tags'}
      options={ids ? withMarketPicks(options, ids) : options}
      optionFilterProp="label"
      allowClear
      placeholder={placeholder}
    />
  );
  const marketLink = (field: MarketField, label: string) => (
    <Button
      type="link"
      size="small"
      className="jx-chatModeForm-marketLink"
      onClick={(e) => { e.preventDefault(); setMarketPicker(field); }}
    >
      {label}
    </Button>
  );
  const labelWithMarket = (label: string, field: MarketField, linkLabel: string) => (
    <span className="jx-chatModeForm-label">{label}{marketLink(field, linkLabel)}</span>
  );

  return (
    <>
      <FormGrid cols={3}>
        <Form.Item name="tool_scope" label={t('工具面')} className="jx-formGrid-span2">
          <Select
            options={[
              { value: 'all', label: t(text.allScope) },
              { value: 'restricted', label: t('只装配下面列出的能力') },
            ]}
          />
        </Form.Item>
        <Form.Item name="default_effort" label={t('默认思考强度')} tooltip={t(text.effort)}>
          <Select options={CHAT_MODE_EFFORT_OPTIONS.map((o) => ({ ...o, label: t(o.label) }))} />
        </Form.Item>
        {restricted && (
          <>
            <Form.Item name="mcp_server_ids" label={t('连接器')} tooltip={t('市场里的连接器需要配置凭据或授权，请先在能力中心安装，装好后会出现在此清单。')}>
              {capabilitySelect(capabilities.mcp, null, t('选择这个模式装配的连接器'))}
            </Form.Item>
            <Form.Item name="skill_ids" label={labelWithMarket(t('技能'), 'skill_ids', t('从市场选择'))}>
              {capabilitySelect(capabilities.skills, watched.skill_ids, t('选择这个模式装配的技能'))}
            </Form.Item>
            <Form.Item
              name="agent_ids"
              label={labelWithMarket(t('智能体'), 'agent_ids', t('从市场选择'))}
              tooltip={t('这个模式下可被委派的智能体；留空则只有本轮被 @ 呼唤的那个能入场。')}
            >
              {capabilitySelect(capabilities.agents, watched.agent_ids, t('选择这个模式可用的智能体'))}
            </Form.Item>
            <Form.Item
              name="plugin_ids"
              label={labelWithMarket(t('插件'), 'plugin_ids', t('从市场选择'))}
              tooltip={t('需要密钥的插件请先在能力中心安装，装好后出现在此清单。')}
            >
              {capabilitySelect(capabilities.plugins, watched.plugin_ids, t('选择这个模式装配的插件'))}
            </Form.Item>
            <Form.Item name="max_iters" label={t('迭代上限')} tooltip={t(text.maxIters)}>
              <InputNumber min={2} style={{ width: '100%' }} placeholder={t('不限制')} />
            </Form.Item>
          </>
        )}
      </FormGrid>
      <FormGrid cols={3}>
        {restricted && (
          <>
            <ToggleField name="manual_invoke_enabled" label={t('允许显式呼唤')} tooltip={t(text.manualInvoke)} />
            <ToggleField name="code_exec_enabled" label={t('保留代码执行')} tooltip={t(text.codeExec)} />
          </>
        )}
        <ToggleField name="effort_locked" label={t('锁定思考强度')} tooltip={t(text.effortLocked)} />
      </FormGrid>

      <MarketPickerModal
        open={marketPicker !== null}
        title={t(MARKET_TITLE[marketPicker ?? 'plugin_ids'])}
        items={market[marketPicker ?? 'plugin_ids']}
        selected={watched[marketPicker ?? 'plugin_ids']}
        onClose={() => setMarketPicker(null)}
        onAdd={(picked) => addFromMarket(marketPicker ?? 'plugin_ids', picked)}
      />
    </>
  );
}
