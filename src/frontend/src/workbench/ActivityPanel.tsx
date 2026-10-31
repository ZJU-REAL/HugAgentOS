import './activity.css';
import type { RequestTarget } from '../api';
import { useState } from 'react';
import { t } from '../i18n';
import { applicationText } from './externalApi';
import { useApplicationDirectory } from './catalog';
import { activityPath, activityRequest } from './activityApi';
import { reloadActivity, useActivity } from './activityStore';
import { HostButton, HostNotice } from './ui';

/** All notification content is plain text. Open actions are resolved by the host. */
export function ActivityPanel({onOpen}: {onOpen: (applicationKey: string) => void}) {
  const value = useActivity();
  const catalog = useApplicationDirectory();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<string>();
  const title = (key: string) => applicationText(catalog.items.find(i => i.applicationKey === key)?.declaration.title);
  async function remove(id: string, path: string, source?: RequestTarget) {
    setBusy(id); setError('');
    try {await activityRequest(path, 'DELETE', undefined, undefined, source); reloadActivity();}
    catch {setError(t('应用活动操作失败，请重试'));}
    finally {setBusy(undefined);}
  }
  async function renew(key: string, instanceId: string, resourceId: string, source?: RequestTarget) {
    setBusy(resourceId); setError('');
    try {
      await activityRequest(activityPath({applicationKey: key, instanceId}) + '/background/' +
        encodeURIComponent(resourceId) + '/lease', 'POST', {}, undefined, source);
      reloadActivity();
    } catch {setError(t('后台运行授权或租约已失效'));}
    finally {setBusy(undefined);}
  }
  if (!value.notifications.length && !value.background.length && !error) return null;
  return <aside aria-label={t('应用活动')} className="jx-activityPanel">
    <div aria-live="polite" aria-atomic="false">
      {value.notifications.map(n => <article key={n.id} aria-label={title(n.applicationKey) + ' · ' + n.title} className="jx-activityPanel-item">
        <div className="jx-activityPanel-text"><strong>{title(n.applicationKey)} · {n.title}</strong><p>{n.body}</p></div>
        <HostButton size="small" onClick={() => onOpen(n.applicationKey)}>{t('打开应用')}</HostButton>
        <HostButton size="small" tone="text" disabled={busy === n.id}
          onClick={() => void remove(n.id, '/v1/workbench/me/activity/notifications/' + encodeURIComponent(n.id), n.source)}>
          {t('忽略通知')}</HostButton>
      </article>)}
    </div>
    {value.background.map(row => <div key={row.resourceId} className="jx-activityPanel-item">
      <span className="jx-activityPanel-text">{title(row.applicationKey)} · {t('后台运行中')}</span>
      <HostButton size="small" disabled={busy === row.resourceId}
        onClick={() => void renew(row.applicationKey, row.instanceId, row.resourceId, row.source)}>{t('续期后台租约')}</HostButton>
      <HostButton size="small" tone="danger" disabled={busy === row.resourceId} onClick={() => void remove(row.resourceId,
        activityPath(row) + '/background/' + encodeURIComponent(row.resourceId), row.source)}>{t('停止后台运行')}</HostButton>
    </div>)}
    {error && <HostNotice tone="error">{error}</HostNotice>}
  </aside>;
}
