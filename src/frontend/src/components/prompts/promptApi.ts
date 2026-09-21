import { apiRequest } from '../../api';

/** Shared settings transport: local login, cookies and explicit Config tokens. */
export async function promptFetch(token: string, path: string, options: RequestInit = {}): Promise<any> {
  const headers = new Headers(options.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return apiRequest(path, { ...options, headers: Object.fromEntries(headers.entries()) });
}
