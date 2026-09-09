import { authFetch, getApiUrl, LOCAL_TARGET_HEADER } from '../../api';

export async function desktopCapabilityRequest<T>(path: string, method = 'GET'): Promise<T> {
  const response = await authFetch(`${getApiUrl()}/v1/desktop/capabilities/${path}`, {
    method, headers: { [LOCAL_TARGET_HEADER]: 'local' },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.message || '暂时无法连接本机服务');
  return body.data as T;
}

export interface CapabilitySyncStatus {
  ready: boolean;
  syncing: boolean;
  partial: boolean;
  can_continue: boolean;
  totals_known: boolean;
  completed: number;
  total: number;
  pending: number;
  error?: string | null;
  groups: { kind: string; completed: number; total: number; failed: number; manifest_ready: boolean }[];
}
