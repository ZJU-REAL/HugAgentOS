import { CheckCircleOutlined, ForkOutlined } from '@ant-design/icons';
import { Empty, Select, Space, Tag, Tooltip, Typography } from 'antd';
import { useMemo } from 'react';
import type { OntologyTagOption } from '../../types';
import { t } from '../../i18n';

const { Text } = Typography;

interface OntologyTagSelectProps {
  options: OntologyTagOption[];
  loading?: boolean;
  value?: string[];
  onChange?: (value: string[]) => void;
}

function reviewColor(level: string): string {
  if (level === 'committee') return 'red';
  if (level === 'checkpoint') return 'orange';
  if (level === 'gate') return 'gold';
  return 'blue';
}

/**
 * Controlled ontology selector. It deliberately uses `multiple` instead of `tags`:
 * users can only choose values that an active Domain Pack has wired to a workflow.
 */
export function OntologyTagSelect({ options, loading, value, onChange }: OntologyTagSelectProps) {
  const selectedOptions = useMemo(() => {
    const selected = new Set(value ?? []);
    return options.filter((item) => selected.has(item.value));
  }, [options, value]);

  const workflows = useMemo(() => {
    const result = new Map<string, OntologyTagOption['workflows'][number]>();
    selectedOptions.forEach((item) => {
      item.workflows.forEach((workflow) => result.set(workflow.workflow_ref, workflow));
    });
    return Array.from(result.values());
  }, [selectedOptions]);

  const labels = useMemo(
    () => new Map(options.map((item) => [item.value, item.concept_name])),
    [options],
  );

  return (
    <div className="jx-ontologyTagSelect">
      <Select
        mode="multiple"
        allowClear
        showSearch
        loading={loading}
        value={value}
        onChange={onChange}
        placeholder={t('选择领域包预置的本体标签')}
        optionFilterProp="label"
        maxTagCount="responsive"
        options={options.map((item) => ({
          value: item.value,
          label: `${item.concept_name} ${item.value} ${item.definition}`,
          item,
        }))}
        labelRender={({ value: selectedValue }) => (
          <Tooltip title={selectedValue}>
            <span>{labels.get(String(selectedValue)) ?? selectedValue}</span>
          </Tooltip>
        )}
        optionRender={(option) => {
          const item = option.data.item as OntologyTagOption;
          return (
            <div className="jx-ontologyTagSelect-option">
              <div className="jx-ontologyTagSelect-optionHead">
                <Text strong>{item.concept_name}</Text>
                <Text code>{item.value}</Text>
              </div>
              <Text type="secondary" className="jx-ontologyTagSelect-definition">
                {item.definition}
              </Text>
              <Space size={[5, 5]} wrap>
                {item.packs.map((pack) => (
                  <Tag bordered={false} key={`${pack.pack_id}:${pack.version}`}>
                    {pack.pack_name} · v{pack.version}
                  </Tag>
                ))}
                {item.workflows.map((workflow) => (
                  <Tag
                    bordered={false}
                    color={reviewColor(workflow.review_level)}
                    key={workflow.workflow_ref}
                  >
                    {workflow.workflow_name} · {t('{level} 评审', { level: workflow.review_level })}
                  </Tag>
                ))}
              </Space>
            </div>
          );
        }}
        notFoundContent={loading ? undefined : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t('当前激活领域包没有为此资产配置可选标签')}
          />
        )}
      />

      {workflows.length > 0 && (
        <div className="jx-ontologyTagSelect-triggered">
          <Text type="secondary"><ForkOutlined /> {t('运行时将触发')}</Text>
          <Space size={[5, 5]} wrap>
            {workflows.map((workflow) => (
              <Tag
                icon={<CheckCircleOutlined />}
                color={reviewColor(workflow.review_level)}
                key={workflow.workflow_ref}
              >
                {workflow.workflow_name} · {t('{level} 评审', { level: workflow.review_level })}
              </Tag>
            ))}
          </Space>
        </div>
      )}
    </div>
  );
}
