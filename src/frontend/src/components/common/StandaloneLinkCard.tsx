import { useEffect, useState } from 'react';
import { ExportOutlined, LinkOutlined } from '@ant-design/icons';
import { Alert, Button, Input, Switch, Typography, message } from 'antd';
import { apiRequest, unwrapData } from '../../api';
import { pathForStandalone, type StandaloneTarget } from '../../routing/standalone';
import { CopyButton } from './CopyButton';
import { t } from '../../i18n';
import '../../styles/standalone-chat.css';

interface LinkInfo { token: string; enabled: boolean }
export function StandaloneLinkCard({ kind, resourceId, canManage = true }: StandaloneTarget & { canManage?: boolean }) {
  const [link, setLink] = useState<LinkInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const path = `/v1/${kind}/${encodeURIComponent(resourceId)}/web-link`;
  useEffect(() => {
    let cancelled = false;
    if (!canManage) { setLoading(false); return; }
    setLoading(true); setError(false);
    void apiRequest(path).then(data => { if (!cancelled) setLink(unwrapData<LinkInfo | null>(data)); })
      .catch(() => { if (!cancelled) setError(true); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [path, attempt, canManage]);
  if (!canManage) return null;
  const url = link ? new URL(pathForStandalone({ kind, resourceId: link.token }), window.location.origin).href : '';
  const update = async (enabled?: boolean) => {
    setBusy(true);
    try { setLink(unwrapData<LinkInfo>(await apiRequest(path, { method: link ? 'PATCH' : 'POST',
      ...(link ? { body: JSON.stringify({ enabled }) } : {}) }))); }
    catch { void message.error(t('更新链接失败，请重试')); }
    finally { setBusy(false); }
  };
  return <section className="jx-standaloneLink" aria-label={t('独立对话链接')}>
    <div className="jx-standaloneLink-heading"><LinkOutlined /><strong>{t('独立对话链接')}</strong>
      {link && <Switch checked={link.enabled} loading={busy} onChange={enabled => void update(enabled)} aria-label={t('启用免登录对话')} />}
    </div>
    <Typography.Text type="secondary">{t('任何获得链接的人均可对话，消耗由发布者承担。访客之间的记录相互隔离。')}</Typography.Text>
    {error ? <Alert type="warning" title={t('加载链接失败')} action={<Button onClick={() => setAttempt(value => value + 1)}>{t('重试')}</Button>} />
      : !link ? <Button type="primary" loading={loading || busy} onClick={() => void update()}>{t('启用免登录对话')}</Button>
      : <div className="jx-standaloneLink-actions">
        <Input readOnly value={url} aria-label={t('独立对话链接')} onFocus={event => event.target.select()} />
        <CopyButton text={url} disabled={!link.enabled}>{t('复制链接')}</CopyButton>
        <Button href={link.enabled ? url : undefined} disabled={!link.enabled} target="_blank" rel="noopener noreferrer" icon={<ExportOutlined />}>{t('打开')}</Button>
      </div>}
  </section>;
}
