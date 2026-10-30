import {useEffect, useState} from 'react';
import {apiRequest} from '../api';
import {t} from '../i18n';
import {externalErrorMessage} from './externalApi';
import {reloadDirectory, useApplicationDirectory} from './catalog';
import {ExternalEmbedFrame} from './ExternalEmbedFrame';
import {validateEmbedLaunch, type ExternalEmbedLaunch} from './externalEmbed';
import {HostButton, HostNotice} from './ui';

function Failure({message, onRetry}: {message:string; onRetry?:() => void}) {
  return <HostNotice tone="error" placement="page"
    actions={onRetry && <HostButton tone="primary" onClick={onRetry}>{t('重试')}</HostButton>}>{message}</HostNotice>;
}
function Loading({children}: {children:string}) {
  return <HostNotice tone="loading" placement="page">{children}</HostNotice>;
}

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
  if (error) return <Failure message={t(error)} onRetry={onRetry}/>;
  if (!launch) return <Loading>{t('正在加载外部页面…')}</Loading>;
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
  if (catalog.error) return <Failure message={t(catalog.error)} onRetry={again}/>;
  if (catalog.loading && !item) return <Loading>{t('正在加载应用目录…')}</Loading>;
  if (!item?.availability.available) return <Failure message={t('应用不存在或你没有访问权限')}/>;
  return <EmbedLaunchLoader key={JSON.stringify([catalog.userId, applicationKey, item.revision, retry])}
    applicationKey={applicationKey} onRetry={again}/>;
}
