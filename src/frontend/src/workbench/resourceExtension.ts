import {randomUUID} from './uuid';
/** Resource requests are bound to a host-created instance, never plugin-supplied identity. */
export interface ResourceBinding {
  applicationKey: string;
  instanceId: string;
  installId: string;
  revision: string;
  executionScope: 'local' | 'cloud';
  origin: {kind: 'local' | 'cloud'; id: string};
  moduleId: string;
  signal?: AbortSignal;
  onResourceChange?:(id:string|undefined)=>void;
  backgroundResources?:(signal:AbortSignal)=>Promise<readonly string[]>;
}
export interface WorkspaceResource {
  resource_id: string;
  status: string;
  expires_at: number;
}
export type ResourceRequest = <T>(path: string, init: RequestInit) => Promise<T>;

export function createResourceExtension(binding: ResourceBinding, request: ResourceRequest) {
  const base = '/v1/workbench/applications/' + encodeURIComponent(binding.applicationKey)
    + '/instances/' + encodeURIComponent(binding.instanceId) + '/resources';
  const owned = new Set<string>();
  let key = randomUUID();
  let active=true,activityEpoch=0;
  let ended = binding.signal?.aborted ?? false;
  let timer: ReturnType<typeof setInterval> | undefined;
  let renewing = false;
  const heartbeat = new AbortController();
  const send = (path: string, method: string, body: unknown, signal?: AbortSignal) =>
    request<{resource: WorkspaceResource}>(path, {
      method, signal, headers: {'Content-Type': 'application/json', 'Idempotency-Key': key},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  const resourceId = (params: unknown) => {
    if (!params || typeof params !== 'object' || !('resourceId' in params)
      || typeof params.resourceId !== 'string' || !owned.has(params.resourceId)) {
      throw new Error('resource_unavailable');
    }
    return params.resourceId;
  };
  const extension = {
    capabilities: ['resource.open', 'resource.lease', 'resource.close', 'resource.command'],
    handlers: {
      'resource.open': async (_params: unknown, signal: AbortSignal) => {
        if (ended||!active) throw new Error('instance_closed');
        const result = await send(base, 'POST', {
          install_id: binding.installId, revision: binding.revision,
          execution_scope: binding.executionScope, module_id: binding.moduleId,
        }, signal);
        if (ended || !active || signal.aborted) {
          await send(base + '/' + encodeURIComponent(result.resource.resource_id), 'DELETE', undefined);
          throw new Error('instance_closed');
        }
        owned.add(result.resource.resource_id);
        binding.onResourceChange?.(result.resource.resource_id);
        startHeartbeat();
        return result.resource;
      },
      'resource.lease': async (params: unknown, signal: AbortSignal) => {
        if (ended||!active) throw new Error('instance_closed');
        return (await send(base + '/' + encodeURIComponent(resourceId(params))
          + '/lease', 'POST', {}, signal)).resource;
      },
      'resource.command': async (params: unknown, signal: AbortSignal) => {
        if (ended || !active || !params || typeof params !== 'object') throw new Error('instance_closed');
        const id = resourceId(params);
        const command = params as Record<string, unknown>;
        if (typeof command.id !== 'string' || typeof command.action !== 'string') {
          throw new Error('invalid_resource_command');
        }
        return send(base + '/' + encodeURIComponent(id) + '/command', 'POST', {
          id: command.id, action: command.action, params: command.params ?? {},
        }, signal);
      },
      'resource.close': async (params: unknown, signal: AbortSignal) => {
        const id = resourceId(params);
        await send(base + '/' + encodeURIComponent(id), 'DELETE', undefined, signal);
        owned.delete(id);if(!owned.size)key=randomUUID();binding.onResourceChange?.([...owned][0]);
        if (!owned.size && timer) { clearInterval(timer); timer = undefined; }
        return {closed: true};
      },
    },
  };
  async function dispose() {
    ended = true;
    if (timer) { clearInterval(timer); timer = undefined; }
    heartbeat.abort();
    binding.signal?.removeEventListener('abort', abort);
    const ids = [...owned];
    owned.clear();binding.onResourceChange?.(undefined);
    return Promise.allSettled(ids.map(id =>
      send(base + '/' + encodeURIComponent(id), 'DELETE', undefined)));
  }
  function startHeartbeat() {
    if (ended || !active || timer || !owned.size) return;
    timer = setInterval(() => {
      if (renewing || ended) return;
      renewing = true;
      void Promise.all([...owned].map(id =>
        send(base + '/' + encodeURIComponent(id) + '/lease', 'POST', {}, heartbeat.signal)))
        .catch(() => dispose())
        .finally(() => { renewing = false; });
    }, 25000);
  }
  function abort() { queueMicrotask(() => void dispose()); }
  binding.signal?.addEventListener('abort', abort, {once: true});
  async function setActive(next:boolean){
    active=next;const epoch=++activityEpoch;
    if(next){startHeartbeat();return;}
    if(timer){clearInterval(timer);timer=undefined;}
    let retained:readonly string[]=[];
    try{retained=await binding.backgroundResources?.(heartbeat.signal)??[];}catch{/* Failure never grants background. */}
    if(ended||active||epoch!==activityEpoch)return;
    await Promise.allSettled([...owned].filter(id=>!retained.includes(id)).map(async id=>{
      await send(base+'/'+encodeURIComponent(id),'DELETE',undefined);
      owned.delete(id);if(!owned.size)key=randomUUID();binding.onResourceChange?.([...owned][0]);
    }));
  }
  return {extension, dispose, setActive};
}
