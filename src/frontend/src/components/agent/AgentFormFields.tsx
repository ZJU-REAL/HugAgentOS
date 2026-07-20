import { ShopOutlined } from '@ant-design/icons';
import { Button, Divider, Form, Input, InputNumber, Select, Spin, Switch } from 'antd';
import { useCallback, useState, type ReactNode } from 'react';
import {
  getMarketplaceSkillDetail,
  getMarketplaceSkills,
  installMarketplaceSkill,
} from '../../api';
import type { MarketplaceFetchers } from '../../types';
import { useAuthStore } from '../../stores';
import { useAgentStore, type AvailableResources } from '../../stores/agentStore';
import { PluginMarketplaceModal } from '../catalog/PluginMarketplaceModal';
import { SkillMarketplaceModal } from '../catalog/SkillMarketplaceModal';
import { OntologyTagSelect } from '../common/OntologyTagSelect';
import { t } from '../../i18n';

const { TextArea } = Input;

interface Option { label: string; value: string }

/**
 * A multi-select dropdown + "select all / clear" shortcut. Placed as a direct child of Form.Item --
 * Form.Item injects value/onChange, which we forward to Select, and we use the same onChange at the top
 * of the dropdown to select-all/clear in one click, avoiding item-by-item clicking. maxTagCount=responsive
 * prevents the tags from overflowing the form after selecting all.
 */
function SelectAllMultiple({
  value,
  onChange,
  options,
  placeholder,
  notFoundContent,
}: {
  value?: string[];
  onChange?: (v: string[]) => void;
  options: Option[];
  placeholder?: string;
  notFoundContent?: ReactNode;
}) {
  const allValues = options.map((o) => o.value);
  const selectedCount = value?.length ?? 0;
  const allSelected = allValues.length > 0 && selectedCount >= allValues.length;
  return (
    <Select
      mode="multiple"
      placeholder={placeholder}
      options={options}
      value={value}
      onChange={onChange}
      allowClear
      notFoundContent={notFoundContent}
      optionFilterProp="label"
      maxTagCount="responsive"
      popupRender={(menu) => (
        <>
          {allValues.length > 0 && (
            <>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '4px 12px',
                }}
                // Prevent mousedown so the dropdown doesn't collapse when clicking "select all"
                onMouseDown={(e) => e.preventDefault()}
              >
                <a onClick={() => onChange?.(allSelected ? [] : allValues)}>
                  {allSelected ? t('清空') : t('全选')}
                </a>
                <span style={{ color: '#999', fontSize: 12 }}>
                  {t('已选 {n} 项', { n: selectedCount })}
                </span>
              </div>
              <Divider style={{ margin: '4px 0' }} />
            </>
          )}
          {menu}
        </>
      )}
    />
  );
}

interface AgentFormFieldsProps {
  availableResources: AvailableResources | null;
}

const SKILL_MARKETPLACE_FETCHERS: MarketplaceFetchers = {
  loadList: () => getMarketplaceSkills(),
  loadDetail: (slug) => getMarketplaceSkillDetail(slug),
  install: (slug, secrets) => installMarketplaceSkill(slug, secrets),
};

export function AgentFormFields({ availableResources }: AgentFormFieldsProps) {
  const form = Form.useFormInstance();
  const fetchAvailableResources = useAgentStore((state) => state.fetchAvailableResources);
  const canAddSkill = useAuthStore((state) => state.authUser?.can_add_skill === true);
  const canImportPlugin = useAuthStore((state) => state.authUser?.can_import_plugin === true);
  const [skillMarketOpen, setSkillMarketOpen] = useState(false);
  const [pluginMarketOpen, setPluginMarketOpen] = useState(false);

  const mcpOptions = (availableResources?.mcp_servers || []).map((s) => ({
    label: s.enabled ? s.name : `${s.name}${t('（未启用）')}`,
    value: s.id,
  }));
  const skillOptions = (availableResources?.skills || []).map((s) => ({
    label: s.name,
    value: s.id,
  }));
  const pluginOptions = (availableResources?.plugins || []).map((p) => ({
    label: `${p.name}（${p.skill_count} 技能 · ${p.mcp_count} 工具）`,
    value: p.id,
  }));

  const bindInstalledResource = useCallback(async (
    field: 'skill_ids' | 'plugin_ids',
    resourceId: string,
  ) => {
    await fetchAvailableResources();
    const current = form.getFieldValue(field);
    const selected = Array.isArray(current) ? current : [];
    form.setFieldValue(field, Array.from(new Set([...selected, resourceId])));
  }, [fetchAvailableResources, form]);

  return (
    <>
      <Form.Item
        name="name"
        label={t('名称')}
        rules={[{ required: true, message: t('请输入智能体名称') }]}
      >
        <Input placeholder={t('如：产业链分析师')} maxLength={50} />
      </Form.Item>

      <Form.Item name="description" label={t('简介')}>
        <Input
          placeholder={t('一句话描述智能体的用途，限 20 字')}
          maxLength={20}
          showCount
        />
      </Form.Item>

      <Form.Item
        name="system_prompt"
        label={t('角色设定 (System Prompt)')}
        rules={[{ required: true, message: t('请输入角色设定') }]}
      >
        <TextArea
          rows={5}
          placeholder={t('定义智能体的角色、专长和行为规范...')}
          maxLength={5000}
          showCount
        />
      </Form.Item>

      <Form.Item name="welcome_message" label={t('开场白')}>
        <TextArea
          rows={2}
          placeholder={t('用户打开对话时的欢迎消息')}
          maxLength={500}
        />
      </Form.Item>

      <Form.Item
        name="mcp_server_ids"
        label={t('绑定工具 (MCP)')}
        tooltip={t('可绑定当前未启用的 MCP；它只会对该子智能体生效，不会同时启用到主智能体')}
      >
        {availableResources ? (
          <SelectAllMultiple
            placeholder={t('选择可用的 MCP 工具')}
            options={mcpOptions}
          />
        ) : (
          <Spin size="small" />
        )}
      </Form.Item>

      <Form.Item
        name="skill_ids"
        label={t('绑定技能')}
        extra={canAddSkill ? (
          <Button
            type="link"
            size="small"
            icon={<ShopOutlined />}
            style={{ paddingInline: 0 }}
            onClick={() => setSkillMarketOpen(true)}
          >
            {t('从技能市场安装并绑定')}
          </Button>
        ) : undefined}
      >
        {availableResources ? (
          <SelectAllMultiple
            placeholder={t('选择可用的技能')}
            options={skillOptions}
          />
        ) : (
          <Spin size="small" />
        )}
      </Form.Item>

      <Form.Item
        name="plugin_ids"
        label={t('绑定插件')}
        tooltip={t('插件是「技能+工具」的能力包，绑定后其全部技能与工具一并对该智能体生效')}
        extra={canImportPlugin ? (
          <Button
            type="link"
            size="small"
            icon={<ShopOutlined />}
            style={{ paddingInline: 0 }}
            onClick={() => setPluginMarketOpen(true)}
          >
            {t('从插件市场安装并绑定')}
          </Button>
        ) : undefined}
      >
        {availableResources ? (
          <SelectAllMultiple
            placeholder={t('选择要绑定的插件')}
            options={pluginOptions}
            notFoundContent={t('暂无已安装插件')}
          />
        ) : (
          <Spin size="small" />
        )}
      </Form.Item>

      <Form.Item
        name="ontology_tags"
        label={t('本体治理标签')}
        tooltip={t('标签来自当前激活领域包；实际调用子智能体时，会触发标签关联的本体工作流和评审级别。')}
      >
        <OntologyTagSelect
          options={availableResources?.ontology_tags ?? []}
          loading={!availableResources}
        />
      </Form.Item>

      <Form.Item name="max_iters" label={t('最大推理轮次')}>
        <InputNumber min={1} max={30} style={{ width: '100%' }} />
      </Form.Item>

      <Form.Item
        name="temperature"
        label={t('温度 (Temperature)')}
        tooltip={t('控制生成结果的随机性；值越低越确定，越高越发散。范围 0–2，默认 0.6')}
      >
        <InputNumber
          min={0}
          max={2}
          step={0.1}
          placeholder="0.6"
          style={{ width: '100%' }}
        />
      </Form.Item>

      <Form.Item
        name="shared_context"
        label={t('共享上下文')}
        valuePropName="checked"
        tooltip={t('启用后，被主智能体调用时可读取完整对话历史和工具调用结果')}
      >
        <Switch />
      </Form.Item>

      <SkillMarketplaceModal
        open={skillMarketOpen}
        onClose={() => setSkillMarketOpen(false)}
        fetchers={SKILL_MARKETPLACE_FETCHERS}
        scopeLabel={t('安装后将自动绑定到当前子智能体')}
        onInstalled={(result) => {
          void bindInstalledResource('skill_ids', result.id);
        }}
      />

      <PluginMarketplaceModal
        open={pluginMarketOpen}
        onClose={() => setPluginMarketOpen(false)}
        scopeLabel={t('安装后将自动绑定到当前子智能体')}
        onInstalled={(result) => {
          void bindInstalledResource('plugin_ids', result.install_id);
        }}
      />
    </>
  );
}
