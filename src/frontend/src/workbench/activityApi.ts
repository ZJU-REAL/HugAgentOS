import type {RequestTarget} from '../api';
import {workbenchRequest as apiRequest} from './sourceRequest';

export interface ActivityBinding {
  revision: string; installId: string; packageRevision: string; slug: string; executionScope: 'local'|'cloud'; origin?: {kind: 'local'|'cloud'; id: string};
}
export interface ActivityContext {applicationKey: string; instanceId: string; moduleId?: string; binding: ActivityBinding}
export function activityPath(context: Pick<ActivityContext, 'applicationKey'|'instanceId'>) {
  return '/v1/workbench/applications/' + encodeURIComponent(context.applicationKey) +
    '/instances/' + encodeURIComponent(context.instanceId) + '/activity';
}
export async function activityRequest<T>(path: string, method: string, body?: unknown, signal?: AbortSignal, target?: RequestTarget, originId?: string): Promise<T> {
  const response = await apiRequest<{data: T}>(path, {
    method, ...(body !== undefined ? {body: JSON.stringify(body)} : {}), signal,
    ...(originId ? {headers: {'x-workbench-origin': originId}} : {}),
  }, target);
  return response.data;
}
export function scopedActivityRequest<T>(context: ActivityContext, suffix: string, method: string, body?: unknown, signal?: AbortSignal) {
  return activityRequest<T>(activityPath(context) + suffix, method, body, signal,
    context.binding.executionScope as RequestTarget, context.binding.origin?.id);
}
/** Host-only registration. It loads no page and grants no capability. */
export function registerActivity(context: ActivityContext, signal?: AbortSignal) {
  return scopedActivityRequest(context, '', 'POST', {binding: context.binding, moduleId: context.moduleId}, signal);
}
export function createActivityExtension(context: ActivityContext) {
  function params(value: unknown, keys: string[]) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        Object.keys(value).some(key => !keys.includes(key))) throw new Error('invalid_request');
    return {...value, moduleId: context.moduleId};
  }
  return {
    capabilities: ['badge.update', 'notification.show'],
    handlers: {
      'badge.update': (value: unknown, signal: AbortSignal) =>
        scopedActivityRequest(context, '/badge', 'PUT', params(value, ['count']), signal),
      'notification.show': (value: unknown, signal: AbortSignal) =>
        scopedActivityRequest(context, '/notification', 'POST', params(value, ['title','body']), signal),
    },
  };
}
