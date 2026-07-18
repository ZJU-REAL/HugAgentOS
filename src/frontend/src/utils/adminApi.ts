import { licenseErrorMessage, readErrorMessage } from './apiError';
import { t } from '../i18n';

export const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || '/api';

// Admin platform
export const ADMIN_STORAGE_KEY = 'jx_admin_token';
export const ADMIN_AUTH_EXPIRED_EVENT = 'jx-admin-auth-expired';
// Config platform
export const CONFIG_STORAGE_KEY = 'jx_config_token';
export const CONFIG_AUTH_EXPIRED_EVENT = 'jx-config-auth-expired';

// backward compatibility
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

/** Error message extraction goes uniformly through utils/apiError.ts (shared with api.ts, single source of copy). */
function readAdminError(status: number, err: unknown): string {
  if (status === 402) {
    return licenseErrorMessage(err);
  }
  return readErrorMessage(err, `HTTP ${status}`);
}

function createAuthFetch(storageKey: string, expiredEvent: string) {
  // When token is an empty string, send no Authorization header and rely on session cookie auth instead (users granted admin permissions get direct access without a token).
  return async (token: string, path: string, init?: RequestInit) => {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
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
      throw new Error(readAdminError(res.status, err));
    }
    return res.json();
  };
}

export const adminFetch = createAuthFetch(ADMIN_STORAGE_KEY, ADMIN_AUTH_EXPIRED_EVENT);
export const configFetch = createAuthFetch(CONFIG_STORAGE_KEY, CONFIG_AUTH_EXPIRED_EVENT);

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

/**
 * Update the home page shortcut list (uses CONFIG_TOKEN, entry inside /config "Page Config").
 */
export async function saveHomepageShortcuts(
  token: string,
  items: unknown[],
) {
  return configFetch(token, '/v1/content/homepage_shortcuts', {
    method: 'PUT',
    body: JSON.stringify({ payload: items }),
  });
}

export async function uploadPageAsset(
  token: string,
  asset: 'logo' | 'favicon' | 'app_icon' | 'mcp_icon',
  file: File,
): Promise<{ url: string; filename: string; size: number; asset: string }> {
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch(`${API_BASE}/v1/content/page_config/assets/upload?asset=${asset}`, {
    method: 'POST',
    credentials: 'include',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: fd,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    if (res.status === 401 || res.status === 403) {
      localStorage.removeItem(CONFIG_STORAGE_KEY);
      window.dispatchEvent(new CustomEvent(CONFIG_AUTH_EXPIRED_EVENT));
    }
    throw new Error(readAdminError(res.status, err));
  }
  const body = await res.json();
  return body.data;
}
