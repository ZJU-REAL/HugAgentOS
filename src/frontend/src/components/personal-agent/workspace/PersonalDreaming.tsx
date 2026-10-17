import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, InputNumber, Modal, Space, Spin, Switch, Tag } from 'antd';
import { MoonOutlined } from '@ant-design/icons';
import { personalApi, errorText, type PersonalDreamingStatus } from '../api';
import { SettingRow } from '../settings/SettingRow';
import { t } from '../../../i18n';

const states: Record<string, string> = {
  pending: '等待整理', running: '正在整理', retry: '等待重试', failed: '整理失败', done: '整理完成',
};

export function PersonalDreaming() {
  const [status, setStatus] = useState<PersonalDreamingStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<string | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const [reportError, setReportError] = useState('');
  const refresh = useCallback(async (signal?: AbortSignal) => {
    try { const next = await personalApi.dreaming(signal); if (!signal?.aborted) setStatus(next); }
    catch (exc) { if (!signal?.aborted) setError(errorText(exc)); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = window.setInterval(() => void refresh(controller.signal), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refresh]);
  const update = async (patch: { enabled?: boolean; hour?: number }) => {
    setBusy(true); setError('');
    try { await personalApi.settings({ dreaming: patch }); await refresh(); }
    catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  };
  const run = async () => {
    setBusy(true); setError('');
    try { setStatus(await personalApi.runDreaming()); }
    catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  };
  const showReport = async () => {
    setReportOpen(true); setReport(null); setReportError('');
    try {
      const file = await personalApi.workspaceFile('DREAMS.md');
      setReport((file.truncated ? t('仅显示最近的整理记录。') + '\n' : '') + (file.content || ''));
    } catch (exc) { setReportError(errorText(exc)); }
  };
  const format = (value?: number | null) => value ? new Date(value * 1000).toLocaleString(undefined, { timeZone: status?.timezone }) : '—';
  return <>
    <SettingRow icon={<MoonOutlined />} label={t('Dreaming · 定期整理')}
      description={t('整理日志中的明确记忆请求；模糊或第三方内容留在报告供核对。自动去重、检查冲突，失败重试。')}
      control={<Switch aria-label={t('启用 Dreaming')} checked={status?.enabled ?? false} loading={busy}
        disabled={!status} onChange={enabled => void update({ enabled })} />} />
    {error && <Alert type="error" showIcon title={error} />}
    {status && <Space orientation="vertical" size="small" style={{ width: '100%' }}>
      <Space wrap>
        <span>{t('每天整理时间')}</span>
        <InputNumber aria-label={t('每天整理时间')} min={0} max={23} precision={0} value={status.hour}
          disabled={busy || !status.enabled} onChange={hour => { if (hour !== null) void update({ hour }); }} />
        <span>:00 · {status.timezone}</span>
        {status.status && <Tag color={status.status === 'failed' ? 'error' : 'default'}>{t(states[status.status] || status.status)}</Tag>}
      </Space>
      <span>{t('上次完成：{time}', { time: format(status.completed_at) })}</span>
      <span>{t('下次整理：{time}', { time: status.enabled ? format(status.next_run) : '—' })}</span>
      {(status.status === 'retry' || status.status === 'failed') && <Alert type="warning" showIcon
        title={t('整理未完成，已保留进度；可重试。')}
        description={status.status === 'retry' ? t('下次重试：{time}', { time: format(status.next_retry) }) : t('已达到自动重试上限，请检查模型或工作区后重试。')} />}
      <Space wrap>
        <Button size="small" loading={busy} disabled={!status.enabled || ['running', 'pending'].includes(status.status || '')}
          onClick={() => void run()}>{t(status.status === 'failed' || status.status === 'retry' ? '重试整理' : '立即整理')}</Button>
        <Button size="small" onClick={() => void showReport()}>{t('查看 Dreaming 报告')}</Button>
      </Space>
    </Space>}
    <Modal title="DREAMS.md" open={reportOpen} onCancel={() => setReportOpen(false)} footer={null} width={760}>
      {reportError ? <Alert type="info" title={reportError} /> : report === null ? <Spin /> :
        <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: '65vh', overflow: 'auto' }}>{report}</pre>}
    </Modal>
  </>;
}
