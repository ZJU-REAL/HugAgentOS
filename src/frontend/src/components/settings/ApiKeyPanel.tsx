import { useCallback, useEffect, useState } from 'react';
import { Button, Checkbox, Empty, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Typography, message } from 'antd';
import { CopyOutlined, KeyOutlined, PlusOutlined } from '@ant-design/icons';
import {
  createApiKey, listApiKeys, revealApiKey, revokeApiKey, toggleApiKey, type ApiKeyItem,
} from '../../api';
import { useFlashKey } from '../../hooks/useFlash';
import { t, tCtx } from '../../i18n';
import { useEditionStore } from '../../stores';
import { copyToClipboard } from '../../utils/clipboard';

const { Text, Paragraph } = Typography;

// Expiry options (days). null = never expires.
const EXPIRY_OPTIONS: { label: string; value: number | 'never' }[] = [
  { label: t('7 天'), value: 7 },
  { label: t('30 天'), value: 30 },
  { label: t('90 天'), value: 90 },
  { label: t('180 天'), value: 180 },
  { label: t('365 天'), value: 365 },
  { label: t('永不过期'), value: 'never' },
];

function fmtDate(s?: string | null): string {
  if (!s) return '—';
  try {
    return new Date(s).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return s;
  }
}

export function ApiKeyPanel() {
  const [items, setItems] = useState<ApiKeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState('');
  const [expiry, setExpiry] = useState<number | 'never'>(30);
  const [forGateway, setForGateway] = useState(false);
  const [creating, setCreating] = useState(false);
  const [plaintext, setPlaintext] = useState<string | null>(null);
  // The key id of the row currently being "copied again" (revealing) in the list, used for that row's copy button loading state
  const [copyingId, setCopyingId] = useState<string | null>(null);
  // The id of the just-created Key: the matching row gets a one-shot background flash (CSS primitive class, no motion element wrapper)
  const { flashKey, flash } = useFlashKey(1500);
  const modelGatewayEnabled = useEditionStore((s) => (s.loaded ? !!s.features.model_gateway : false));

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listApiKeys());
    } catch (e) {
      message.error(t('加载 API-Key 失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  useEffect(() => {
    if (!modelGatewayEnabled && forGateway) {
      setForGateway(false);
    }
  }, [modelGatewayEnabled, forGateway]);

  const handleCreate = useCallback(async () => {
    setCreating(true);
    try {
      const expiresInDays = expiry === 'never' ? null : expiry;
      const created = await createApiKey(name.trim() || 'API Key', expiresInDays, modelGatewayEnabled && forGateway);
      setCreateOpen(false);
      setName('');
      setExpiry(30);
      setForGateway(false);
      setPlaintext(created.api_key ?? null);
      await reload();
      flash(created.id);
    } catch (e) {
      message.error(t('创建失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setCreating(false);
    }
  }, [name, expiry, modelGatewayEnabled, forGateway, reload, flash]);

  const handleToggle = useCallback(async (row: ApiKeyItem, enabled: boolean) => {
    setItems((prev) => prev.map((k) => k.id === row.id ? { ...k, enabled } : k));
    try {
      await toggleApiKey(row.id, enabled);
    } catch (e) {
      setItems((prev) => prev.map((k) => k.id === row.id ? { ...k, enabled: !enabled } : k));
      message.error(t('操作失败：{msg}', { msg: (e as Error).message }));
    }
  }, []);

  const handleRevoke = useCallback(async (row: ApiKeyItem) => {
    try {
      await revokeApiKey(row.id);
      message.success(t('已撤销'));
      await reload();
    } catch (e) {
      message.error(t('撤销失败：{msg}', { msg: (e as Error).message }));
    }
  }, [reload]);

  const copyPlaintext = useCallback(async () => {
    if (!plaintext) return;
    if (await copyToClipboard(plaintext)) {
      message.success(t('已复制到剪贴板'));
    } else {
      message.warning(t('复制失败，请手动选择文本复制'));
    }
  }, [plaintext]);

  // "Copy again" in the list: decrypt on demand from the backend to retrieve the full plaintext and write it to the clipboard (plaintext is not kept in frontend state)
  const copyExisting = useCallback(async (row: ApiKeyItem) => {
    setCopyingId(row.id);
    try {
      const raw = await revealApiKey(row.id);
      if (!raw) {
        message.warning(t('未能取回密钥明文，请撤销后新建'));
        return;
      }
      if (await copyToClipboard(raw)) {
        message.success(t('已复制到剪贴板'));
      } else {
        message.warning(t('复制失败，请手动选择文本复制'));
      }
    } catch (e) {
      message.error(t('复制失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setCopyingId(null);
    }
  }, []);

  const columns = [
    {
      title: t('名称'),
      dataIndex: 'name',
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: 'Key',
      dataIndex: 'key_prefix',
      render: (v: string, row: ApiKeyItem) => (
        <Space size={4}>
          <Text code>{v}…</Text>
          {row.revealable && (
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              loading={copyingId === row.id}
              onClick={() => void copyExisting(row)}
              title={t('复制完整密钥')}
            />
          )}
        </Space>
      ),
    },
    {
      title: t('过期时间'),
      dataIndex: 'expires_at',
      render: (v: string | null) => v ? fmtDate(v) : <Tag>{t('永不过期')}</Tag>,
    },
    {
      title: t('最近使用'),
      dataIndex: 'last_used_at',
      render: (v: string | null) => fmtDate(v),
    },
    {
      title: tCtx('state', '启用'),
      dataIndex: 'enabled',
      width: 80,
      render: (_: unknown, row: ApiKeyItem) => (
        <Switch size="small" checked={row.enabled} onChange={(c) => handleToggle(row, c)} />
      ),
    },
    {
      title: t('操作'),
      width: 90,
      render: (_: unknown, row: ApiKeyItem) => (
        <Popconfirm title={t('撤销后立即失效且不可恢复，确定？')} okText={t('撤销')} cancelText={t('取消')} okButtonProps={{ danger: true }} onConfirm={() => handleRevoke(row)}>
          <Button type="link" danger size="small">{t('撤销')}</Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div className="jx-apikey-panel">
      <div className="jx-apikey-intro" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
        <Text type="secondary" style={{ fontSize: 13 }}>
          {t('使用 API-Key 可在你自己的程序里以当前账号身份调用智能体，继承你已启用的全部能力。')}
        </Text>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)} style={{ flexShrink: 0 }}>{t('新建 Key')}</Button>
      </div>

      {items.length === 0 && !loading ? (
        <div key="apikey-empty" className="jx-anim-fadeIn">
          <Empty image={<KeyOutlined style={{ fontSize: 32, color: 'var(--color-text-placeholder)' }} />} description={t('还没有 API-Key')} />
        </div>
      ) : (
        <div key="apikey-table" className="jx-anim-fadeIn">
          <Table
            rowKey="id"
            size="small"
            loading={loading}
            dataSource={items}
            columns={columns}
            pagination={false}
            rowClassName={(row: ApiKeyItem) => (row.id === flashKey ? 'jx-anim-flash-row' : '')}
          />
        </div>
      )}

      {/* API usage instructions (page-level, persistent; no longer written inside individual Key cards/modals) */}
      <div
        className="jx-apikey-usage"
        style={{ marginTop: 20, padding: '12px 14px', background: 'var(--color-fill-quaternary)', borderRadius: 8, fontSize: 12, color: 'var(--color-text-tertiary)', lineHeight: 1.9 }}
      >
        <Text strong style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}>{t('接口调用说明')}</Text>
        <Paragraph style={{ margin: '6px 0 8px', fontSize: 12, color: 'inherit' }}>
          {t('所有接口均在请求头携带')}<Text code style={{ margin: '0 4px' }}>Authorization: Bearer &lt;{t('你的Key')}&gt;</Text>{t('（Anthropic 客户端也可用')}<Text code>x-api-key</Text>{t('头）。')}
        </Paragraph>
        <div>
          <Text strong style={{ color: 'inherit' }}>{t('智能体（原生）')}</Text>：
          <Text code copyable>{`POST ${window.location.origin}/api/v1/chats/stream`}</Text>
        </div>
        {modelGatewayEnabled && (
          <>
            <div style={{ marginTop: 6 }}>
              <Text strong style={{ color: 'inherit' }}>OpenAI {t('兼容')}</Text>
              {t('（Cherry Studio 等，Base URL）')}：
              <Text code copyable>{`${window.location.origin}/gateway/v1`}</Text>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>→ POST /v1/chat/completions</Text>
            </div>
            <div style={{ marginTop: 6 }}>
              <Text strong style={{ color: 'inherit' }}>Anthropic {t('兼容')}</Text>
              {t('（Claude Code 等，Base URL）')}：
              <Text code copyable>{`${window.location.origin}/gateway/anthropic`}</Text>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>→ SDK {t('自动追加')} /v1/messages</Text>
            </div>
            <Paragraph style={{ marginTop: 8, marginBottom: 0, fontSize: 12, color: 'inherit' }}>
              {t('OpenAI / Anthropic 兼容格式需在创建密钥时勾选「对外模型网关」后方可使用。')}
            </Paragraph>
          </>
        )}
      </div>

      {/* Create modal */}
      <Modal
        title={t('新建 API-Key')}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void handleCreate()}
        okText={t('创建')}
        cancelText={t('取消')}
        confirmLoading={creating}
        destroyOnHidden
      >
        <div style={{ marginBottom: 12 }}>
          <div style={{ marginBottom: 6 }}>{t('名称')}</div>
          <Input
            value={name}
            maxLength={128}
            placeholder={t('便于识别，如「我的自动化脚本」')}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <div style={{ marginBottom: 6 }}>{t('过期时间')}</div>
          <Select
            style={{ width: '100%' }}
            value={expiry}
            onChange={(v) => setExpiry(v)}
            options={EXPIRY_OPTIONS}
          />
        </div>
        {modelGatewayEnabled && (
          <div style={{ marginTop: 12 }}>
            <Checkbox checked={forGateway} onChange={(e) => setForGateway(e.target.checked)}>
              {t('同时用于对外模型网关（可在 Cherry Studio 等用此密钥直接调用模型）')}
            </Checkbox>
          </div>
        )}
      </Modal>

      {/* One-time plaintext display modal */}
      <Modal
        title={t('请立即复制并妥善保存')}
        open={plaintext !== null}
        onCancel={() => setPlaintext(null)}
        footer={[
          <Button key="ok" type="primary" onClick={() => setPlaintext(null)}>{t('我已保存')}</Button>,
        ]}
        destroyOnHidden
      >
        <Paragraph type="warning" style={{ marginBottom: 8 }}>
          {t('请立即复制并妥善保存。关闭后也可在列表中点「复制」再次取回完整密钥。')}
        </Paragraph>
        <Space.Compact style={{ width: '100%' }}>
          <Input readOnly value={plaintext ?? ''} />
          <Button icon={<CopyOutlined />} onClick={() => void copyPlaintext()}>{t('复制')}</Button>
        </Space.Compact>
        <Paragraph style={{ marginTop: 12, marginBottom: 0, fontSize: 12, color: 'var(--color-text-tertiary)' }}>
          {t(modelGatewayEnabled ? '调用方式（原生 / OpenAI / Anthropic 兼容）见本页下方「接口调用说明」。' : '调用方式见本页下方「接口调用说明」。')}
        </Paragraph>
      </Modal>
    </div>
  );
}
