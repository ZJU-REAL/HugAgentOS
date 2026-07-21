import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Switch,
  Typography,
  message,
} from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { adminFetch } from '../../utils/adminApi';
import { t } from '../../i18n';
import { OntologyListPagination } from './OntologyListPagination';
import { ONTOLOGY_LIST_DEFAULT_PAGE_SIZE, paginateOntologyItems } from './ontologyPagination';
import type {
  OntologyConcept,
  OntologyConstraint,
  OntologyDocument,
  OntologyEditableModule,
  OntologyPackConfig,
  OntologyPackVersion,
  OntologyRelation,
  OntologyWorkflow,
  ValidationIssue,
} from './ontologyTypes';

const { Paragraph, Text, Title } = Typography;
const { TextArea } = Input;

type JsonSchemaType = 'object' | 'array' | 'string' | 'number' | 'integer' | 'boolean';

interface SchemaPropertyFormValue {
  name: string;
  type: JsonSchemaType;
  description?: string;
  required?: boolean;
  enum_values?: string[];
  min_length?: number;
  max_length?: number;
  minimum?: number;
  maximum?: number;
  pattern?: string;
}

interface SchemaFormValue {
  enabled?: boolean;
  type?: JsonSchemaType;
  description?: string;
  enum_values?: string[];
  min_length?: number;
  max_length?: number;
  minimum?: number;
  maximum?: number;
  pattern?: string;
  additional_properties?: boolean;
  properties?: SchemaPropertyFormValue[];
}

interface ConstraintFormValue extends Omit<OntologyConstraint, 'schema'> {
  schema_form?: SchemaFormValue;
}

interface OntologyEditorValues {
  next_version: string;
  name?: string;
  domain?: string;
  description?: string;
  config?: OntologyPackConfig;
  concepts?: OntologyConcept[];
  relations?: OntologyRelation[];
  constraints?: ConstraintFormValue[];
  workflows?: OntologyWorkflow[];
}

interface OntologyModuleEditorProps {
  token: string;
  apiPrefix: string;
  open: boolean;
  module: OntologyEditableModule | null;
  document: OntologyDocument | null;
  version: OntologyPackVersion | null;
  existingVersions: string[];
  onCancel: () => void;
  onSaved: (versionId: string) => Promise<void> | void;
}

interface FormListField {
  key: number;
  name: number;
}

const IDENTIFIER_PATTERN = /^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/;
const VERSION_PATTERN = /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$/;

const RISK_OPTIONS = [
  { value: 'low', label: t('低风险') },
  { value: 'medium', label: t('中风险') },
  { value: 'high', label: t('高风险') },
];

const SCHEMA_TYPE_OPTIONS = [
  { value: 'object', label: t('对象') },
  { value: 'array', label: t('数组') },
  { value: 'string', label: t('文本') },
  { value: 'number', label: t('数字') },
  { value: 'integer', label: t('整数') },
  { value: 'boolean', label: t('布尔值') },
];

const MODULE_LABELS: Record<OntologyEditableModule, string> = {
  overview: t('基本信息与运行配置'),
  concepts: t('概念'),
  relations: t('关系'),
  constraints: t('约束'),
  workflows: t('工作流'),
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function nextPatchVersion(version: string, existingVersions: string[]): string {
  const parsed = [version, ...existingVersions].flatMap((item) => {
    const match = item.match(/^(\d+)\.(\d+)\.(\d+)/);
    return match ? [[Number(match[1]), Number(match[2]), Number(match[3])]] : [];
  });
  const latest = parsed.sort((left, right) => (
    right[0] - left[0] || right[1] - left[1] || right[2] - left[2]
  ))[0];
  if (!latest) return '1.0.0';
  return `${latest[0]}.${latest[1]}.${latest[2] + 1}`;
}

function schemaToForm(schema?: Record<string, unknown>): SchemaFormValue {
  if (!schema || !Object.keys(schema).length) {
    return { enabled: false, type: 'object', additional_properties: true, properties: [] };
  }
  const properties = isRecord(schema.properties) ? schema.properties : {};
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((item): item is string => typeof item === 'string')
      : [],
  );
  return {
    enabled: true,
    type: stringValue(schema.type) as JsonSchemaType | undefined,
    description: stringValue(schema.description),
    enum_values: Array.isArray(schema.enum) ? schema.enum.map(String) : [],
    min_length: numberValue(schema.minLength),
    max_length: numberValue(schema.maxLength),
    minimum: numberValue(schema.minimum),
    maximum: numberValue(schema.maximum),
    pattern: stringValue(schema.pattern),
    additional_properties: schema.additionalProperties !== false,
    properties: Object.entries(properties).flatMap(([name, value]) => {
      if (!isRecord(value)) return [];
      return [{
        name,
        type: (stringValue(value.type) as JsonSchemaType | undefined) ?? 'string',
        description: stringValue(value.description),
        required: required.has(name),
        enum_values: Array.isArray(value.enum) ? value.enum.map(String) : [],
        min_length: numberValue(value.minLength),
        max_length: numberValue(value.maxLength),
        minimum: numberValue(value.minimum),
        maximum: numberValue(value.maximum),
        pattern: stringValue(value.pattern),
      }];
    }),
  };
}

function coerceEnumValue(value: string, type?: JsonSchemaType): string | number | boolean {
  if (type === 'number' || type === 'integer') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : value;
  }
  if (type === 'boolean') return value === 'true';
  return value;
}

function assignOptional(target: Record<string, unknown>, key: string, value: unknown): void {
  if (value === undefined || value === null || value === '') {
    delete target[key];
  } else {
    target[key] = value;
  }
}

function schemaFromForm(
  formValue: SchemaFormValue | undefined,
  originalSchema?: Record<string, unknown>,
): Record<string, unknown> {
  if (!formValue?.enabled) return {};
  const next: Record<string, unknown> = { ...(originalSchema ?? {}) };
  const type = formValue.type ?? 'object';
  next.type = type;
  assignOptional(next, 'description', formValue.description);
  assignOptional(
    next,
    'enum',
    formValue.enum_values?.length
      ? formValue.enum_values.map((value) => coerceEnumValue(value, type))
      : undefined,
  );
  assignOptional(next, 'minLength', formValue.min_length);
  assignOptional(next, 'maxLength', formValue.max_length);
  assignOptional(next, 'minimum', formValue.minimum);
  assignOptional(next, 'maximum', formValue.maximum);
  assignOptional(next, 'pattern', formValue.pattern);

  if (type === 'object') {
    const originalProperties = isRecord(originalSchema?.properties)
      ? originalSchema.properties
      : {};
    const properties: Record<string, unknown> = {};
    const required: string[] = [];
    for (const property of formValue.properties ?? []) {
      const name = property.name?.trim();
      if (!name) continue;
      const original = isRecord(originalProperties[name]) ? originalProperties[name] : {};
      const propertySchema: Record<string, unknown> = { ...original, type: property.type };
      assignOptional(propertySchema, 'description', property.description);
      assignOptional(
        propertySchema,
        'enum',
        property.enum_values?.length
          ? property.enum_values.map((value) => coerceEnumValue(value, property.type))
          : undefined,
      );
      assignOptional(propertySchema, 'minLength', property.min_length);
      assignOptional(propertySchema, 'maxLength', property.max_length);
      assignOptional(propertySchema, 'minimum', property.minimum);
      assignOptional(propertySchema, 'maximum', property.maximum);
      assignOptional(propertySchema, 'pattern', property.pattern);
      properties[name] = propertySchema;
      if (property.required) required.push(name);
    }
    next.properties = properties;
    assignOptional(next, 'required', required.length ? required : undefined);
    next.additionalProperties = formValue.additional_properties !== false;
  } else {
    delete next.properties;
    delete next.required;
    delete next.additionalProperties;
  }
  return next;
}

function validationIssues(value: unknown): ValidationIssue[] {
  if (!isRecord(value)) return [];
  const data = isRecord(value.data) ? value.data : value;
  if (!Array.isArray(data.errors)) return [];
  return data.errors.flatMap((item) => {
    if (!isRecord(item)) return [];
    return [{
      severity: stringValue(item.severity),
      path: stringValue(item.path),
      message: stringValue(item.message) ?? t('未知校验问题'),
    }];
  });
}

function uniqueIdRule(label: string) {
  return {
    validator: async (_: unknown, items?: Array<{ id?: string }>) => {
      const ids = (items ?? []).map((item) => item?.id?.trim()).filter(Boolean);
      if (new Set(ids).size !== ids.length) {
        throw new Error(t('{label}标识不能重复', { label }));
      }
    },
  };
}

function ModuleListHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="jx-ontologyEditor-listHead">
      <div>
        <Title level={5}>{title}</Title>
        <Text type="secondary">{description}</Text>
      </div>
    </div>
  );
}

function RemoveButton({ onClick }: { onClick: () => void }) {
  return (
    <Button danger type="text" icon={<DeleteOutlined />} onClick={onClick}>
      {t('移除')}
    </Button>
  );
}

function SchemaFields({ field }: { field: FormListField }) {
  return (
    <Card size="small" className="jx-ontologyEditor-schemaCard">
      <div className="jx-ontologyEditor-schemaHead">
        <div>
          <Text strong>{t('结构化校验规则')}</Text>
          <Paragraph type="secondary">
            {t('按字段配置类型、必填项和取值范围，无需编写 JSON Schema。')}
          </Paragraph>
        </div>
        <Form.Item name={[field.name, 'schema_form', 'enabled']} valuePropName="checked" noStyle>
          <Switch checkedChildren={t('启用')} unCheckedChildren={t('不校验')} />
        </Form.Item>
      </div>
      <Row gutter={12}>
        <Col xs={24} md={8}>
          <Form.Item name={[field.name, 'schema_form', 'type']} label={t('数据类型')}>
            <Select options={SCHEMA_TYPE_OPTIONS} />
          </Form.Item>
        </Col>
        <Col xs={24} md={16}>
          <Form.Item name={[field.name, 'schema_form', 'description']} label={t('校验说明')}>
            <Input placeholder={t('说明这条结构校验的用途')} />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={12}>
        <Col xs={12} md={6}>
          <Form.Item name={[field.name, 'schema_form', 'min_length']} label={t('最小长度')}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={12} md={6}>
          <Form.Item name={[field.name, 'schema_form', 'max_length']} label={t('最大长度')}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={12} md={6}>
          <Form.Item name={[field.name, 'schema_form', 'minimum']} label={t('最小数值')}>
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={12} md={6}>
          <Form.Item name={[field.name, 'schema_form', 'maximum']} label={t('最大数值')}>
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={12}>
        <Col xs={24} md={12}>
          <Form.Item name={[field.name, 'schema_form', 'enum_values']} label={t('限定取值')}>
            <Select mode="tags" tokenSeparators={[',', '，']} placeholder={t('输入后回车，可添加多个值')} />
          </Form.Item>
        </Col>
        <Col xs={24} md={12}>
          <Form.Item name={[field.name, 'schema_form', 'pattern']} label={t('格式规则（正则，可选）')}>
            <Input placeholder="^[A-Za-z0-9_-]+$" />
          </Form.Item>
        </Col>
      </Row>
      <Divider titlePlacement="start" plain>{t('对象字段')}</Divider>
      <Form.Item
        name={[field.name, 'schema_form', 'additional_properties']}
        label={t('允许未列出的其他字段')}
        valuePropName="checked"
      >
        <Switch />
      </Form.Item>
      <Form.List name={[field.name, 'schema_form', 'properties']}>
        {(propertyFields, { add, remove }) => (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            {propertyFields.map((propertyField) => (
              <div className="jx-ontologyEditor-property" key={propertyField.key}>
                <Row gutter={10} align="middle">
                  <Col xs={24} md={6}>
                    <Form.Item
                      name={[propertyField.name, 'name']}
                      label={t('字段名')}
                      rules={[{ required: true, message: t('请输入字段名') }]}
                    >
                      <Input placeholder="company_id" />
                    </Form.Item>
                  </Col>
                  <Col xs={12} md={5}>
                    <Form.Item name={[propertyField.name, 'type']} label={t('类型')} rules={[{ required: true }]}>
                      <Select options={SCHEMA_TYPE_OPTIONS} />
                    </Form.Item>
                  </Col>
                  <Col xs={12} md={4}>
                    <Form.Item name={[propertyField.name, 'required']} label={t('必填')} valuePropName="checked">
                      <Switch />
                    </Form.Item>
                  </Col>
                  <Col xs={24} md={7}>
                    <Form.Item name={[propertyField.name, 'description']} label={t('字段说明')}>
                      <Input />
                    </Form.Item>
                  </Col>
                  <Col xs={24} md={2} className="jx-ontologyEditor-propertyRemove">
                    <Button
                      danger
                      type="text"
                      aria-label={t('移除字段')}
                      icon={<DeleteOutlined />}
                      onClick={() => remove(propertyField.name)}
                    />
                  </Col>
                </Row>
                <Row gutter={10}>
                  <Col xs={24} md={8}>
                    <Form.Item name={[propertyField.name, 'enum_values']} label={t('限定取值')}>
                      <Select mode="tags" tokenSeparators={[',', '，']} />
                    </Form.Item>
                  </Col>
                  <Col xs={12} md={4}>
                    <Form.Item name={[propertyField.name, 'min_length']} label={t('最小长度')}>
                      <InputNumber min={0} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                  <Col xs={12} md={4}>
                    <Form.Item name={[propertyField.name, 'max_length']} label={t('最大长度')}>
                      <InputNumber min={0} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                  <Col xs={12} md={4}>
                    <Form.Item name={[propertyField.name, 'minimum']} label={t('最小数值')}>
                      <InputNumber style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                  <Col xs={12} md={4}>
                    <Form.Item name={[propertyField.name, 'maximum']} label={t('最大数值')}>
                      <InputNumber style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                </Row>
              </div>
            ))}
            <Button
              type="dashed"
              block
              icon={<PlusOutlined />}
              onClick={() => add({ type: 'string', required: false })}
            >
              {t('添加对象字段')}
            </Button>
          </Space>
        )}
      </Form.List>
    </Card>
  );
}

export function OntologyModuleEditor({
  token,
  apiPrefix,
  open,
  module,
  document,
  version,
  existingVersions,
  onCancel,
  onSaved,
}: OntologyModuleEditorProps) {
  const [form] = Form.useForm<OntologyEditorValues>();
  const [saving, setSaving] = useState(false);
  const [saveIssues, setSaveIssues] = useState<ValidationIssue[]>([]);
  const [listPage, setListPage] = useState(1);
  const [listPageSize, setListPageSize] = useState(ONTOLOGY_LIST_DEFAULT_PAGE_SIZE);
  const isWorkingDraft = version?.status === 'draft';
  const watchedConcepts = Form.useWatch('concepts', form) as OntologyConcept[] | undefined;
  const conceptOptions = useMemo(
    () => (watchedConcepts ?? document?.concepts ?? [])
      .filter((item) => item?.id)
      .map((item) => ({ value: item.id, label: `${item.name || item.id} · ${item.id}` })),
    [document?.concepts, watchedConcepts],
  );

  useEffect(() => {
    if (!open || !module || !document) return;
    const constraints = (document.constraints ?? []).map((constraint) => ({
      ...constraint,
      schema_form: schemaToForm(constraint.schema),
    }));
    form.setFieldsValue({
      next_version: isWorkingDraft
        ? document.version
        : nextPatchVersion(document.version, existingVersions),
      name: document.name,
      domain: document.domain,
      description: document.description,
      config: { ...document.config },
      concepts: (document.concepts ?? []).map((item) => ({ ...item })),
      relations: (document.relations ?? []).map((item) => ({ ...item })),
      constraints,
      workflows: (document.workflows ?? []).map((item) => ({
        ...item,
        asset_triggers: (item.asset_triggers ?? []).map((trigger) => ({ ...trigger })),
      })),
    });
    setSaveIssues([]);
    setListPage(1);
    setListPageSize(ONTOLOGY_LIST_DEFAULT_PAGE_SIZE);
  }, [document, existingVersions, form, isWorkingDraft, module, open]);

  const handleListPageChange = useCallback((page: number, pageSize: number) => {
    setListPage(page);
    setListPageSize(pageSize);
  }, []);

  const moveToLastListPage = useCallback((total: number) => {
    setListPage(Math.max(1, Math.ceil(total / listPageSize)));
  }, [listPageSize]);

  const keepListPageInRange = useCallback((total: number) => {
    const lastPage = Math.max(1, Math.ceil(total / listPageSize));
    setListPage((current) => Math.min(current, lastPage));
  }, [listPageSize]);

  const buildNextDocument = (values: OntologyEditorValues): OntologyDocument => {
    if (!document || !module) throw new Error(t('缺少可编辑的本体版本'));
    const next = JSON.parse(JSON.stringify(document)) as OntologyDocument;
    next.version = isWorkingDraft ? document.version : values.next_version.trim();
    if (module === 'overview') {
      next.name = values.name?.trim() ?? next.name;
      next.domain = values.domain?.trim() ?? next.domain;
      next.description = values.description?.trim() ?? '';
      next.config = values.config ?? {};
    } else if (module === 'concepts') {
      next.concepts = values.concepts ?? [];
    } else if (module === 'relations') {
      next.relations = values.relations ?? [];
    } else if (module === 'constraints') {
      const originalById = new Map((document.constraints ?? []).map((item) => [item.id, item]));
      next.constraints = (values.constraints ?? []).map((item) => {
        const { schema_form: schemaForm, ...constraint } = item;
        const target = { ...constraint.target };
        if (target.kind === 'output') {
          delete target.tool;
          delete target.parameter;
        } else {
          delete target.output_tag;
          if (target.kind === 'tool') delete target.parameter;
        }
        return {
          ...constraint,
          target,
          schema: schemaFromForm(schemaForm, originalById.get(item.id)?.schema),
        };
      });
    } else if (module === 'workflows') {
      next.workflows = values.workflows ?? [];
    }
    return next;
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveIssues([]);
    try {
      const values = await form.validateFields();
      const nextDocument = buildNextDocument(values);
      const validationResponse = await adminFetch(token, `${apiPrefix}/validate`, {
        method: 'POST',
        body: JSON.stringify(nextDocument),
      });
      const report = validationResponse?.data ?? validationResponse;
      if (!report?.valid) {
        setSaveIssues(validationIssues(report));
        return;
      }
      const response = await adminFetch(
        token,
        `${apiPrefix}/${encodeURIComponent(nextDocument.pack_id)}/draft`,
        {
          method: 'PUT',
          body: JSON.stringify({
            document: nextDocument,
            draft_version_id: isWorkingDraft ? version?.version_id : null,
            expected_checksum: isWorkingDraft ? version?.checksum : null,
          }),
        },
      );
      const savedDraft = response?.data ?? response;
      message.success(savedDraft.created ? t('工作草稿已创建') : t('工作草稿已更新'));
      await onSaved(String(savedDraft.version_id));
      onCancel();
    } catch (error) {
      const issues = isRecord(error) && 'data' in error ? validationIssues(error.data) : [];
      if (issues.length) setSaveIssues(issues);
      message.error(error instanceof Error ? error.message : t('保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const renderOverview = () => (
    <div className="jx-ontologyEditor-section">
      <Alert
        showIcon
        type="info"
        message={t('包标识与结构版本保持不变')}
        description={t('当前包标识为 {packId}，结构版本为 {schemaVersion}。', {
          packId: document?.pack_id ?? '-',
          schemaVersion: document?.schema_version ?? '1.0',
        })}
      />
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Form.Item name="name" label={t('领域包名称')} rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={255} showCount />
          </Form.Item>
        </Col>
        <Col xs={24} md={12}>
          <Form.Item name="domain" label={t('领域')} rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={128} />
          </Form.Item>
        </Col>
      </Row>
      <Form.Item name="description" label={t('领域包说明')}>
        <TextArea rows={3} maxLength={4000} showCount />
      </Form.Item>
      <Divider titlePlacement="start">{t('运行配置')}</Divider>
      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Form.Item name={['config', 'injection_enabled']} label={t('允许运行时注入')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name={['config', 'max_concepts']} label={t('最多注入概念')} rules={[{ required: true }]}>
            <InputNumber min={1} max={50} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name={['config', 'token_budget']} label={t('注入 Token 预算')} rules={[{ required: true }]}>
            <InputNumber min={256} max={16000} step={128} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name={['config', 'committee_size']} label={t('评审委员会人数')} rules={[{ required: true }]}>
            <InputNumber min={2} max={5} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name={['config', 'repeated_denial_threshold']} label={t('连续拒绝阈值')} rules={[{ required: true }]}>
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name={['config', 'circuit_breaker_threshold']} label={t('熔断阈值')} rules={[{ required: true }]}>
            <InputNumber min={2} max={50} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>
      <Form.Item name={['config', 'allow_unresolved_tools']} label={t('允许未解析工具')} valuePropName="checked">
        <Switch />
      </Form.Item>
    </div>
  );

  const renderConcepts = () => (
    <Form.List name="concepts" rules={[uniqueIdRule(t('概念'))]}>
      {(fields, { add, remove }, meta) => (
        <div className="jx-ontologyEditor-section">
          <ModuleListHeader
            title={t('概念词表')}
            description={t('逐项维护领域术语、定义、别名、层级和受控取值。')}
          />
          {paginateOntologyItems(fields, listPage, listPageSize).map((field) => (
            <Card
              size="small"
              title={t('概念 {n}', { n: field.name + 1 })}
              extra={<RemoveButton onClick={() => {
                remove(field.name);
                keepListPageInRange(fields.length - 1);
              }} />}
              key={field.key}
              className="jx-ontologyEditor-itemCard"
            >
              <Row gutter={12}>
                <Col xs={24} md={8}>
                  <Form.Item
                    name={[field.name, 'id']}
                    label={t('概念标识')}
                    rules={[
                      { required: true, whitespace: true },
                      { pattern: IDENTIFIER_PATTERN, message: t('需以字母开头，只能包含字母、数字及 _ . : -') },
                    ]}
                  >
                    <Input placeholder="RiskEvent" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'name']} label={t('概念名称')} rules={[{ required: true, whitespace: true }]}>
                    <Input maxLength={255} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'risk']} label={t('风险等级')} rules={[{ required: true }]}>
                    <Select options={RISK_OPTIONS} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name={[field.name, 'definition']} label={t('概念定义')} rules={[{ required: true, whitespace: true }]}>
                <TextArea rows={2} maxLength={4000} showCount />
              </Form.Item>
              <Row gutter={12}>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'parent_id']} label={t('上位概念')}>
                    <Select allowClear showSearch options={conceptOptions} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'aliases']} label={t('别名')}>
                    <Select mode="tags" tokenSeparators={[',', '，']} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'tags']} label={t('标签')}>
                    <Select mode="tags" tokenSeparators={[',', '，']} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name={[field.name, 'closed_values']} label={t('受控取值')}>
                <Select mode="tags" tokenSeparators={[',', '，']} placeholder={t('输入后回车，可添加多个值')} />
              </Form.Item>
            </Card>
          ))}
          <OntologyListPagination
            current={listPage}
            pageSize={listPageSize}
            total={fields.length}
            onChange={handleListPageChange}
          />
          <Form.ErrorList errors={meta.errors} />
          <Button
            type="dashed"
            block
            icon={<PlusOutlined />}
            onClick={() => {
              add({ aliases: [], closed_values: [], tags: [], risk: 'low' });
              moveToLastListPage(fields.length + 1);
            }}
          >
            {t('添加概念')}
          </Button>
        </div>
      )}
    </Form.List>
  );

  const renderRelations = () => (
    <Form.List name="relations" rules={[uniqueIdRule(t('关系'))]}>
      {(fields, { add, remove }, meta) => (
        <div className="jx-ontologyEditor-section">
          <ModuleListHeader
            title={t('概念关系')}
            description={t('从已有概念中选择起点和终点，并设置基数或禁止关系。')}
          />
          {paginateOntologyItems(fields, listPage, listPageSize).map((field) => (
            <Card
              size="small"
              title={t('关系 {n}', { n: field.name + 1 })}
              extra={<RemoveButton onClick={() => {
                remove(field.name);
                keepListPageInRange(fields.length - 1);
              }} />}
              key={field.key}
              className="jx-ontologyEditor-itemCard"
            >
              <Row gutter={12}>
                <Col xs={24} md={8}>
                  <Form.Item
                    name={[field.name, 'id']}
                    label={t('关系标识')}
                    rules={[
                      { required: true, whitespace: true },
                      { pattern: IDENTIFIER_PATTERN, message: t('需以字母开头，只能包含字母、数字及 _ . : -') },
                    ]}
                  >
                    <Input />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'subject']} label={t('起点概念')} rules={[{ required: true }]}>
                    <Select showSearch options={conceptOptions} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'object']} label={t('终点概念')} rules={[{ required: true }]}>
                    <Select showSearch options={conceptOptions} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col xs={24} md={12}>
                  <Form.Item name={[field.name, 'predicate']} label={t('关系谓词')} rules={[{ required: true, whitespace: true }]}>
                    <Input placeholder={t('例如：由证据支持')} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={4}>
                  <Form.Item name={[field.name, 'min_cardinality']} label={t('最少数量')}>
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={4}>
                  <Form.Item name={[field.name, 'max_cardinality']} label={t('最多数量')}>
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={4}>
                  <Form.Item name={[field.name, 'forbidden']} label={t('禁止此关系')} valuePropName="checked">
                    <Switch />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name={[field.name, 'description']} label={t('关系说明')}>
                <TextArea rows={2} maxLength={2000} showCount />
              </Form.Item>
            </Card>
          ))}
          <OntologyListPagination
            current={listPage}
            pageSize={listPageSize}
            total={fields.length}
            onChange={handleListPageChange}
          />
          <Form.ErrorList errors={meta.errors} />
          <Button
            type="dashed"
            block
            icon={<PlusOutlined />}
            onClick={() => {
              add({ forbidden: false, min_cardinality: 0 });
              moveToLastListPage(fields.length + 1);
            }}
          >
            {t('添加关系')}
          </Button>
        </div>
      )}
    </Form.List>
  );

  const renderConstraints = () => (
    <Form.List name="constraints" rules={[uniqueIdRule(t('约束'))]}>
      {(fields, { add, remove }, meta) => (
        <div className="jx-ontologyEditor-section">
          <ModuleListHeader
            title={t('执行约束')}
            description={t('配置工具参数或输出要求；校验规则以表单字段维护。')}
          />
          {paginateOntologyItems(fields, listPage, listPageSize).map((field) => (
            <Card
              size="small"
              title={t('约束 {n}', { n: field.name + 1 })}
              extra={<RemoveButton onClick={() => {
                remove(field.name);
                keepListPageInRange(fields.length - 1);
              }} />}
              key={field.key}
              className="jx-ontologyEditor-itemCard"
            >
              <Row gutter={12}>
                <Col xs={24} md={8}>
                  <Form.Item
                    name={[field.name, 'id']}
                    label={t('约束标识')}
                    rules={[
                      { required: true, whitespace: true },
                      { pattern: IDENTIFIER_PATTERN, message: t('需以字母开头，只能包含字母、数字及 _ . : -') },
                    ]}
                  >
                    <Input />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'name']} label={t('约束名称')} rules={[{ required: true, whitespace: true }]}>
                    <Input maxLength={255} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={4}>
                  <Form.Item name={[field.name, 'mode']} label={t('执行模式')} rules={[{ required: true }]}>
                    <Select options={[
                      { value: 'log', label: t('仅记录') },
                      { value: 'enforce', label: t('强制执行') },
                    ]} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={4}>
                  <Form.Item name={[field.name, 'risk']} label={t('风险等级')} rules={[{ required: true }]}>
                    <Select options={RISK_OPTIONS} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col xs={24} md={6}>
                  <Form.Item name={[field.name, 'target', 'kind']} label={t('约束目标')} rules={[{ required: true }]}>
                    <Select options={[
                      { value: 'tool', label: t('工具调用') },
                      { value: 'tool_parameter', label: t('工具参数') },
                      { value: 'output', label: t('最终输出') },
                    ]} />
                  </Form.Item>
                </Col>
                <Form.Item noStyle shouldUpdate>
                  {({ getFieldValue }) => {
                    const kind = getFieldValue(['constraints', field.name, 'target', 'kind']);
                    if (kind === 'output') {
                      return (
                        <Col xs={24} md={10}>
                          <Form.Item name={[field.name, 'target', 'output_tag']} label={t('输出标签')} rules={[{ required: true, whitespace: true }]}>
                            <Input placeholder="enterprise_risk_report" />
                          </Form.Item>
                        </Col>
                      );
                    }
                    return (
                      <>
                        <Col xs={24} md={kind === 'tool_parameter' ? 6 : 10}>
                          <Form.Item name={[field.name, 'target', 'tool']} label={t('工具标识')} rules={[{ required: true, whitespace: true }]}>
                            <Input placeholder="get_company_risk_warning" />
                          </Form.Item>
                        </Col>
                        {kind === 'tool_parameter' && (
                          <Col xs={24} md={6}>
                            <Form.Item name={[field.name, 'target', 'parameter']} label={t('参数名')} rules={[{ required: true, whitespace: true }]}>
                              <Input placeholder="company_id" />
                            </Form.Item>
                          </Col>
                        )}
                      </>
                    );
                  }}
                </Form.Item>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'concept_id']} label={t('关联概念')}>
                    <Select allowClear showSearch options={conceptOptions} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col xs={24} md={12}>
                  <Form.Item name={[field.name, 'prerequisite_tools']} label={t('前置工具')}>
                    <Select mode="tags" tokenSeparators={[',', '，']} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={6}>
                  <Form.Item name={[field.name, 'requires_citations']} label={t('要求引用证据')} valuePropName="checked">
                    <Switch />
                  </Form.Item>
                </Col>
                <Col xs={12} md={6}>
                  <Form.Item name={[field.name, 'enabled']} label={t('启用约束')} valuePropName="checked">
                    <Switch />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name={[field.name, 'message']} label={t('拦截提示')} rules={[{ required: true, whitespace: true }]}>
                <TextArea rows={2} maxLength={2000} showCount />
              </Form.Item>
              <Form.Item name={[field.name, 'suggestion']} label={t('修正建议')}>
                <TextArea rows={2} maxLength={2000} showCount />
              </Form.Item>
              <SchemaFields field={field} />
            </Card>
          ))}
          <OntologyListPagination
            current={listPage}
            pageSize={listPageSize}
            total={fields.length}
            onChange={handleListPageChange}
          />
          <Form.ErrorList errors={meta.errors} />
          <Button
            type="dashed"
            block
            icon={<PlusOutlined />}
            onClick={() => {
              add({
                target: { kind: 'tool' },
                schema_form: { enabled: false, type: 'object', additional_properties: true, properties: [] },
                prerequisite_tools: [],
                requires_citations: false,
                mode: 'log',
                risk: 'low',
                enabled: true,
              });
              moveToLastListPage(fields.length + 1);
            }}
          >
            {t('添加约束')}
          </Button>
        </div>
      )}
    </Form.List>
  );

  const renderWorkflows = () => (
    <Form.List name="workflows" rules={[uniqueIdRule(t('工作流'))]}>
      {(fields, { add, remove }, meta) => (
        <div className="jx-ontologyEditor-section">
          <ModuleListHeader
            title={t('领域工作流')}
            description={t('配置文本或资产触发入口、工具边界和交付前评审级别。')}
          />
          {paginateOntologyItems(fields, listPage, listPageSize).map((field) => (
            <Card
              size="small"
              title={t('工作流 {n}', { n: field.name + 1 })}
              extra={<RemoveButton onClick={() => {
                remove(field.name);
                keepListPageInRange(fields.length - 1);
              }} />}
              key={field.key}
              className="jx-ontologyEditor-itemCard"
            >
              <Row gutter={12}>
                <Col xs={24} md={8}>
                  <Form.Item
                    name={[field.name, 'id']}
                    label={t('工作流标识')}
                    rules={[
                      { required: true, whitespace: true },
                      { pattern: IDENTIFIER_PATTERN, message: t('需以字母开头，只能包含字母、数字及 _ . : -') },
                    ]}
                  >
                    <Input />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'name']} label={t('工作流名称')} rules={[{ required: true, whitespace: true }]}>
                    <Input maxLength={255} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={4}>
                  <Form.Item name={[field.name, 'review_level']} label={t('评审级别')} rules={[{ required: true }]}>
                    <Select options={[
                      { value: 'none', label: t('不评审') },
                      { value: 'checkpoint', label: 'Checkpoint' },
                      { value: 'committee', label: 'Committee' },
                    ]} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={4}>
                  <Form.Item name={[field.name, 'risk']} label={t('风险等级')} rules={[{ required: true }]}>
                    <Select options={RISK_OPTIONS} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name={[field.name, 'triggers']} label={t('文本命中词')}>
                <Select mode="tags" tokenSeparators={[',', '，']} placeholder={t('用户问题包含这些词时触发')} />
              </Form.Item>
              <Divider titlePlacement="start" plain>{t('资产触发条件')}</Divider>
              <Form.List name={[field.name, 'asset_triggers']}>
                {(triggerFields, { add: addTrigger, remove: removeTrigger }) => (
                  <Space direction="vertical" size="small" style={{ width: '100%' }}>
                    {triggerFields.map((triggerField) => (
                      <div className="jx-ontologyEditor-trigger" key={triggerField.key}>
                        <Row gutter={10} align="middle">
                          <Col xs={24} md={5}>
                            <Form.Item name={[triggerField.name, 'kind']} label={t('资产类型')} rules={[{ required: true }]}>
                              <Select options={[
                                { value: 'tool', label: t('工具') },
                                { value: 'skill', label: t('技能') },
                                { value: 'subagent', label: t('子智能体') },
                              ]} />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={8}>
                            <Form.Item name={[triggerField.name, 'ids']} label={t('资产标识')}>
                              <Select mode="tags" tokenSeparators={[',', '，']} />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={9}>
                            <Form.Item name={[triggerField.name, 'tags_any']} label={t('任一治理标签')}>
                              <Select mode="tags" tokenSeparators={[',', '，']} placeholder="ontology:RiskReport" />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={2} className="jx-ontologyEditor-propertyRemove">
                            <Button
                              danger
                              type="text"
                              aria-label={t('移除资产触发条件')}
                              icon={<DeleteOutlined />}
                              onClick={() => removeTrigger(triggerField.name)}
                            />
                          </Col>
                        </Row>
                      </div>
                    ))}
                    <Button
                      type="dashed"
                      block
                      icon={<PlusOutlined />}
                      onClick={() => addTrigger({ kind: 'tool', ids: [], tags_any: [] })}
                    >
                      {t('添加资产触发条件')}
                    </Button>
                  </Space>
                )}
              </Form.List>
              <Row gutter={12} className="jx-ontologyEditor-workflowLists">
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'required_tools']} label={t('必需工具')}>
                    <Select mode="tags" tokenSeparators={[',', '，']} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'forbidden_tools']} label={t('禁止工具')}>
                    <Select mode="tags" tokenSeparators={[',', '，']} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name={[field.name, 'output_tags']} label={t('输出标签')}>
                    <Select mode="tags" tokenSeparators={[',', '，']} />
                  </Form.Item>
                </Col>
              </Row>
            </Card>
          ))}
          <OntologyListPagination
            current={listPage}
            pageSize={listPageSize}
            total={fields.length}
            onChange={handleListPageChange}
          />
          <Form.ErrorList errors={meta.errors} />
          <Button
            type="dashed"
            block
            icon={<PlusOutlined />}
            onClick={() => {
              add({
                triggers: [],
                asset_triggers: [],
                required_tools: [],
                forbidden_tools: [],
                output_tags: [],
                review_level: 'none',
                risk: 'low',
              });
              moveToLastListPage(fields.length + 1);
            }}
          >
            {t('添加工作流')}
          </Button>
        </div>
      )}
    </Form.List>
  );

  const moduleContent = module === 'overview'
    ? renderOverview()
    : module === 'concepts'
      ? renderConcepts()
      : module === 'relations'
        ? renderRelations()
        : module === 'constraints'
          ? renderConstraints()
          : module === 'workflows'
            ? renderWorkflows()
            : null;

  return (
    <Modal
      title={module ? t('编辑{module}', { module: MODULE_LABELS[module] }) : t('编辑本体模块')}
      open={open}
      onCancel={onCancel}
      onOk={() => void handleSave()}
      okText={t('校验并保存草稿')}
      cancelText={t('取消')}
      confirmLoading={saving}
      okButtonProps={{ disabled: !document || !version }}
      width="min(1080px, calc(100vw - 32px))"
      destroyOnHidden
      className="jx-ontologyEditorModal"
    >
      <Alert
        showIcon
        type="info"
        message={isWorkingDraft ? t('继续编辑当前工作草稿') : t('首次保存将创建工作草稿')}
        description={isWorkingDraft
          ? t('本次只更新“{module}”模块，其他草稿内容保持不变；发布后该版本将锁定。', {
            module: module ? MODULE_LABELS[module] : '',
          })
          : t('本次以当前版本为基础创建一份可反复编辑的草稿；后续模块修改不会继续增加版本。', {
            module: module ? MODULE_LABELS[module] : '',
          })}
      />
      <Form form={form} layout="vertical" requiredMark="optional" className="jx-ontologyEditor-form">
        <Card size="small" className="jx-ontologyEditor-versionCard">
          <Row gutter={16} align="middle">
            <Col xs={24} md={10}>
              <Form.Item
                name="next_version"
                label={isWorkingDraft ? t('工作草稿版本号') : t('新版本号')}
                rules={[
                  { required: true, whitespace: true },
                  { pattern: VERSION_PATTERN, message: t('版本号格式示例：1.2.0') },
                ]}
              >
                <Input placeholder="1.2.0" disabled={isWorkingDraft} />
              </Form.Item>
            </Col>
            <Col xs={24} md={14}>
              <Alert
                showIcon
                type={isWorkingDraft ? 'success' : 'info'}
                message={isWorkingDraft ? t('保存时更新同一草稿') : t('该版本号只在首次创建草稿时使用')}
                description={t('完成全部模块编辑后，到“版本管理”中发布草稿。')}
              />
            </Col>
          </Row>
        </Card>
        {saveIssues.length > 0 && (
          <Alert
            showIcon
            type="error"
            message={t('当前修改未通过完整性校验')}
            description={(
              <ul className="jx-ontologyEditor-errorList">
                {saveIssues.map((issue, index) => (
                  <li key={`${issue.path ?? 'issue'}-${index}`}>
                    {issue.path && <Text code>{issue.path}</Text>} {issue.message}
                  </li>
                ))}
              </ul>
            )}
          />
        )}
        {moduleContent}
      </Form>
    </Modal>
  );
}
