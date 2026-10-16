import { useEffect, useState } from 'react';
import { useLocation } from 'react-router';
import {workbenchRequest as apiRequest} from './sourceRequest';
import {EmbeddedExternalApplication} from './EmbeddedExternalApplication';
import { ApiResponseError } from '../utils/apiError';
import { t } from '../i18n';
import { reloadDirectory, useApplicationDirectory, type CatalogItem } from './catalog';
import {DiagnosticFailure} from './DiagnosticFailure';
import { applicationRoute } from './workspaceNavigation';

function unavailable(item: CatalogItem) {
  const reason = item.availability.reason;
  if (reason === 'app_disabled') return t('应用已停用或配置发生冲突，请重新加载');
  if (reason === 'permission_denied') return t('没有访问权限');
  if (reason === 'host_incompatible') return t('宿主版本不兼容');
  return t('当前环境不支持此应用');
}
export function WorkspaceApplication() {
  const location = useLocation();
  const route = applicationRoute(location.pathname, location.search, location.hash);
  const catalog = useApplicationDirectory();
  const item = catalog.items.find(value => value.applicationKey === route?.applicationKey);
  if (!route) return <DiagnosticFailure message={t('应用地址无效')} onRetry={()=>void reloadDirectory()}/>;
  if (catalog.error) return <DiagnosticFailure applicationKey={route.applicationKey} onRetry={()=>void reloadDirectory()}/>;
  if (catalog.loading && !item) return <p role="status">{t('正在加载应用目录…')}</p>;
  if (!item) return <MissingApplication key={JSON.stringify([catalog.identity,route.applicationKey])} applicationKey={route.applicationKey} empty={catalog.items.length === 0} />;
  if (!item.availability.available) return <DiagnosticFailure message={unavailable(item)} onRetry={()=>void reloadDirectory()}/>;
  if (item.declaration.target.kind === 'external' && item.declaration.target.mode === 'embedded')
    return <EmbeddedExternalApplication key={JSON.stringify([catalog.identity,item.applicationKey,item.revision])} applicationKey={item.applicationKey} />;
  if (item.declaration.target.kind !== 'plugin') return <DiagnosticFailure message={t('此应用请从原入口打开')} onRetry={()=>void reloadDirectory()}/>;
  return null;
}
function MissingApplication({applicationKey, empty}:{applicationKey:string; empty:boolean}) {
  const [attempt,setAttempt]=useState(0);
  const [result,setResult]=useState<{item?:CatalogItem;done:boolean;error?:unknown;failed?:boolean}>({done:false});
  useEffect(()=>{
    const controller=new AbortController();
    apiRequest<{data:CatalogItem}>('/v1/workbench/applications/'+encodeURIComponent(applicationKey),{signal:controller.signal})
      .then(response=>{if(!controller.signal.aborted)setResult({item:response.data,done:true});})
      .catch(error=>{
        if(controller.signal.aborted)return;
        const missing=error instanceof ApiResponseError && (error.status===403 || error.status===404);
        setResult({done:true,error,failed:!missing});
      });
    return ()=>controller.abort();
  },[applicationKey,attempt]);
  if(!result.done)return <p role="status">{t('正在加载应用目录…')}</p>;
  const retry=()=>{setResult({done:false});setAttempt(value=>value+1);void reloadDirectory();};
  const message=result.failed ? t('应用目录加载失败') :
    result.item && !result.item.availability.available ? unavailable(result.item) : t('应用不存在或你没有访问权限');
  return <section>
    {empty && !result.item && <p role="status">{t('暂无可用应用')}</p>}
    <DiagnosticFailure error={result.error} message={message} onRetry={retry}/>
  </section>;
}
