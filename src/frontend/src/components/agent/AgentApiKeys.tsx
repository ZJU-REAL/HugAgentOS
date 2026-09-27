import { useState } from 'react';
import { Alert, Button, Empty, Input, Popconfirm, Select, Space, Switch, Table, Tag, Typography, message } from 'antd';
import { CopyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { createAgentApiKey, revealAgentApiKey, revokeAgentApiKey, toggleAgentApiKey, type AgentApiKey } from '../../api/agentApi';
import { copyToClipboard } from '../../utils/clipboard';
import { formatDateTime } from '../../utils/date';
import { CopyButton } from '../common/CopyButton';
import { useAgentApiKeys } from './useAgentApiKeys';
import { t } from '../../i18n';

export function AgentApiKeys({ agentId, canCreate }: { agentId: string; canCreate: boolean }) {
  const { keys, loading, error, pending, reload, perform } = useAgentApiKeys(agentId);
  const [name, setName] = useState('');
  const [expiry, setExpiry] = useState<number | 'never'>(30);
  const [plaintext, setPlaintext] = useState('');
  const create = () => canCreate && void perform('create', (signal) => createAgentApiKey(
    agentId, name.trim() || 'API Key', expiry === 'never' ? null : expiry, signal,
  ), async (key) => {
    setPlaintext(key.api_key || '');
    setName('');
    message.success(t('API-Key 已创建'));
    await reload();
  });
  const change = (key: AgentApiKey, enabled: boolean) => void perform(key.id,
    signal => toggleAgentApiKey(agentId, key.id, enabled, signal), async () => {
      message.success(t('已更新'));
      await reload();
    });
  const revoke = (key: AgentApiKey) => perform(key.id,
    signal => revokeAgentApiKey(agentId, key.id, signal), async () => {
      setPlaintext('');
      message.success(t('已撤销'));
      await reload();
    });
  const reveal = (key: AgentApiKey) => void perform(key.id,
    signal => revealAgentApiKey(agentId, key.id, signal), async (raw) => {
      if (!raw) { message.warning(t('未能取回密钥明文，请撤销后新建')); return; }
      if (await copyToClipboard(raw)) message.success(t('已复制到剪贴板'));
      else {
        setPlaintext(raw);
        message.warning(t('复制失败，请手动选择文本复制'));
      }
    });
  return (
    <div className="jx-agentApi-section">
      <form className="jx-agentApi-create" onSubmit={event => { event.preventDefault(); create(); }}>
        <label className="jx-agentApi-field">
          <span>{t('名称')}</span>
          <Input aria-label={t('密钥名称')} value={name} maxLength={128}
            placeholder={t('便于识别，如「我的自动化脚本」')} onChange={event => setName(event.target.value)} />
        </label>
        <label className="jx-agentApi-field">
          <span>{t('过期时间')}</span>
          <Select aria-label={t('过期时间')} value={expiry} onChange={setExpiry}
            options={[
              { value: 7, label: t('7 天') }, { value: 30, label: t('30 天') },
              { value: 90, label: t('90 天') }, { value: 180, label: t('180 天') },
              { value: 365, label: t('365 天') }, { value: 'never', label: t('永不过期') },
            ]} />
        </label>
        <Button type="primary" htmlType="submit" icon={<PlusOutlined />} loading={pending.has('create')} disabled={!canCreate}>
          {t('新建 Key')}
        </Button>
      </form>
      {plaintext && <Alert type="success" showIcon
        title={t('请立即复制并妥善保存')}
        description={<div className="jx-agentApi-secret">
          <Input aria-label={t('完整密钥')} readOnly value={plaintext} autoComplete="off" />
          <CopyButton text={plaintext}>{t('复制')}</CopyButton>
          <Button onClick={() => setPlaintext('')}>{t('我已保存')}</Button>
        </div>} />}
      <div className="jx-agentApi-toolbar">
        <Typography.Text type="secondary">{t('密钥绑定此智能体，关闭弹窗会清除本次显示的明文。')}</Typography.Text>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void reload()}>{t('刷新')}</Button>
      </div>
      {error ? <Alert type="error" showIcon title={t('加载 API-Key 失败：{msg}', { msg: error })}
        action={<Button onClick={() => void reload()}>{t('重试')}</Button>} /> :
        <Table<AgentApiKey> rowKey="id" size="small" loading={loading} dataSource={keys}
          pagination={false} scroll={{ x: 680 }}
          locale={{ emptyText: <Empty description={t('还没有 API-Key')} image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          columns={[
            { title: t('名称'), dataIndex: 'name', render: value => <Typography.Text strong>{value}</Typography.Text> },
            { title: 'Key', dataIndex: 'key_prefix', render: (value, key) => <Space>
              <Typography.Text code>{value}…</Typography.Text>
              {key.revealable && <Button size="small" icon={<CopyOutlined />} aria-label={t('复制完整密钥')}
                loading={pending.has(key.id)} onClick={() => reveal(key)} />}
            </Space> },
            { title: t('过期时间'), dataIndex: 'expires_at', render: value => value ? formatDateTime(value) : <Tag>{t('永不过期')}</Tag> },
            { title: t('最近使用'), dataIndex: 'last_used_at', render: value => formatDateTime(value, '—') },
            { title: t('启用'), render: (_, key) => <Switch size="small" aria-label={t('启用密钥')}
              checked={key.enabled} loading={pending.has(key.id)} onChange={enabled => change(key, enabled)} /> },
            { title: t('操作'), render: (_, key) => <Popconfirm
              title={t('撤销后立即失效且不可恢复，确定？')} okText={t('撤销')} cancelText={t('取消')}
              okButtonProps={{ danger: true, loading: pending.has(key.id) }}
              onConfirm={() => revoke(key)}>
              <Button size="small" danger disabled={pending.has(key.id)}>{t('撤销')}</Button>
            </Popconfirm> },
          ]} />}
    </div>
  );
}
