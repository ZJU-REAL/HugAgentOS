import { useEffect, useMemo, useState } from 'react';
import { t } from '../i18n';
import { scopedActivityRequest, registerActivity, type ActivityContext } from './activityApi';
import { reloadActivity } from './activityStore';

const labels = {badge: '角标', notification: '通知', background: '后台运行'} as const;
export function ActivityControls({context, declared, resourceId}: {
  context: ActivityContext; declared: string[]; resourceId?: string;
}) {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const key = JSON.stringify(context);
  const stableContext = useMemo(() => JSON.parse(key) as ActivityContext, [key]);
  useEffect(() => {
    const controller = new AbortController();
    setReady(false); setSelected([]); setError('');
    const renew = () => registerActivity(stableContext, controller.signal)
      .then(() => {if (!controller.signal.aborted) setReady(true);})
      .catch(() => {if (!controller.signal.aborted) {setReady(false); setError(t('应用活动授权已失效'));}});
    void registerActivity(stableContext, controller.signal).then(() =>
      scopedActivityRequest<{capabilities: string[]}>(stableContext, '/consent',
        'GET', undefined, controller.signal)).then(value => {
          if (!controller.signal.aborted) {setSelected(value.capabilities); setReady(true);}
        }).catch(() => {
          if (!controller.signal.aborted) setError(t('应用活动授权已失效'));
        });
    const timer = setInterval(() => void renew(), 30000);
    return () => {
      clearInterval(timer); controller.abort();
      void scopedActivityRequest(stableContext, '', 'DELETE').catch(() => undefined);
    };
  }, [stableContext]);
  async function consent() {
    setBusy(true); setError('');
    try {
      await scopedActivityRequest(context, '/consent', 'POST', {capabilities: selected});
      reloadActivity();
    } catch {setError(t('应用活动授权失败，请重试'));}
    finally {setBusy(false);}
  }
  async function start() {
    setBusy(true); setError('');
    try {
      await scopedActivityRequest(context, '/background', 'POST', {resourceId});
      reloadActivity();
    } catch {setError(t('后台运行被拒绝，请检查授权和配额'));}
    finally {setBusy(false);}
  }
  const permissions = Object.keys(labels).filter(key => declared.includes(key)) as (keyof typeof labels)[];
  if (!permissions.length) return null;
  return <section aria-label={t('应用活动权限')}>
    <p>{t('仅允许本应用已声明的能力。后台运行最多十五分钟，停止后释放资源。')}</p>
    {permissions.map(permission => <label key={permission}><input type="checkbox" checked={selected.includes(permission)}
      disabled={!ready || busy} onChange={event => setSelected(event.target.checked
        ? [...selected,permission] : selected.filter(value => value !== permission))} />{t(labels[permission])}</label>)}
    <button type="button" disabled={!ready || busy} onClick={() => void consent()}>{t('保存应用活动授权')}</button>
    {resourceId && permissions.includes('background') && <button type="button" disabled={!ready || busy}
      onClick={() => void start()}>{t('允许此资源后台运行')}</button>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
