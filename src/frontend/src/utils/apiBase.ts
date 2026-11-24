/**
 * The one place that turns `VITE_API_BASE_URL` into the API base URL ('/api' by default).
 * Trimmed and without a trailing slash, so callers can always write `${API_BASE}/v1/...`.
 */
export const API_BASE: string =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined) || '').trim().replace(/\/+$/, '') || '/api';
