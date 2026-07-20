import { useState } from 'react';
import {
  Alert,
  Button,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EyeOutlined,
  HistoryOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { t } from '../../i18n';
import { ADMIN_TABLE_PAGINATION } from '../../utils/adminApi';
import { formatDateTime } from '../../utils/date';
import type { OntologyPackSummary, OntologyPackVersion } from './ontologyTypes';

const { Text, Title } = Typography;

interface OntologyVersionManagerProps {
  pack: OntologyPackSummary | null;
  onClose: () => void;
  onView: (pack: OntologyPackSummary, version: OntologyPackVersion) => void;
  onExport: (pack: OntologyPackSummary, version: OntologyPackVersion) => Promise<void>;
  onActivate: (pack: OntologyPackSummary, version: OntologyPackVersion) => Promise<void>;
  onDiscard: (pack: OntologyPackSummary, version: OntologyPackVersion) => Promise<void>;
}

function versionStatus(version: OntologyPackVersion): { color: string; label: string } {
  if (version.status === 'active') return { color: 'green', label: t('已激活') };
  if (version.status === 'retired') return { color: 'default', label: t('已归档') };
  return { color: 'blue', label: t('工作草稿') };
}

export function OntologyVersionManager({
  pack,
  onClose,
  onView,
  onExport,
  onActivate,
  onDiscard,
}: OntologyVersionManagerProps) {
  const [actionKey, setActionKey] = useState('');
  const activeVersion = pack?.versions.find((version) => version.version_id === pack.active_version_id);
  const workingDraft = pack?.versions.find(
    (version) => version.version_id === pack.working_draft_version_id,
  ) ?? pack?.versions.find((version) => version.status === 'draft');
  const officialVersionCount = pack?.versions.filter((version) => version.status !== 'draft').length ?? 0;

  const runAction = async (key: string, action: () => Promise<void>) => {
    setActionKey(key);
    try {
      await action();
    } finally {
      setActionKey('');
    }
  };

  const columns = [
    {
      title: t('版本'),
      key: 'version',
      width: 170,
      render: (_: unknown, version: OntologyPackVersion) => (
        <Space direction="vertical" size={2}>
          <Space size={6}>
            <Text strong>v{version.version}</Text>
            {version.version_id === pack?.active_version_id && <Tag color="green">{t('当前运行')}</Tag>}
          </Space>
          <Text type="secondary" className="jx-ontologyVersionManager-versionId" copyable>
            {version.version_id}
          </Text>
        </Space>
      ),
    },
    {
      title: t('状态'),
      key: 'status',
      width: 96,
      render: (_: unknown, version: OntologyPackVersion) => {
        const status = versionStatus(version);
        return <Tag color={status.color}>{status.label}</Tag>;
      },
    },
    {
      title: t('完整性校验'),
      key: 'validation',
      width: 150,
      render: (_: unknown, version: OntologyPackVersion) => {
        const report = version.validation_report ?? {};
        const errors = report.errors?.length ?? 0;
        const warnings = report.warnings?.length ?? 0;
        return (
          <Space direction="vertical" size={3}>
            <Tag
              color={report.valid ? 'success' : 'error'}
              icon={report.valid ? <CheckCircleOutlined /> : <WarningOutlined />}
            >
              {report.valid ? t('校验通过') : t('校验未通过')}
            </Tag>
            {(errors > 0 || warnings > 0) && (
              <Text type="secondary" className="jx-ontologyVersionManager-issueCount">
                {t('{errors} 个错误，{warnings} 个警告', { errors, warnings })}
              </Text>
            )}
          </Space>
        );
      },
    },
    {
      title: t('时间'),
      key: 'time',
      width: 190,
      render: (_: unknown, version: OntologyPackVersion) => (
        <Space direction="vertical" size={2}>
          <Text type="secondary">
            {version.status === 'draft' ? t('更新于') : t('创建于')}{' '}
            {formatDateTime(version.updated_at || version.created_at)}
          </Text>
          {version.activated_at && (
            <Text type="secondary">{t('激活于')} {formatDateTime(version.activated_at)}</Text>
          )}
        </Space>
      ),
    },
    {
      title: t('操作'),
      key: 'actions',
      width: 330,
      fixed: 'right' as const,
      render: (_: unknown, version: OntologyPackVersion) => (
        <Space size={4} wrap={false}>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => pack && onView(pack, version)}
          >
            {t('查看内容')}
          </Button>
          <Button
            type="link"
            size="small"
            icon={<DownloadOutlined />}
            loading={actionKey === `export:${version.version_id}`}
            onClick={() => pack && void runAction(
              `export:${version.version_id}`,
              () => onExport(pack, version),
            )}
          >
            {t('下载')}
          </Button>
          {version.status !== 'active' && (
            <Tooltip title={!version.validation_report?.valid ? t('未通过校验的版本不能激活') : undefined}>
              <span>
                <Popconfirm
                  title={version.status === 'draft'
                    ? t('发布工作草稿 v{version}？', { version: version.version })
                    : t('激活版本 v{version}？', { version: version.version })}
                  description={version.status === 'draft'
                    ? t('发布后草稿将锁定为正式版本，并立即替换当前运行版本。')
                    : t('激活后将立即替换当前运行版本。')}
                  disabled={!version.validation_report?.valid}
                  onConfirm={() => pack && runAction(
                    `activate:${version.version_id}`,
                    () => onActivate(pack, version),
                  )}
                >
                  <Button
                    type="link"
                    size="small"
                    icon={<ThunderboltOutlined />}
                    disabled={!version.validation_report?.valid}
                    loading={actionKey === `activate:${version.version_id}`}
                  >
                    {version.status === 'draft' ? t('发布') : t('激活')}
                  </Button>
                </Popconfirm>
              </span>
            </Tooltip>
          )}
          {version.version_id === workingDraft?.version_id && (
            <Popconfirm
              title={t('放弃工作草稿 v{version}？', { version: version.version })}
              description={t('未发布的草稿内容将被删除，此操作无法撤销。')}
              okText={t('放弃草稿')}
              okButtonProps={{ danger: true }}
              onConfirm={() => pack && runAction(
                `discard:${version.version_id}`,
                () => onDiscard(pack, version),
              )}
            >
              <Button
                type="link"
                danger
                size="small"
                icon={<DeleteOutlined />}
                loading={actionKey === `discard:${version.version_id}`}
              >
                {t('放弃')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Modal
      title={pack ? t('版本管理 · {name}', { name: pack.name }) : t('版本管理')}
      open={Boolean(pack)}
      onCancel={onClose}
      footer={<Button onClick={onClose}>{t('关闭')}</Button>}
      width="min(980px, calc(100vw - 32px))"
      destroyOnHidden
      className="jx-ontologyVersionManager"
      styles={{ body: { maxHeight: 'calc(100vh - 190px)', overflowY: 'auto' } }}
    >
      {pack && (
        <div className="jx-ontologyVersionManager-content">
          <div className="jx-ontologyVersionManager-summary">
            <div className="jx-ontologyVersionManager-summaryIcon"><HistoryOutlined /></div>
            <div className="jx-ontologyVersionManager-summaryBody">
              <Title level={5}>
                {workingDraft
                  ? t('{official} 个正式版本，1 个工作草稿', { official: officialVersionCount })
                  : t('共 {n} 个正式版本', { n: officialVersionCount })}
              </Title>
              <Text type="secondary">
                {activeVersion
                  ? t('当前运行 v{version}。可以查看任一历史版本，或激活已通过校验的版本。', { version: activeVersion.version })
                  : t('当前没有运行中的版本，请激活一个已通过校验的版本。')}
              </Text>
            </div>
          </div>
          {!activeVersion && (
            <Alert
              showIcon
              type="warning"
              message={t('领域包尚未激活版本')}
              description={t('未激活版本时，该领域包不会进入运行时。')}
            />
          )}
          <Table<OntologyPackVersion>
            rowKey="version_id"
            dataSource={pack.versions}
            columns={columns}
            pagination={{
              ...ADMIN_TABLE_PAGINATION,
              pageSize: 10,
              pageSizeOptions: [10, 20, 50],
            }}
            size="middle"
            scroll={{ x: 936 }}
          />
          <Text type="secondary" className="jx-ontologyVersionManager-footnote">
            {t('每个领域包只保留一个可反复编辑的工作草稿；发布后版本锁定，历史版本全部保留。')}
          </Text>
        </div>
      )}
    </Modal>
  );
}
