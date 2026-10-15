import { API_BASE } from '../utils/adminApi';
import { createApiResponseError } from '../utils/apiError';

export type ConfigField = {
  key: string; label: string; description?: string; secret: boolean;
  is_set: boolean; value?: string;
};
export type ConfigurableApplication = {
  applicationKey: string; title: string | Record<string, string>; ownerUserId: string | null;
  origin: { kind: string; id: string }; revision: string; enabled: boolean;
  fields: ConfigField[]; configured: boolean;
};
const endpoint = '/v1/workbench/admin/applications';
/** This seam accepts only the Config console's credential and stays on its current server. */
export async function applicationConfigRequest<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(API_BASE + endpoint + path, {
    ...init, credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw createApiResponseError(response.status, body, 'HTTP ' + response.status);
  return body.data as T;
}
export async function configurableApplications(token: string, signal: AbortSignal) {
  const items: ConfigurableApplication[] = [];
  for (let offset = 0; ; offset += 100) {
    const result = await applicationConfigRequest<{ items: ConfigurableApplication[] }>(
      token, '/configurable?offset=' + offset + '&limit=100', { signal });
    if (!Array.isArray(result.items)) throw new Error('invalid_application_config_list');
    items.push(...result.items);
    if (result.items.length < 100) return items;
  }
}
export function applicationConfigPath(item: ConfigurableApplication) {
  return '/' + encodeURIComponent(item.applicationKey) + '/config';
}
export function configurationValues(fields: ConfigField[], values: Record<string, string>, clearKeys: string[]) {
  return Object.fromEntries(fields.flatMap(field => {
    const value = values[field.key] ?? '';
    return clearKeys.includes(field.key) || (field.secret && (!value || value === '****'))
      ? [] : [[field.key, value]];
  }));
}
