import { createEditionAccessError } from '../editionAccessError';
import { createApiResponseError, readErrorMessage } from './apiError';
import { t } from '../i18n';
import { invalidateWorkbenchDirectory } from '../workbench/invalidation';
import { API_BASE } from './apiBase';
import { saveBlob } from './download';

export { API_BASE };

// Admin platform
export const ADMIN_STORAGE_KEY = 'jx_admin_token';
export const ADMIN_AUTH_EXPIRED_EVENT = 'jx-admin-auth-expired';
// Config platform
export const CONFIG_STORAGE_KEY = 'jx_config_token';
export const CONFIG_AUTH_EXPIRED_EVENT = 'jx-config-auth-expired';

/** @deprecated Use ADMIN_STORAGE_KEY; kept only until components/admin/LoginView.tsx switches over. */
export const STORAGE_KEY = ADMIN_STORAGE_KEY;

export function move<T>(arr: T[], from: number, to: number): T[] {
  if (to < 0 || to >= arr.length) return arr;
  const next = [...arr];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/**
 * Unified pagination preset for admin list tables.
 *
 * Multiple admin lists (feature updates / capability center / prompt center / sub-agents / skills…) historically
 * each wrote their own pagination: some used `pagination={false}`, which piles everything into one long list with no
 * paging once items grow numerous; some had pageSize so large you had to scroll a long way before paging appeared.
 * This centralizes one default pagination config that every Table references, ensuring consistent paging behavior
 * and making it easy to adjust in one place later.
 *
 * ⚠️ For tables that "add/delete/edit / move up-down by row index", the index used in column rendering must be
 * `items.indexOf(record)` (the global index), not the third argument of the antd render callback
 * — that is the local index **within the current page**, which becomes misaligned after paging.
 */
export const ADMIN_TABLE_PAGINATION = {
  pageSize: 10,
  size: 'small' as const,
  showSizeChanger: true,
  pageSizeOptions: [10, 20, 50, 100],
  showTotal: (total: number) => t('共 {total} 条', { total }),
};

/**
 * The console request core shared by Admin and Config: API base, session cookie, optional Bearer token,
 * JSON content type (except for FormData bodies), and on 401/403 drop the stored token and announce expiry.
 * Error message extraction goes uniformly through utils/apiError.ts (shared with api.ts, single source of copy).
 * When token is an empty string, no Authorization header is sent and the session cookie authenticates instead
 * (users granted admin permissions get direct access without a token).
 */
function createAuthRequest(storageKey: string, expiredEvent: string) {
  return async (token: string, path: string, init?: RequestInit, fallbackMessage?: string): Promise<Response> => {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: 'include',
      headers: {
        ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...((init?.headers as Record<string, string>) || {}),
      },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (res.status === 401 || res.status === 403) {
        localStorage.removeItem(storageKey);
        window.dispatchEvent(new CustomEvent(expiredEvent));
      }
      const editionError = createEditionAccessError(res.status, err, readErrorMessage);
      if (editionError) throw editionError;
      throw createApiResponseError(res.status, err, fallbackMessage || `HTTP ${res.status}`);
    }
    return res;
  };
}

/** JSON request for a console: resolves to the response envelope. */
function createAuthFetch(storageKey: string, expiredEvent: string) {
  const request = createAuthRequest(storageKey, expiredEvent);
  return async (token: string, path: string, init?: RequestInit) => {
    const body = await (await request(token, path, init)).json();
    if (init?.method && !['GET', 'HEAD'].includes(init.method.toUpperCase())
      && (path.startsWith('/v1/workbench/') || path === '/v1/content/docs/app_config'))
      invalidateWorkbenchDirectory();
    return body;
  };
}

/** File download for a console: fetches the response as a blob and saves it under `filename`. */
function createAuthDownload(storageKey: string, expiredEvent: string) {
  const request = createAuthRequest(storageKey, expiredEvent);
  return async (token: string, path: string, filename: string, init?: RequestInit, fallbackMessage?: string) => {
    saveBlob(await (await request(token, path, init, fallbackMessage)).blob(), filename);
  };
}

const configRequest = createAuthRequest(CONFIG_STORAGE_KEY, CONFIG_AUTH_EXPIRED_EVENT);
export const adminFetch = createAuthFetch(ADMIN_STORAGE_KEY, ADMIN_AUTH_EXPIRED_EVENT);
export const configFetch = createAuthFetch(CONFIG_STORAGE_KEY, CONFIG_AUTH_EXPIRED_EVENT);
export const adminDownload = createAuthDownload(ADMIN_STORAGE_KEY, ADMIN_AUTH_EXPIRED_EVENT);
export const configDownload = createAuthDownload(CONFIG_STORAGE_KEY, CONFIG_AUTH_EXPIRED_EVENT);

export const fetchContent = (token: string) => adminFetch(token, '/v1/content/docs');

export async function saveBlock(
  token: string,
  blockId: string,
  payload: unknown[] | Record<string, unknown>,
) {
  return adminFetch(token, `/v1/content/docs/${blockId}`, {
    method: 'PUT',
    body: JSON.stringify({ payload }),
  });
}

/**
 * Update page config (uses CONFIG_TOKEN, access point under /config).
 */
export async function savePageConfig(
  token: string,
  payload: Record<string, unknown>,
) {
  return configFetch(token, '/v1/content/page_config', {
    method: 'PUT',
    body: JSON.stringify({ payload }),
  });
}

/**
 * Update app config (external sub-app URLs such as enterprise profile / enterprise research), uses CONFIG_TOKEN.
 */
export async function saveAppConfig(
  token: string,
  payload: Record<string, unknown>,
) {
  return configFetch(token, '/v1/content/app_config', {
    method: 'PUT',
    body: JSON.stringify({ payload }),
  });
}


export async function uploadPageAsset(
  token: string,
  asset: 'logo' | 'favicon' | 'login_background' | 'app_icon' | 'mcp_icon',
  file: File,
): Promise<{ url: string; filename: string; size: number; asset: string }> {
  const fd = new FormData();
  fd.append('file', file);
  const res = await configRequest(token, `/v1/content/page_config/assets/upload?asset=${asset}`, { method: 'POST', body: fd });
  const body = await res.json();
  return body.data;
}
