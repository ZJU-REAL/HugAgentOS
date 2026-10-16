import {useEffect, useState} from 'react';
import {workbenchRequest as apiRequest} from './sourceRequest';
import {ApiResponseError} from '../utils/apiError';
import {getLang} from '../i18n';
import {useApplicationDirectory} from './catalog';

function diagnosticId(error: unknown): string | null {
  if (!(error instanceof ApiResponseError)) return null;
  const payload = error.payload as {detail?:{diagnosticId?:unknown}; data?:{diagnosticId?:unknown}};
  const id = payload?.detail?.diagnosticId ?? payload?.data?.diagnosticId;
  return typeof id === 'string' && /^[a-f0-9]{32}$/.test(id) ? id : null;
}

/** Host failure UI; callers provide a retry that rechecks the current binding. */
export function DiagnosticFailure({error, applicationKey, code, message, onRetry}: {
  error?: unknown; applicationKey?: string; code?: 'bridge_timeout'|'page_failed'; message?:string; onRetry:()=>void;
}) {
  const userId = useApplicationDirectory().userId;
  const seed = diagnosticId(error);
  return <FailureReceipt key={JSON.stringify([userId,applicationKey,code,seed])}
    seed={seed} applicationKey={applicationKey} code={code} message={message} onRetry={onRetry} />;
}
function FailureReceipt({seed,applicationKey,code,message,onRetry}:{
  seed:string|null;applicationKey?:string;code?:'bridge_timeout'|'page_failed';message?:string;onRetry:()=>void;
}) {
  const [id,setId] = useState<string|null>(seed);
  const [copied,setCopied] = useState(false);
  const english = getLang() === 'en';
  useEffect(()=>{
    const controller = new AbortController();
    if (applicationKey && code) {
      void apiRequest<{data:{diagnosticId:string}}>(
        '/v1/workbench/applications/'+encodeURIComponent(applicationKey)+'/diagnostics',
        {method:'POST', body:JSON.stringify({code}), signal:controller.signal})
        .then(result=>{if(!controller.signal.aborted && /^[a-f0-9]{32}$/.test(result.data.diagnosticId))setId(result.data.diagnosticId);})
        .catch(()=>{/* Retry remains available when diagnostic storage is unavailable. */});
    }
    return ()=>controller.abort();
  },[applicationKey,code]);
  async function copy() {
    if (!id) return;
    try {await navigator.clipboard.writeText(id);setCopied(true);}
    catch {setCopied(false);}
  }
  return <div role="alert">
    <p>{message ?? (english?'Application could not load. Retry checks current access.':'应用未能加载。重试会重新检查当前访问权限。')}</p>
    {id && <p>{english?'Diagnostic ID:':'诊断编号：'} <code tabIndex={0}>{id}</code>{' '}
      <button type="button" onClick={()=>void copy()}>{english?'Copy ID':'复制编号'}</button></p>}
    {copied && <p role="status">{english?'Copied':'已复制'}</p>}
    <button type="button" onClick={onRetry}>{english?'Retry':'重试'}</button>
  </div>;
}
