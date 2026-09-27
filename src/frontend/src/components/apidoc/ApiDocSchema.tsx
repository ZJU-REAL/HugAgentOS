import { Collapse, Space, Table, Tag, Typography } from 'antd';
import { t } from '../../i18n';
import type { ApiComponents, JsonValue, SchemaDefinition } from './apiDocModel';
import { resolveRef, typeLabel } from './apiDocSchemaHelpers';
const { Text } = Typography;

interface SchemaTreeProps {
  schema: SchemaDefinition | undefined;
  components: ApiComponents;
  depth?: number;
  visited?: Set<string>;
}

export function SchemaTree({ schema, components, depth = 0, visited }: SchemaTreeProps) {
  const v = visited ?? new Set<string>();

  if (!schema) return <Text type="secondary">{t('无')}</Text>;

  if (depth > 4) return <Text type="secondary">{t('…（已折叠，超过 4 层嵌套）')}</Text>;

  // Empty schema {} —— common when FastAPI declares no response_model
  if (typeof schema === 'object' && !schema.$ref && Object.keys(schema).length === 0) {
    return (
      <Text type="secondary">
        任意类型（schema 未限定，通常意味着后端返回标准响应包络 <Text code>{'{ code, message, data, trace_id, timestamp }'}</Text>）
      </Text>
    );
  }

  if (schema.$ref) {
    if (v.has(schema.$ref)) {
      return <Text type="secondary">{t('…（循环引用：{ref}）', { ref: schema.$ref })}</Text>;
    }
    const next = new Set(v);
    next.add(schema.$ref);
    const resolved = resolveRef(schema.$ref, components);
    if (!resolved) return <Text type="secondary">{t('未解析: {ref}', { ref: schema.$ref })}</Text>;
    return <SchemaTree schema={resolved} components={components} depth={depth} visited={next} />;
  }

  if (Array.isArray(schema.allOf)) {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        {schema.allOf.map((s: SchemaDefinition, i: number) => (
          <SchemaTree key={i} schema={s} components={components} depth={depth} visited={v} />
        ))}
      </Space>
    );
  }

  if (Array.isArray(schema.anyOf) || Array.isArray(schema.oneOf)) {
    const list = schema.anyOf || schema.oneOf || [];
    return (
      <div>
        <Text type="secondary">{t('联合类型（满足任一即可）：')}</Text>
        {list.map((s: SchemaDefinition, i: number) => (
          <Collapse size="small" style={{ marginTop: 4 }} key={i}
            items={[{
              key: String(i),
              label: t('选项 {n}：{type}', { n: i + 1, type: typeLabel(s) }),
              children: <SchemaTree schema={s} components={components} depth={depth + 1} visited={v} />,
            }]}
          />
        ))}
      </div>
    );
  }

  if (schema.type === 'array') {
    return (
      <div>
        <Text type="secondary">{t('数组，元素类型：')}</Text>
        <div style={{ marginLeft: 12, marginTop: 4 }}>
          <SchemaTree schema={schema.items} components={components} depth={depth + 1} visited={v} />
        </div>
      </div>
    );
  }

  if (schema.type === 'object' || schema.properties) {
    const props = schema.properties || {};
    const required: string[] = schema.required || [];
    const rows = Object.entries(props).map(([name, def]) => ({
      name,
      def,
      isRequired: required.includes(name),
      typeStr: typeLabel(def),
      isComplex: def.type === 'object' || def.properties || def.$ref || def.type === 'array' || def.anyOf || def.oneOf || def.allOf,
    }));

    if (rows.length === 0) {
      return <Text type="secondary">{schema.additionalProperties ? t('任意键值对（additionalProperties）') : t('空对象')}</Text>;
    }

    return (
      <Table
        dataSource={rows}
        rowKey="name"
        pagination={false}
        size="small"
        columns={[
          {
            title: t('字段'),
            dataIndex: 'name',
            width: 200,
            render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
          },
          {
            title: t('类型'),
            dataIndex: 'typeStr',
            width: 140,
            render: (v: string) => <Tag color="default" style={{ fontFamily: 'monospace' }}>{v}</Tag>,
          },
          {
            title: t('必填'),
            dataIndex: 'isRequired',
            width: 70,
            render: (v: boolean) => v ? <Tag color="red">{t('必填')}</Tag> : <Tag>{t('可选')}</Tag>,
          },
          {
            title: t('说明'),
            render: (_, r) => (
              <div>
                {r.def.description && <div style={{ marginBottom: 4 }}>{r.def.description}</div>}
                {Array.isArray(r.def.enum) && (
                  <div style={{ marginBottom: 4 }}>
                    <Text type="secondary">{t('枚举：')}</Text>
                    {r.def.enum.map((e: JsonValue, i: number) => (
                      <Tag key={i} style={{ marginRight: 4 }}>{String(e)}</Tag>
                    ))}
                  </div>
                )}
                {r.def.default !== undefined && (
                  <div style={{ marginBottom: 4 }}>
                    <Text type="secondary">{t('默认：')}</Text>
                    <Text code>{JSON.stringify(r.def.default)}</Text>
                  </div>
                )}
                {r.isComplex && (
                  <Collapse size="small" ghost
                    items={[{
                      key: 'nested',
                      label: <Text type="secondary">{t('展开嵌套结构')}</Text>,
                      children: <SchemaTree schema={r.def} components={components} depth={depth + 1} visited={v} />,
                    }]}
                  />
                )}
              </div>
            ),
          },
        ]}
      />
    );
  }

  return (
    <Space size={4} wrap>
      <Tag style={{ fontFamily: 'monospace' }}>{typeLabel(schema)}</Tag>
      {schema.format && <Text type="secondary">format: {schema.format}</Text>}
      {Array.isArray(schema.enum) && (
        <span>{t('枚举：')}{schema.enum.map((e: JsonValue, i: number) => <Tag key={i}>{String(e)}</Tag>)}</span>
      )}
      {schema.description && <Text type="secondary">{schema.description}</Text>}
    </Space>
  );
}
