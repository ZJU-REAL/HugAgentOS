import { authFetch, getApiUrl, LOCAL_TARGET_HEADER } from '../../api';

export async function desktopCapabilityRequest<T>(path: string, method = 'GET', signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  const timer = setTimeout(cancel, 15_000);
  try {
    const response = await authFetch(`${getApiUrl()}/v1/desktop/capabilities/${path}`, {
      method, signal: controller.signal, headers: { [LOCAL_TARGET_HEADER]: 'local' },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.message || '暂时无法连接本机服务');
    return body.data as T;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
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
