import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  DeploymentUnitOutlined,
  EditOutlined,
  SafetyCertificateOutlined,
  ShareAltOutlined,
  TagsOutlined,
} from '@ant-design/icons';
import { adminFetch } from '../../utils/adminApi';
import { formatDateTime } from '../../utils/date';
import { t } from '../../i18n';
import { OntologyModuleEditor } from './OntologyModuleEditor';
import type {
  OntologyDocument,
  OntologyEditableModule,
  OntologyPackSummary,
  OntologyPackVersion,
  ValidationIssue,
} from './ontologyTypes';

const { Paragraph, Text, Title } = Typography;

interface OntologyDetailDrawerProps {
  token: string;
  apiPrefix: string;
  pack: OntologyPackSummary | null;
  initialVersionId?: string;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

interface EditableModulePanelProps {
  module: OntologyEditableModule;
  description: string;
  onEdit: (module: OntologyEditableModule) => void;
  children: React.ReactNode;
}

const MODULE_TITLES: Record<OntologyEditableModule, string> = {
  overview: t('基本信息与运行配置'),
  concepts: t('概念词表'),
  relations: t('概念关系'),
  constraints: t('执行约束'),
  workflows: t('领域工作流'),
};

function EditableModulePanel({ module, description, onEdit, children }: EditableModulePanelProps) {
  return (
    <div className="jx-ontologyDetail-module">
      <div className="jx-ontologyDetail-moduleHead">
        <div>
          <Text strong>{MODULE_TITLES[module]}</Text>
          <Text type="secondary">{description}</Text>
        </div>
        <Button icon={<EditOutlined />} onClick={() => onEdit(module)}>
          {t('编辑此模块')}
        </Button>
      </div>
      {children}
    </div>
  );
}

function statusColor(status: OntologyPackVersion['status']): string {
  if (status === 'active') return 'green';
  if (status === 'retired') return 'default';
  return 'blue';
}

function statusLabel(status: OntologyPackVersion['status']): string {
  if (status === 'active') return t('已激活');
  if (status === 'retired') return t('已归档');
  return t('工作草稿');
}

function riskColor(risk?: string): string {
  if (risk === 'high') return 'red';
  if (risk === 'medium') return 'orange';
  return 'green';
}

function yesNo(value?: boolean): string {
  return value ? t('是') : t('否');
}

function ValueTags({ values, color }: { values?: string[]; color?: string }) {
  if (!values?.length) return <Text type="secondary">{t('无')}</Text>;
  return (
    <Space size={[6, 6]} wrap>
      {values.map((value) => <Tag color={color} key={value}>{value}</Tag>)}
    </Space>
  );
}

function ValidationList({ items, kind }: { items?: ValidationIssue[]; kind: 'error' | 'warning' }) {
  if (!items?.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('没有此类问题')} />;
  }
  const Icon = kind === 'error' ? CloseCircleOutlined : ClockCircleOutlined;
  return (
    <div className="jx-ontologyDetail-validationList">
      {items.map((item, index) => (
        <div className={`jx-ontologyDetail-validationItem jx-ontologyDetail-validationItem--${kind}`} key={`${item.path ?? 'item'}-${index}`}>
          <Icon />
          <div>
            {item.path && <Text code>{item.path}</Text>}
            <Paragraph>{item.message || t('未知校验问题')}</Paragraph>
          </div>
        </div>
      ))}
    </div>
  );
}

export function OntologyDetailDrawer({
  token,
  apiPrefix,
  pack,
  initialVersionId,
  onClose,
  onChanged,
}: OntologyDetailDrawerProps) {
  const [selectedVersionId, setSelectedVersionId] = useState<string | undefined>(initialVersionId);
  const [editingModule, setEditingModule] = useState<OntologyEditableModule | null>(null);
  const [loadState, setLoadState] = useState<{
    key: string;
    document: OntologyDocument | null;
    error: string;
  }>({ key: '', document: null, error: '' });
  const initialVersion = pack?.versions.find((version) => version.version_id === pack.active_version_id)
    ?? pack?.versions[0];
  const effectiveVersionId = pack?.versions.some((version) => version.version_id === selectedVersionId)
    ? selectedVersionId
    : initialVersion?.version_id;
  const packId = pack?.pack_id;
  const selectedVersion = pack?.versions.find((version) => version.version_id === effectiveVersionId);
  const workingDraft = pack?.versions.find(
    (version) => version.version_id === pack.working_draft_version_id,
  ) ?? pack?.versions.find((version) => version.status === 'draft');
  const existingVersions = useMemo(
    () => pack?.versions.map((version) => version.version) ?? [],
    [pack?.versions],
  );
  const requestKey = packId && effectiveVersionId
    ? `${packId}:${effectiveVersionId}:${selectedVersion?.checksum ?? ''}`
    : '';

  const handleEditModule = (module: OntologyEditableModule) => {
    if (workingDraft && effectiveVersionId !== workingDraft.version_id) {
      setSelectedVersionId(workingDraft.version_id);
    }
    setEditingModule(module);
  };

  useEffect(() => {
    if (!packId || !effectiveVersionId || !requestKey) return;
    let cancelled = false;
    void adminFetch(
      token,
      `${apiPrefix}/${encodeURIComponent(packId)}/versions/${encodeURIComponent(effectiveVersionId)}/export`,
    )
      .then((response) => {
        if (!cancelled) {
          setLoadState({ key: requestKey, document: (response?.data ?? response) as OntologyDocument, error: '' });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadState({
            key: requestKey,
            document: null,
            error: error instanceof Error ? error.message : t('完整本体内容加载失败'),
          });
        }
      });
    return () => { cancelled = true; };
  }, [apiPrefix, effectiveVersionId, packId, requestKey, token]);

  const isCurrentRequest = Boolean(requestKey) && loadState.key === requestKey;
  const loading = Boolean(requestKey) && !isCurrentRequest;
  const document = isCurrentRequest ? loadState.document : null;
  const loadError = isCurrentRequest ? loadState.error : '';
  const concepts = useMemo(() => document?.concepts ?? [], [document]);
  const relations = document?.relations ?? [];
  const constraints = document?.constraints ?? [];
  const workflows = document?.workflows ?? [];
  const conceptNames = useMemo(
    () => new Map(concepts.map((concept) => [concept.id, concept.name])),
    [concepts],
  );

  const overview = document && selectedVersion ? (
    <EditableModulePanel
      module="overview"
      description={t('维护领域包说明、注入预算和评审阈值。')}
      onEdit={handleEditModule}
    >
      <div className="jx-ontologyDetail-overview">
      <Card size="small" title={t('领域包信息')}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
          <Descriptions.Item label={t('包标识')}><Text copyable>{document.pack_id}</Text></Descriptions.Item>
          <Descriptions.Item label={t('领域')}>{document.domain}</Descriptions.Item>
          <Descriptions.Item label={t('结构版本')}>{document.schema_version || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('本体版本')}>v{document.version}</Descriptions.Item>
          <Descriptions.Item label={t('创建时间')}>{formatDateTime(selectedVersion.created_at)}</Descriptions.Item>
          <Descriptions.Item label={t('激活时间')}>{formatDateTime(selectedVersion.activated_at)}</Descriptions.Item>
          <Descriptions.Item label={t('校验和')} span={3}><Text code copyable>{selectedVersion.checksum}</Text></Descriptions.Item>
        </Descriptions>
      </Card>

      <Card size="small" title={t('运行配置')}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} bordered>
          <Descriptions.Item label={t('允许运行时注入')}>{yesNo(document.config?.injection_enabled)}</Descriptions.Item>
          <Descriptions.Item label={t('最多注入概念')}>{document.config?.max_concepts ?? '-'}</Descriptions.Item>
          <Descriptions.Item label={t('注入 Token 预算')}>{document.config?.token_budget ?? '-'}</Descriptions.Item>
          <Descriptions.Item label={t('评审委员会人数')}>{document.config?.committee_size ?? '-'}</Descriptions.Item>
          <Descriptions.Item label={t('连续拒绝阈值')}>{document.config?.repeated_denial_threshold ?? '-'}</Descriptions.Item>
          <Descriptions.Item label={t('熔断阈值')}>{document.config?.circuit_breaker_threshold ?? '-'}</Descriptions.Item>
          <Descriptions.Item label={t('允许未解析工具')}>{yesNo(document.config?.allow_unresolved_tools)}</Descriptions.Item>
        </Descriptions>
      </Card>
      </div>
    </EditableModulePanel>
  ) : null;

  const conceptPanel = concepts.length ? (
    <EditableModulePanel
      module="concepts"
      description={t('维护术语定义、别名、层级、标签和受控取值。')}
      onEdit={handleEditModule}
    >
      <div className="jx-ontologyDetail-cardGrid">
        {concepts.map((concept) => (
        <Card className="jx-ontologyDetail-concept" size="small" key={concept.id}>
          <div className="jx-ontologyDetail-cardHead">
            <div>
              <Title level={5}>{concept.name}</Title>
              <Text code>{concept.id}</Text>
            </div>
            <Tag color={riskColor(concept.risk)}>{t('{risk} 风险', { risk: concept.risk ?? 'low' })}</Tag>
          </div>
          <Paragraph>{concept.definition}</Paragraph>
          {concept.parent_id && (
            <div className="jx-ontologyDetail-line">
              <Text type="secondary">{t('上位概念')}</Text>
              <span>{conceptNames.get(concept.parent_id) || concept.parent_id} <Text code>{concept.parent_id}</Text></span>
            </div>
          )}
          <div className="jx-ontologyDetail-line">
            <Text type="secondary">{t('别名')}</Text>
            <ValueTags values={concept.aliases} color="blue" />
          </div>
          <div className="jx-ontologyDetail-line">
            <Text type="secondary">{t('标签')}</Text>
            <ValueTags values={concept.tags} />
          </div>
          {Boolean(concept.closed_values?.length) && (
            <div className="jx-ontologyDetail-line">
              <Text type="secondary">{t('受控取值')}</Text>
              <ValueTags values={concept.closed_values} color="purple" />
            </div>
          )}
        </Card>
        ))}
      </div>
    </EditableModulePanel>
  ) : (
    <EditableModulePanel module="concepts" description={t('当前没有概念，可从这里开始添加。')} onEdit={handleEditModule}>
      <Empty description={t('暂无概念')} />
    </EditableModulePanel>
  );

  const relationPanel = relations.length ? (
    <EditableModulePanel
      module="relations"
      description={t('维护概念之间的方向、谓词、基数和禁止关系。')}
      onEdit={handleEditModule}
    >
      <div className="jx-ontologyDetail-relationList">
        {relations.map((relation) => (
        <Card size="small" key={relation.id}>
          <div className="jx-ontologyDetail-relationFlow">
            <div><Text strong>{conceptNames.get(relation.subject) || relation.subject}</Text><Text code>{relation.subject}</Text></div>
            <div className="jx-ontologyDetail-predicate"><ShareAltOutlined /> {relation.predicate}</div>
            <div><Text strong>{conceptNames.get(relation.object) || relation.object}</Text><Text code>{relation.object}</Text></div>
          </div>
          {relation.description && <Paragraph>{relation.description}</Paragraph>}
          <Space wrap>
            <Tag>{relation.id}</Tag>
            <Tag>{t('最少 {n} 个', { n: relation.min_cardinality ?? 0 })}</Tag>
            <Tag>{relation.max_cardinality == null ? t('数量不设上限') : t('最多 {n} 个', { n: relation.max_cardinality })}</Tag>
            {relation.forbidden && <Tag color="red">{t('禁止关系')}</Tag>}
          </Space>
        </Card>
        ))}
      </div>
    </EditableModulePanel>
  ) : (
    <EditableModulePanel module="relations" description={t('当前没有关系，可从已有概念中建立连接。')} onEdit={handleEditModule}>
      <Empty description={t('暂无关系')} />
    </EditableModulePanel>
  );

  const constraintPanel = constraints.length ? (
    <EditableModulePanel
      module="constraints"
      description={t('用结构化表单维护工具参数、输出要求和修正建议。')}
      onEdit={handleEditModule}
    >
      <div className="jx-ontologyDetail-stack">
        {constraints.map((constraint) => {
        const target = constraint.target;
        const targetValue = target.kind === 'output'
          ? target.output_tag
          : [target.tool, target.parameter].filter(Boolean).join(' · ');
        return (
          <Card size="small" key={constraint.id} className="jx-ontologyDetail-ruleCard">
            <div className="jx-ontologyDetail-cardHead">
              <div>
                <Title level={5}>{constraint.name}</Title>
                <Text code>{constraint.id}</Text>
              </div>
              <Space wrap>
                <Tag color={constraint.mode === 'enforce' ? 'red' : 'orange'}>{constraint.mode}</Tag>
                <Tag color={riskColor(constraint.risk)}>{t('{risk} 风险', { risk: constraint.risk ?? 'low' })}</Tag>
                <Tag color={constraint.enabled === false ? 'default' : 'green'}>{constraint.enabled === false ? t('已停用') : t('已启用')}</Tag>
              </Space>
            </div>
            <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
              <Descriptions.Item label={t('约束目标')}>
                <Tag color="blue">{target.kind}</Tag> {targetValue || '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('关联概念')}>{constraint.concept_id || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('要求引用证据')}>{yesNo(constraint.requires_citations)}</Descriptions.Item>
              <Descriptions.Item label={t('前置工具')}><ValueTags values={constraint.prerequisite_tools} color="green" /></Descriptions.Item>
            </Descriptions>
            <Alert type="warning" showIcon message={constraint.message} description={constraint.suggestion || undefined} />
            <div className="jx-ontologyDetail-schema">
              <Text type="secondary">{t('校验 Schema')}</Text>
              <pre>{JSON.stringify(constraint.schema ?? {}, null, 2)}</pre>
            </div>
          </Card>
        );
        })}
      </div>
    </EditableModulePanel>
  ) : (
    <EditableModulePanel module="constraints" description={t('当前没有执行约束，可按工具或输出目标添加。')} onEdit={handleEditModule}>
      <Empty description={t('暂无约束')} />
    </EditableModulePanel>
  );

  const workflowPanel = workflows.length ? (
    <EditableModulePanel
      module="workflows"
      description={t('维护触发入口、工具边界和交付前评审级别。')}
      onEdit={handleEditModule}
    >
      <div className="jx-ontologyDetail-stack">
        {workflows.map((workflow, index) => (
        <Card size="small" key={workflow.id} className="jx-ontologyDetail-workflowCard">
          <div className="jx-ontologyDetail-workflowIndex">{String(index + 1).padStart(2, '0')}</div>
          <div className="jx-ontologyDetail-workflowBody">
            <div className="jx-ontologyDetail-cardHead">
              <div><Title level={5}>{workflow.name}</Title><Text code>{workflow.id}</Text></div>
              <Space wrap>
                <Tag color="purple">{t('评审：{level}', { level: workflow.review_level ?? 'none' })}</Tag>
                <Tag color={riskColor(workflow.risk)}>{t('{risk} 风险', { risk: workflow.risk ?? 'low' })}</Tag>
              </Space>
            </div>
            <div className="jx-ontologyDetail-line"><Text type="secondary">{t('命中词')}</Text><ValueTags values={workflow.triggers} color="blue" /></div>
            <div className="jx-ontologyDetail-line">
              <Text type="secondary">{t('资产触发')}</Text>
              <ValueTags
                values={(workflow.asset_triggers || []).flatMap((trigger) => [
                  ...(trigger.ids || []).map((id) => `${trigger.kind}:${id}`),
                  ...(trigger.tags_any || []).map((tag) => `${trigger.kind}:#${tag}`),
                ])}
                color="cyan"
              />
            </div>
            <div className="jx-ontologyDetail-line"><Text type="secondary">{t('必需工具')}</Text><ValueTags values={workflow.required_tools} color="green" /></div>
            <div className="jx-ontologyDetail-line"><Text type="secondary">{t('禁止工具')}</Text><ValueTags values={workflow.forbidden_tools} color="red" /></div>
            <div className="jx-ontologyDetail-line"><Text type="secondary">{t('输出标签')}</Text><ValueTags values={workflow.output_tags} color="purple" /></div>
          </div>
        </Card>
        ))}
      </div>
    </EditableModulePanel>
  ) : (
    <EditableModulePanel module="workflows" description={t('当前没有工作流，可配置文本或资产触发入口。')} onEdit={handleEditModule}>
      <Empty description={t('暂无工作流')} />
    </EditableModulePanel>
  );

  const validationPanel = selectedVersion ? (
    <div className="jx-ontologyDetail-stack">
      <Alert
        showIcon
        type={selectedVersion.validation_report?.valid ? 'success' : 'error'}
        message={selectedVersion.validation_report?.valid ? t('该版本已通过完整性校验') : t('该版本未通过完整性校验')}
        description={t('这里展示导入版本时保存的校验结果。')}
      />
      <Card size="small" title={t('错误')}><ValidationList items={selectedVersion.validation_report?.errors} kind="error" /></Card>
      <Card size="small" title={t('警告')}><ValidationList items={selectedVersion.validation_report?.warnings} kind="warning" /></Card>
    </div>
  ) : null;

  const rawPanel = document ? (
    <div className="jx-ontologyDetail-raw">
      <div className="jx-ontologyDetail-rawHead">
        <Text type="secondary">{t('原始 JSON 保留全部字段，可用于核对或迁移。')}</Text>
        <Text copyable={{ text: JSON.stringify(document, null, 2) }}>{t('复制完整 JSON')}</Text>
      </div>
      <pre>{JSON.stringify(document, null, 2)}</pre>
    </div>
  ) : null;

  return (
    <>
      <Drawer
      title={pack ? t('{name} · 完整本体', { name: pack.name }) : t('完整本体')}
      open={Boolean(pack)}
      onClose={() => {
        setEditingModule(null);
        onClose();
      }}
      width="min(1120px, calc(100vw - 24px))"
      destroyOnHidden
      className="jx-ontologyDetailDrawer"
      extra={pack && (
        <Select
          aria-label={t('选择本体版本')}
          value={effectiveVersionId}
          onChange={setSelectedVersionId}
          style={{ minWidth: 180 }}
          options={pack.versions.map((version) => ({
            value: version.version_id,
            label: `v${version.version} · ${statusLabel(version.status)}`,
          }))}
        />
      )}
    >
      <Spin spinning={loading}>
        {loadError && <Alert type="error" showIcon message={t('完整本体内容加载失败')} description={loadError} />}
        {!loading && !loadError && !document && <Empty description={t('该领域包暂无可查看版本')} />}
        {document && pack && selectedVersion && (
          <div className="jx-ontologyDetail">
            <div className="jx-ontologyDetail-hero">
              <div className="jx-ontologyDetail-heroIcon"><SafetyCertificateOutlined /></div>
              <div className="jx-ontologyDetail-heroBody">
                <Space size={[8, 8]} wrap>
                  <Tag color="geekblue">{document.domain}</Tag>
                  <Tag color={statusColor(selectedVersion.status)}>
                    v{selectedVersion.version} · {statusLabel(selectedVersion.status)}
                  </Tag>
                  {pack.is_default && <Tag color="gold">{t('默认领域包')}</Tag>}
                  <Tag color={pack.is_enabled ? 'green' : 'default'}>{pack.is_enabled ? t('已启用') : t('已停用')}</Tag>
                  {selectedVersion.validation_report?.valid && <Tag icon={<CheckCircleOutlined />} color="success">{t('校验通过')}</Tag>}
                </Space>
                <Title level={3}>{document.name}</Title>
                <Paragraph>{document.description || pack.description || t('暂无描述')}</Paragraph>
              </div>
            </div>

            <div className="jx-ontologyDetail-stats">
              <div><TagsOutlined /><strong>{concepts.length}</strong><span>{t('概念')}</span></div>
              <div><ShareAltOutlined /><strong>{relations.length}</strong><span>{t('关系')}</span></div>
              <div><SafetyCertificateOutlined /><strong>{constraints.length}</strong><span>{t('约束')}</span></div>
              <div><DeploymentUnitOutlined /><strong>{workflows.length}</strong><span>{t('工作流')}</span></div>
            </div>

            <Tabs
              className="jx-ontologyDetail-tabs"
              items={[
                { key: 'overview', label: t('总览'), children: overview },
                { key: 'concepts', label: t('概念（{n}）', { n: concepts.length }), children: conceptPanel },
                { key: 'relations', label: t('关系（{n}）', { n: relations.length }), children: relationPanel },
                { key: 'constraints', label: t('约束（{n}）', { n: constraints.length }), children: constraintPanel },
                { key: 'workflows', label: t('工作流（{n}）', { n: workflows.length }), children: workflowPanel },
                { key: 'validation', label: t('版本校验'), children: validationPanel },
                { key: 'raw', label: t('原始 JSON'), children: rawPanel },
              ]}
            />
          </div>
        )}
      </Spin>
      </Drawer>
      <OntologyModuleEditor
        token={token}
        apiPrefix={apiPrefix}
        open={Boolean(editingModule)}
        module={editingModule}
        document={document}
        version={selectedVersion ?? null}
        existingVersions={existingVersions}
        onCancel={() => setEditingModule(null)}
        onSaved={async (versionId) => {
          setSelectedVersionId(versionId);
          await onChanged();
        }}
      />
    </>
  );
}
