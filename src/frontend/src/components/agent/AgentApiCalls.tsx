import { useEffect, useState } from 'react';
import { Alert, Button, Empty, Select, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { listAgentApiCalls, type AgentApiCall, type AgentApiCallPage, type AgentApiCallStatus } from '../../api/agentApi';
import { formatDateTime } from '../../utils/date';
import { CopyButton } from '../common/CopyButton';
import { useAgentApiKeys } from './useAgentApiKeys';
import { t } from '../../i18n';

const statuses: Record<AgentApiCallStatus, { label: string; color: string }> = {
  running: { label: t('运行中'), color: 'processing' },
  completed: { label: t('已完成'), color: 'success' },
  failed: { label: t('失败'), color: 'error' },
  cancelled: { label: t('已取消'), color: 'default' },
  needs_attention: { label: t('需要处理'), color: 'warning' },
};

export function AgentApiCalls({ agentId }: { agentId: string }) {
  const { keys, error: keyError, reload: reloadKeys } = useAgentApiKeys(agentId);
  const [page, setPage] = useState(1);
  const [keyId, setKeyId] = useState<string>();
  const [status, setStatus] = useState<AgentApiCallStatus>();
  const [revision, setRevision] = useState(0);
  const query = JSON.stringify({ agentId, page, keyId, status, revision });
  const [result, setResult] = useState<{ query: string; data?: AgentApiCallPage; error?: string }>();
  const loading = result?.query !== query;
  const data = !loading ? result?.data : undefined;
  const error = !loading ? result?.error : undefined;
  useEffect(() => {
    const controller = new AbortController();
    void listAgentApiCalls(agentId, { page, page_size: 10, key_id: keyId, status }, controller.signal)
      .then(data => { if (!controller.signal.aborted) setResult({ query, data }); })
      .catch((e: Error) => { if (!controller.signal.aborted) setResult({ query, error: e.message }); });
    return () => controller.abort();
  }, [agentId, page, keyId, status, query]);
  return <div className="jx-agentApi-section">
    <div className="jx-agentApi-filters">
      <Select aria-label={t('筛选密钥')} placeholder={t('全部密钥')} allowClear value={keyId}
        onChange={value => { setKeyId(value); setPage(1); }}
        options={keys.map(key => ({ value: key.id, label: `${key.name} · ${key.key_prefix}…` }))} />
      <Select aria-label={t('筛选状态')} placeholder={t('全部状态')} allowClear value={status}
        onChange={value => { setStatus(value); setPage(1); }}
        options={Object.entries(statuses).map(([value, item]) => ({ value, label: item.label }))} />
      <Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRevision(value => value + 1)}>{t('刷新')}</Button>
    </div>
    {keyError && <Alert type="warning" title={t('密钥筛选暂不可用，仍可查看全部调用记录。')}
      action={<Button onClick={() => void reloadKeys()}>{t('重试')}</Button>} />}
    <Typography.Text type="secondary">{t('状态反映智能体执行结果，流式连接建立成功不代表执行完成。记录不包含请求正文或密钥明文。')}</Typography.Text>
    {error ? <Alert type="error" showIcon title={error}
      action={<Button onClick={() => setRevision(value => value + 1)}>{t('重试')}</Button>} /> :
      <Table<AgentApiCall> rowKey="id" size="small" loading={loading}
        dataSource={data?.items || []} scroll={{ x: 850 }}
        pagination={{ current: page, pageSize: 10, total: data?.pagination.total_items || 0,
          showSizeChanger: false, onChange: setPage }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('暂无 API 调用记录')} /> }}
        expandable={{ expandedRowRender: call => <div className="jx-agentApi-callDetail">
          <span>Chat ID: <Typography.Text code>{call.chat_id || '—'}</Typography.Text></span>
          <span>Run ID: <Typography.Text code>{call.run_id || '—'}</Typography.Text></span>
          <span>HTTP: {call.http_status ?? '—'}</span>
          <span>{t('错误码')}：{call.error_code || '—'}</span>
          <span>{t('完成时间')}：{formatDateTime(call.completed_at, '—')}</span>
          <span>Tokens: {call.prompt_tokens ?? 0} + {call.completion_tokens ?? 0} = {call.total_tokens ?? 0}</span>
          <CopyButton text={call.id} size="small">{t('复制调用 ID')}</CopyButton>
        </div> }}
        columns={[
          { title: t('调用时间'), dataIndex: 'created_at', render: value => formatDateTime(value) },
          { title: t('密钥名称'), render: (_, call) => <div>{call.key_name}<br /><Typography.Text code>{call.key_prefix}…</Typography.Text></div> },
          { title: t('响应方式'), dataIndex: 'stream', render: value => value ? 'SSE' : 'JSON' },
          { title: t('状态'), dataIndex: 'status', render: (value: AgentApiCallStatus) => <Tag color={statuses[value]?.color}>{statuses[value]?.label || value}</Tag> },
          { title: t('耗时'), dataIndex: 'duration_ms', render: value => value == null ? '—' : `${(value / 1000).toFixed(2)} s` },
          { title: 'Tokens', dataIndex: 'total_tokens', render: value => value ?? '—' },
        ]} />}
  </div>;
}
