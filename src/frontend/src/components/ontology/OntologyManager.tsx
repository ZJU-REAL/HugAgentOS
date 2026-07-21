import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Input,
  Modal,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  EyeOutlined,
  HistoryOutlined,
  ImportOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { adminFetch, ADMIN_TABLE_PAGINATION } from '../../utils/adminApi';
import { t } from '../../i18n';
import { OntologyDetailDrawer } from './OntologyDetailDrawer';
import { OntologyVersionManager } from './OntologyVersionManager';
import type { OntologyPackSummary, OntologyPackVersion } from './ontologyTypes';

const { Paragraph, Text } = Typography;
const { TextArea } = Input;

type PackVersion = OntologyPackVersion;
type OntologyPack = OntologyPackSummary;

interface AuditEvent {
  event_id: string;
  created_at: string;
  stage: string;
  target?: string;
  decision: string;
  rule_id?: string;
  latency_ms?: number;
  details?: Record<string, unknown>;
}

interface ReviewRun {
  review_id: string;
  created_at: string;
  level: string;
  verdict: string;
  latency_ms?: number;
  evidence?: string[];
  feedback?: string;
}

interface OntologyDraft {
  draft_id: string;
  pack_id: string;
  candidate_type: string;
  value_score: number;
  review_status: string;
  proposal: Record<string, unknown>;
  evidence: unknown[];
}

interface GovernanceMetrics {
  events_total: number;
  reviews_total: number;
  drafts_total: number;
  materialized_total: number;
  event_decisions: Record<string, number>;
  review_verdicts: Record<string, number>;
  draft_statuses: Record<string, number>;
  source_acceptance: Record<string, unknown>;
  daily_30d: Array<Record<string, unknown>>;
}

function verdictColor(value: string): string {
  if (value === 'pass' || value === 'active' || value === 'approved') return 'green';
  if (value === 'deny' || value === 'escalate' || value === 'rejected') return 'red';
  if (value === 'revise' || value === 'log') return 'orange';
  return 'default';
}

interface OntologyManagerProps {
  token?: string;
  apiPrefix?: string;
  onChanged?: () => Promise<void> | void;
}

export function OntologyManager({
  token = '',
  apiPrefix = '/v1/admin/ontologies',
  onChanged,
}: OntologyManagerProps) {
  const [packs, setPacks] = useState<OntologyPack[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [reviews, setReviews] = useState<ReviewRun[]>([]);
  const [drafts, setDrafts] = useState<OntologyDraft[]>([]);
  const [metrics, setMetrics] = useState<GovernanceMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importActivate, setImportActivate] = useState(false);
  const [importing, setImporting] = useState(false);
  const [detail, setDetail] = useState<{ title: string; value: unknown } | null>(null);
  const [detailPack, setDetailPack] = useState<OntologyPack | null>(null);
  const [detailVersionId, setDetailVersionId] = useState<string>();
  const [versionPack, setVersionPack] = useState<OntologyPack | null>(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [packResp, eventResp, reviewResp, draftResp, metricsResp] = await Promise.all([
        adminFetch(token, apiPrefix),
        adminFetch(token, `${apiPrefix}/events?limit=100`),
        adminFetch(token, `${apiPrefix}/reviews?limit=100`),
        adminFetch(token, `${apiPrefix}/drafts?limit=100`),
        adminFetch(token, `${apiPrefix}/metrics`),
      ]);
      const nextPacks = ((packResp?.data ?? packResp)?.items ?? []) as OntologyPack[];
      setPacks(nextPacks);
      setDetailPack((current) => (
        current ? nextPacks.find((item) => item.pack_id === current.pack_id) ?? null : null
      ));
      setVersionPack((current) => (
        current ? nextPacks.find((item) => item.pack_id === current.pack_id) ?? null : null
      ));
      setEvents((eventResp?.data ?? eventResp)?.items ?? []);
      setReviews((reviewResp?.data ?? reviewResp)?.items ?? []);
      setDrafts((draftResp?.data ?? draftResp)?.items ?? []);
      setMetrics((metricsResp?.data ?? metricsResp) ?? null);
      await onChanged?.();
    } catch (err) {
      message.error(t('加载本体资产失败：{msg}', { msg: (err as Error)?.message || String(err) }));
    } finally {
      setLoading(false);
    }
  }, [apiPrefix, onChanged, token]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const updateFlags = async (pack: OntologyPack, patch: { is_enabled?: boolean; is_default?: boolean }) => {
    try {
      await adminFetch(token, `${apiPrefix}/${encodeURIComponent(pack.pack_id)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('更新失败'));
    }
  };

  const activateVersion = async (pack: OntologyPack, version: PackVersion) => {
    try {
      await adminFetch(
        token,
        `${apiPrefix}/${encodeURIComponent(pack.pack_id)}/versions/${encodeURIComponent(version.version_id)}/activate`,
        { method: 'POST' },
      );
      message.success(version.status === 'draft' ? t('工作草稿已发布') : t('本体版本已激活'));
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('激活失败'));
    }
  };

  const exportVersion = async (pack: OntologyPack, version: PackVersion) => {
    try {
      const resp = await adminFetch(
        token,
        `${apiPrefix}/${encodeURIComponent(pack.pack_id)}/versions/${encodeURIComponent(version.version_id)}/export`,
      );
      const content = resp?.data ?? resp;
      const blob = new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${pack.pack_id}-${version.version}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      message.error((err as Error)?.message || t('导出失败'));
    }
  };

  const discardDraft = async (pack: OntologyPack, version: PackVersion) => {
    try {
      await adminFetch(
        token,
        `${apiPrefix}/${encodeURIComponent(pack.pack_id)}/draft/${encodeURIComponent(version.version_id)}`,
        { method: 'DELETE' },
      );
      message.success(t('工作草稿已放弃'));
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('放弃草稿失败'));
    }
  };

  const importPack = async () => {
    let document: Record<string, unknown>;
    try {
      document = JSON.parse(importText) as Record<string, unknown>;
    } catch {
      message.error(t('请输入合法 JSON'));
      return;
    }
    setImporting(true);
    try {
      const validation = await adminFetch(token, `${apiPrefix}/validate`, {
        method: 'POST',
        body: JSON.stringify(document),
      });
      const report = validation?.data ?? validation;
      if (!report?.valid) {
        setDetail({ title: t('Domain Pack 校验报告'), value: report });
        return;
      }
      await adminFetch(token, `${apiPrefix}/versions`, {
        method: 'POST',
        body: JSON.stringify({ document, activate: importActivate }),
      });
      message.success(t('Domain Pack 已导入'));
      setImportOpen(false);
      setImportText('');
      setImportActivate(false);
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('导入失败'));
    } finally {
      setImporting(false);
    }
  };

  const reviewDraft = async (draft: OntologyDraft, approved: boolean) => {
    try {
      await adminFetch(token, `${apiPrefix}/drafts/${draft.draft_id}/review`, {
        method: 'POST',
        body: JSON.stringify({ approved, comment: approved ? '通过人工一致性审查' : '人工审查不通过' }),
      });
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('审核失败'));
    }
  };

  const generateDrafts = async () => {
    try {
      const resp = await adminFetch(token, `${apiPrefix}/evolution/generate`, {
        method: 'POST',
        body: JSON.stringify({ min_occurrences: 3, limit: 500 }),
      });
      const data = resp?.data ?? resp;
      message.success(t('已生成 {n} 条演进草案', { n: data?.created ?? 0 }));
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('生成草案失败'));
    }
  };

  const materializeDraft = async (draft: OntologyDraft) => {
    try {
      await adminFetch(token, `${apiPrefix}/drafts/${draft.draft_id}/materialize`, {
        method: 'POST',
      });
      message.success(t('已合并到工作草稿'));
      await loadAll();
    } catch (err) {
      message.error((err as Error)?.message || t('合并草稿失败'));
    }
  };

  const packColumns = [
    {
      title: t('领域包'),
      key: 'name',
      render: (_: unknown, row: OntologyPack) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.name}</Text>
          <Text type="secondary" copyable>{row.pack_id}</Text>
        </Space>
      ),
    },
    { title: t('领域'), dataIndex: 'domain', key: 'domain' },
    {
      title: t('当前版本'),
      key: 'active',
      render: (_: unknown, row: OntologyPack) => {
        const active = row.versions.find((item) => item.version_id === row.active_version_id);
        return active ? <Tag color="green">v{active.version}</Tag> : <Tag>{t('未激活')}</Tag>;
      },
    },
    {
      title: t('启用'),
      key: 'enabled',
      render: (_: unknown, row: OntologyPack) => (
        <Switch size="small" checked={row.is_enabled} onChange={(value) => void updateFlags(row, { is_enabled: value })} />
      ),
    },
    {
      title: t('默认'),
      key: 'default',
      render: (_: unknown, row: OntologyPack) => (
        <Switch size="small" checked={row.is_default} onChange={(value) => void updateFlags(row, { is_default: value })} />
      ),
    },
    {
      title: t('版本治理'),
      key: 'versions',
      width: 250,
      render: (_: unknown, row: OntologyPack) => (
        <Space size="small">
          <Button
            size="small"
            icon={<HistoryOutlined />}
            onClick={() => setVersionPack(row)}
          >
            {t('版本管理（{n}）', { n: row.versions.length })}
          </Button>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => {
              setDetailVersionId(undefined);
              setDetailPack(row);
            }}
          >
            {t('详情')}
          </Button>
        </Space>
      ),
    },
  ];

  const eventColumns = [
    { title: t('时间'), dataIndex: 'created_at', key: 'created_at', width: 190 },
    { title: t('阶段'), dataIndex: 'stage', key: 'stage', width: 100 },
    { title: t('目标'), dataIndex: 'target', key: 'target' },
    { title: t('规则'), dataIndex: 'rule_id', key: 'rule_id' },
    { title: t('决策'), dataIndex: 'decision', key: 'decision', render: (v: string) => <Tag color={verdictColor(v)}>{v}</Tag> },
    { title: t('延迟'), dataIndex: 'latency_ms', key: 'latency_ms', render: (v?: number) => v == null ? '-' : `${v} ms` },
    { title: '', key: 'detail', render: (_: unknown, row: AuditEvent) => <Button size="small" onClick={() => setDetail({ title: row.event_id, value: row })}>{t('证据')}</Button> },
  ];

  const reviewColumns = [
    { title: t('时间'), dataIndex: 'created_at', key: 'created_at', width: 190 },
    { title: t('级别'), dataIndex: 'level', key: 'level' },
    { title: t('结论'), dataIndex: 'verdict', key: 'verdict', render: (v: string) => <Tag color={verdictColor(v)}>{v}</Tag> },
    { title: t('延迟'), dataIndex: 'latency_ms', key: 'latency_ms', render: (v?: number) => v == null ? '-' : `${v} ms` },
    { title: '', key: 'detail', render: (_: unknown, row: ReviewRun) => <Button size="small" onClick={() => setDetail({ title: row.review_id, value: row })}>{t('证据')}</Button> },
  ];

  const draftColumns = [
    { title: t('领域包'), dataIndex: 'pack_id', key: 'pack_id' },
    { title: t('候选类型'), dataIndex: 'candidate_type', key: 'candidate_type' },
    { title: t('价值分'), dataIndex: 'value_score', key: 'value_score' },
    { title: t('状态'), dataIndex: 'review_status', key: 'review_status', render: (v: string) => <Tag color={verdictColor(v)}>{v}</Tag> },
    {
      title: t('操作'), key: 'actions', render: (_: unknown, row: OntologyDraft) => (
        <Space>
          <Button size="small" onClick={() => setDetail({ title: row.draft_id, value: row })}>{t('详情')}</Button>
          {row.review_status === 'pending' && <>
            <Button size="small" type="primary" onClick={() => void reviewDraft(row, true)}>{t('通过')}</Button>
            <Button size="small" danger onClick={() => void reviewDraft(row, false)}>{t('驳回')}</Button>
          </>}
          {row.review_status === 'approved'
            && row.candidate_type !== 'false_positive'
            && !row.proposal.materialized_version_id && (
            <Button size="small" onClick={() => void materializeDraft(row)}>{t('合并到工作草稿')}</Button>
          )}
          {Boolean(row.proposal.materialized_version_id) && <Tag color="blue">{t('已合并草稿')}</Tag>}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Alert
        showIcon
        type="info"
        message={t('本体资产中心')}
        description={t('Domain Pack 版本发布后才会进入运行时；规则演进草案必须人工审核，审核通过也不会自动发布。')}
      />
      <Space>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadAll()}>{t('刷新')}</Button>
        <Button type="primary" icon={<ImportOutlined />} onClick={() => setImportOpen(true)}>{t('导入 Domain Pack')}</Button>
        <Button onClick={() => void generateDrafts()}>{t('从运行证据生成草案')}</Button>
      </Space>
      <Table<OntologyPack>
        rowKey="pack_id"
        loading={loading}
        dataSource={packs}
        columns={packColumns}
        pagination={ADMIN_TABLE_PAGINATION}
      />
      <Tabs items={[
        {
          key: 'metrics',
          label: t('闭环指标'),
          children: metrics ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions bordered size="small" column={4}>
                <Descriptions.Item label={t('门禁事件')}>{metrics.events_total}</Descriptions.Item>
                <Descriptions.Item label={t('评审次数')}>{metrics.reviews_total}</Descriptions.Item>
                <Descriptions.Item label={t('演进草案')}>{metrics.drafts_total}</Descriptions.Item>
                <Descriptions.Item label={t('已合并候选')}>{metrics.materialized_total}</Descriptions.Item>
              </Descriptions>
              <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                {JSON.stringify({
                  event_decisions: metrics.event_decisions,
                  review_verdicts: metrics.review_verdicts,
                  draft_statuses: metrics.draft_statuses,
                  source_acceptance: metrics.source_acceptance,
                  daily_30d: metrics.daily_30d,
                }, null, 2)}
              </pre>
            </Space>
          ) : <Text type="secondary">{t('暂无指标数据')}</Text>,
        },
        { key: 'events', label: t('门禁审计'), children: <Table rowKey="event_id" dataSource={events} columns={eventColumns} pagination={ADMIN_TABLE_PAGINATION} /> },
        { key: 'reviews', label: t('评审记录'), children: <Table rowKey="review_id" dataSource={reviews} columns={reviewColumns} pagination={ADMIN_TABLE_PAGINATION} /> },
        { key: 'drafts', label: t('演进草案'), children: <Table rowKey="draft_id" dataSource={drafts} columns={draftColumns} pagination={ADMIN_TABLE_PAGINATION} /> },
      ]} />

      <Modal
        title={t('导入 Domain Pack')}
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={() => void importPack()}
        okText={t('校验并导入')}
        confirmLoading={importing}
        width={760}
      >
        <Paragraph type="secondary">{t('粘贴完整的 Domain Pack JSON。每个领域包同时只能有一个工作草稿；勾选后可立即发布。')}</Paragraph>
        <TextArea rows={18} value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="{ ... }" />
        <Space style={{ marginTop: 12 }}>
          <Switch checked={importActivate} onChange={setImportActivate} />
          <Text>{t('导入后立即发布')}</Text>
        </Space>
      </Modal>

      <OntologyVersionManager
        pack={versionPack}
        onClose={() => setVersionPack(null)}
        onExport={exportVersion}
        onActivate={activateVersion}
        onDiscard={discardDraft}
        onView={(pack, version) => {
          setVersionPack(null);
          setDetailVersionId(version.version_id);
          setDetailPack(pack);
        }}
      />

      <OntologyDetailDrawer
        key={`${detailPack?.pack_id ?? 'closed'}:${detailVersionId ?? 'default'}`}
        token={token}
        apiPrefix={apiPrefix}
        pack={detailPack}
        initialVersionId={detailVersionId}
        onClose={() => {
          setDetailPack(null);
          setDetailVersionId(undefined);
        }}
        onChanged={loadAll}
      />

      <Drawer title={detail?.title} open={!!detail} onClose={() => setDetail(null)} width={720}>
        {detail && <>
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label={t('结构化内容')}>
              <CheckCircleOutlined style={{ color: '#52c41a' }} /> JSON
            </Descriptions.Item>
          </Descriptions>
          <pre style={{ marginTop: 16, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {JSON.stringify(detail.value, null, 2)}
          </pre>
        </>}
      </Drawer>
    </Space>
  );
}
