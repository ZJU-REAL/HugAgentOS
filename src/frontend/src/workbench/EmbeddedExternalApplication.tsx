import {useEffect, useState} from 'react';
import {apiRequest} from '../api';
import {t} from '../i18n';
import {externalErrorMessage} from './externalApi';
import {reloadDirectory, useApplicationDirectory} from './catalog';
import {ExternalEmbedFrame} from './ExternalEmbedFrame';
import {validateEmbedLaunch, type ExternalEmbedLaunch} from './externalEmbed';

function EmbedLaunchLoader({applicationKey, onRetry}: {
  applicationKey:string; onRetry:() => void;
}) {
  const [launch, setLaunch] = useState<ExternalEmbedLaunch | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<{data:ExternalEmbedLaunch}>(
      '/v1/workbench/applications/' + encodeURIComponent(applicationKey) + '/launch',
      {method:'POST', body:'{}', signal:controller.signal},
    ).then(result => {
      if (controller.signal.aborted) return;
      if (result.data.applicationKey !== applicationKey) throw new Error('应用启动信息无效');
      validateEmbedLaunch(result.data, window.location.origin);
      setLaunch(result.data);
    }).catch(e => {if (!controller.signal.aborted) setError(externalErrorMessage(e));});
    return () => controller.abort();
  }, [applicationKey]);
  if (error) return <p role="alert">{t(error)} <button onClick={onRetry}>{t('重试')}</button></p>;
  if (!launch) return <p role="status">{t('正在加载外部页面…')}</p>;
  return <ExternalEmbedFrame launch={launch} onRetry={onRetry}/>;
}

/** Optional standalone host; the generic route may pass its own validated launch
 * directly to ExternalEmbedFrame instead. Neither host uses a catalog URL.
 */
export function EmbeddedExternalApplication({applicationKey}: {applicationKey:string}) {
  const catalog = useApplicationDirectory();
  const [retry, setRetry] = useState(0);
  const item = catalog.items.find(i => i.applicationKey === applicationKey);
  function again() {reloadDirectory(); setRetry(n => n + 1);}
  if (catalog.error) return <p role="alert">{t(catalog.error)} <button onClick={again}>{t('重试')}</button></p>;
  if (catalog.loading && !item) return <p role="status">{t('正在加载应用目录…')}</p>;
  if (!item?.availability.available) return <p role="alert">{t('应用不存在或你没有访问权限')}</p>;
  return <EmbedLaunchLoader key={JSON.stringify([catalog.userId, applicationKey, item.revision, retry])}
    applicationKey={applicationKey} onRetry={again}/>;
}
