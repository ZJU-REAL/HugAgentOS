/** Optional resource helper; requires host resource capabilities. */
export interface ResourceClient {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
}
export function resources(client: ResourceClient) {
  return {
    open: () => client.request('resource.open', {}),
    renew: (resourceId: string) => client.request('resource.lease', {resourceId}),
    command: (resourceId: string, id: string, action: string, params: Record<string, unknown> = {}) =>
      client.request('resource.command', {resourceId, id, action, params}),
    close: (resourceId: string) => client.request('resource.close', {resourceId}),
  };
}
