/** Resource bindings are declarative; the host never checks a plugin name. */
import { apiRequest, getApiUrl, unwrapData } from '../../api';
import { readPointer, unwrap } from '../pointer';
import type { ModuleContribution } from '../types';

export interface ResourceBinding {
  resource_id: string;
  chat_id: string;
  slug: string;
  module_id: string;
  install_id: string;
  revision: string;
  execution_scope: 'local' | 'cloud';
  status: string;
}

export function resourceBinding(payload: unknown, module?: ModuleContribution): ResourceBinding | null {
  if (!module?.resource_binding) return null;
  const value = readPointer(unwrap(payload, module.unwrap), module.resource_binding);
  if (!value || typeof value !== 'object') return null;
  const binding = value as Partial<ResourceBinding>;
  return typeof binding.resource_id === 'string' && typeof binding.install_id === 'string'
    && (binding.execution_scope === 'local' || binding.execution_scope === 'cloud')
    && [binding.resource_id, binding.install_id, binding.slug, binding.chat_id, binding.revision, binding.status].every(value => typeof value === 'string' && value.length > 0)
    && binding.module_id === module.id
    ? binding as ResourceBinding : null;
}

export function resourceUrl(binding: ResourceBinding, suffix: string): string {
  const url = new URL(`${getApiUrl()}/v1/plugin-resources/${encodeURIComponent(binding.resource_id)}${suffix}`, location.href);
  if (binding.execution_scope === 'local') url.searchParams.set('hg_target', 'local');
  return url.href;
}

export async function attachResource(binding: ResourceBinding, signal: AbortSignal): Promise<WebSocket> {
  const data = unwrapData<{ ticket: string }>(await apiRequest(
    `/v1/plugin-resources/${encodeURIComponent(binding.resource_id)}/attach`,
    { method: 'POST', signal }, binding.execution_scope,
  ));
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  const url = new URL(resourceUrl(binding, '/stream'));
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(url);
  socket.binaryType = 'arraybuffer';
  await new Promise<void>((resolve, reject) => {
    const cancel = () => { socket.close(); reject(new DOMException('Aborted', 'AbortError')); };
    signal.addEventListener('abort', cancel, { once: true });
    socket.onopen = () => {
      signal.removeEventListener('abort', cancel);
      socket.send(JSON.stringify({ ticket: data.ticket }));
      resolve();
    };
    socket.onerror = () => { signal.removeEventListener('abort', cancel); socket.close(); reject(new Error('resource_connection_failed')); };
  });
  return socket;
}

export async function resourceAssetUrl(binding: ResourceBinding, signal: AbortSignal): Promise<string> {
  const data = unwrapData<{ ticket: string; entry: string }>(await apiRequest(
    `/v1/plugin-resources/${encodeURIComponent(binding.resource_id)}/asset-ticket`,
    { method: 'POST', signal }, binding.execution_scope,
  ));
  return new URL(`${getApiUrl()}/v1/plugin-resource-assets/${binding.execution_scope}/${encodeURIComponent(data.ticket)}/${data.entry.replace(/^web\//, '')}`, location.href).href;
}
