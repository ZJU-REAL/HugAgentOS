import { Button, Empty, Select, Tag } from 'antd';
import { AppstoreOutlined, ApiOutlined, RobotOutlined, ShopOutlined, ToolOutlined } from '@ant-design/icons';
import type { EnvironmentField, EnvironmentOptions, EnvironmentPreview } from '../../api/projectEnvironment';
import { t } from '../../i18n';

const resources = {
  agent_ids: { label: '子智能体', icon: <RobotOutlined /> },
  plugin_ids: { label: '插件', icon: <AppstoreOutlined /> },
  skill_ids: { label: '技能', icon: <ToolOutlined /> },
  mcp_ids: { label: '连接器', icon: <ApiOutlined /> },
};

export default function ProjectEnvironmentResourcePicker({ field, value, options, dependencies, onChange, onMarket }: {
  field: EnvironmentField;
  value: string[];
  options?: EnvironmentOptions[EnvironmentField];
  dependencies: EnvironmentPreview['dependencies'];
  onChange: (ids: string[]) => void;
  onMarket: () => void;
}) {
  const resource = resources[field];
  const required = dependencies.filter(item => item.kind === field).map(item => item.id);
  // Inferred dependencies remain derived, so removing the last parent removes
  // them from the picker without disturbing explicitly selected resources.
  const inferred = new Set(required.filter(id => !value.includes(id)));
  const selected = [...new Set([...value, ...required])];
  return <div className="jx-projectEnvironment-resource">
    <div className="jx-projectEnvironment-label">
      <label htmlFor={`project-env-${field}`}>{resource.icon} {t(resource.label)}</label>
      <Button type="link" size="small" icon={<ShopOutlined />} onClick={onMarket}>
        {t('从市场获取')}
      </Button>
    </div>
    <Select id={`project-env-${field}`} aria-label={t(resource.label)} mode="multiple"
      style={{ width: '100%' }} value={selected} allowClear
      placeholder={t('选择项目可用的{kind}', { kind: t(resource.label) })}
      options={options?.map(item => ({
        value: item.id, label: item.name, disabled: inferred.has(item.id),
      }))}
      tagRender={({ value: id, label, closable, onClose }) =>
        <Tag closable={closable && !inferred.has(String(id))} onClose={onClose}
          onMouseDown={event => { event.preventDefault(); event.stopPropagation(); }}>
          {label}
        </Tag>}
      onChange={ids => onChange(ids.filter(id => !inferred.has(id)))}
      optionFilterProp="label" maxTagCount="responsive"
      notFoundContent={<Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={t('暂无可绑定资源，可从市场获取')} />}
    />
  </div>;
}
