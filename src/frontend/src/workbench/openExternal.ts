import {ApiResponseError} from '../utils/apiError';
import { useAuthStore } from '../stores/authStore';
import { t } from '../i18n';
import {workbenchRequest as apiRequest} from './sourceRequest';
import { externalErrorMessage } from './externalApi';
import {createHostPlatform} from './platformHost';
import {safePlatformUrl} from './platformAdapter';

/** Open synchronously during the gesture; authorization finishes in the waiting window. */
export async function openExternalApplication(applicationKey: string, signal?: AbortSignal, embeddedFallback = false): Promise<void> {
  const userId = useAuthStore.getState().authUser?.user_id;
  if(window.top === window.self && window.__HG_PLATFORM__) {
    const adapter=createHostPlatform();
    try {
      const result=await apiRequest<{data:{
        applicationKey:string;target:{kind:string;mode:string;url:string};
      }}>('/v1/workbench/applications/'+encodeURIComponent(applicationKey)+'/launch',{
        method:'POST',body:'{}',signal,
      });
      if(signal?.aborted || useAuthStore.getState().authUser?.user_id !== userId)
        throw new Error(t('打开应用已取消或超时，请重试'));
      const target=result.data?.target;
      if(result.data?.applicationKey !== applicationKey || target?.kind !== 'external' || (target.mode !== 'browser' && !(embeddedFallback && target.mode === 'embedded')))
        throw new Error(t('应用启动信息无效'));
      await adapter.openExternal(safePlatformUrl(target.url),signal);
      return;
    } catch(error){if(error instanceof ApiResponseError)throw error;throw new Error(externalErrorMessage(error));}
    finally{adapter.dispose();}
  }
  const popup = window.open('about:blank', '_blank');
  if (!popup) throw new Error(t('浏览器已阻止弹窗，请允许弹窗后重试'));
  popup.opener = null;
  popup.document.title = '正在打开应用';
  popup.document.body.textContent = '正在验证应用访问权限…';
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, {once:true});
  const timeout = window.setTimeout(abort, 10000);
  try {
    if (signal?.aborted) controller.abort();
    const result = await apiRequest<{data: {
      applicationKey: string; target: {kind: string; mode: string; url: string};
    }}>('/v1/workbench/applications/' + encodeURIComponent(applicationKey) + '/launch', {
      method:'POST', body:'{}', signal:controller.signal,
    });
    if (controller.signal.aborted || useAuthStore.getState().authUser?.user_id !== userId) throw new Error(t('打开应用已取消或超时，请重试'));
    const target = result.data?.target;
    if (result.data?.applicationKey !== applicationKey || target?.kind !== 'external' || (target.mode !== 'browser' && !(embeddedFallback && target.mode === 'embedded'))) {
      throw new Error(t('应用启动信息无效'));
    }
    const url = new URL(target.url);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
        ([...target.url].some(c => c.charCodeAt(0) <= 32 || c.charCodeAt(0) === 127) || target.url.includes(String.fromCharCode(92)))) throw new Error(t('应用地址无效'));
    if (popup.closed) throw new Error(t('应用窗口已关闭，请重试'));
    // Prevent the destination from learning the waiting page URL or retaining an opener.
    const link = popup.document.createElement('a');
    link.href = url.href;
    link.rel = 'noreferrer noopener';
    link.referrerPolicy = 'no-referrer';
    popup.document.body.replaceChildren(link);
    link.click();
  } catch (error) {
    popup.close();
    if (controller.signal.aborted) throw new Error(t('打开应用已取消或超时，请重试'));
    if (error instanceof ApiResponseError) throw error;
    throw new Error(externalErrorMessage(error));
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
  }
}
