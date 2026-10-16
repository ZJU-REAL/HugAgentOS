import type { EmbeddingPolicy } from './externalEmbed';
import type { ApplicationDeclaration } from '@hugagent/workbench-protocol';
import { validateApplication } from '@hugagent/workbench-protocol';
import {workbenchRequest as apiRequest,WorkbenchSourceError} from './sourceRequest';
import {sourceLabel} from './sourceIdentity';
import { configFetch } from '../utils/adminApi';
import { getLang, t } from '../i18n';
import { ApiResponseError } from '../utils/apiError';

export interface ExternalApplication {
  applicationKey: string;
  declaration: ApplicationDeclaration;
  enabled: boolean;
  revision: number;
  embedding?: EmbeddingPolicy;
}
export const externalAdminPath = '/v1/workbench/admin/external-applications';
export function applicationText(value: ApplicationDeclaration['title'] | undefined): string {
  if (typeof value === 'string') return value;
  return value?.[getLang()] || value?.en || value?.['zh-CN'] || Object.values(value || {})[0] || '';
}
export async function externalDirectory(signal: AbortSignal) {
  const response = await apiRequest<{ data: { items: ExternalApplication[] } }>(
    '/v1/workbench/applications', { signal });
  if (!Array.isArray(response.data?.items)) throw new Error('应用目录格式无效');
  return response.data.items.filter(item =>
    validateApplication(item.declaration).length === 0 &&
    item.declaration.target.kind === 'external' && ['browser', 'embedded'].includes(item.declaration.target.mode));
}
export async function saveExternal(token: string, item: ExternalApplication | null, declaration: ApplicationDeclaration, enabled: boolean, embedding?: EmbeddingPolicy) {
  const response = await configFetch(token, item ? externalAdminPath + '/' + encodeURIComponent(item.applicationKey) : externalAdminPath, {
    method: item ? 'PUT' : 'POST',
    body: JSON.stringify({ declaration, enabled, ...(embedding ? {embedding} : {}), ...(item ? {revision: item.revision} : {}) }),
  });
  return response.data as ExternalApplication;
}

export function externalErrorMessage(error: unknown): string {
  if(error instanceof WorkbenchSourceError)return t(sourceLabel({kind:error.source,id:''}))+': '+t(error.code==='execution_plane_not_ready'?'本机执行面尚未就绪':error.code==='origin_mismatch'?'服务身份已改变，请重新加载':'此来源暂不可用，请检查连接');
  if (error instanceof ApiResponseError) {
    const payload = error.payload as {detail?: {code?: string}} | undefined;
    const code = payload?.detail?.code;
    if (code === 'origin_unavailable') return t('此来源暂不可用，请检查连接');
    if (code === 'origin_mismatch') return t('服务身份已改变，请重新加载');
    if (code === 'execution_scope_unsupported') return t('此安装不支持当前执行位置');
    if (code === 'app_disabled') return t('应用已停用或配置发生冲突，请重新加载');
    if (code === 'revision_conflict') return t('版本冲突，请重新加载应用后再编辑');
    if (code === 'app_not_found') return t('应用不存在或你没有访问权限');
    if (['application_exists', 'legacy_application_id', 'workbench_application_id'].includes(code || '')) return t('应用 ID 已存在');
  }
  return error instanceof Error ? t(error.message) : t('打开应用失败');
}
