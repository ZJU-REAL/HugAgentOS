/** One grant-scoped channel owns one resource connection and its pending calls. */
import { ApiResponseError } from '../../utils/apiError';
import { attachResource, resourceUrl, type ResourceBinding } from './resource';

export function createResourceChannel(binding: ResourceBinding, report: (value: unknown) => void) {
  const controller = new AbortController();
  let socket: WebSocket | null = null;
  let port: MessagePort | null = null;
  let stopped = false;
  let attempts = 0;
  let reconnect: ReturnType<typeof setTimeout> | null = null;
  const pending = new Set<string>();
  const post = (value: unknown, transfer: Transferable[] = []) => {
    if (!stopped) port?.postMessage(value, transfer);
  };
  const failPending = () => {
    for (const id of pending) post({ type: 'result', id, ok: false, error: 'connection_lost_result_unknown' });
    pending.clear();
  };
  const connect = async () => {
    try {
      const next = await attachResource(binding, controller.signal);
      if (stopped) { next.close(); return; }
      socket = next;
      next.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          post(event.data, [event.data]);
        } else {
          const data = JSON.parse(String(event.data));
          if (data.type === 'attached') attempts = 0;
          if (data.type === 'result') pending.delete(data.id);
          post(data);
        }
      };
      next.onclose = (event) => {
        failPending();
        post({ type: 'connection', status: 'disconnected' });
        if (!stopped && attempts < 8 && ![4401, 4403, 4410].includes(event.code)) {
          reconnect = setTimeout(() => { void connect(); }, Math.min(10000, 500 * 2 ** attempts++));
        }
      };
    } catch (error) {
      if (!stopped && attempts < 8 && !(error instanceof ApiResponseError && [401, 403, 404, 409, 410].includes(error.status))) {
        post({ type: 'connection', status: 'error', error: error instanceof Error ? error.message : String(error) });
        reconnect = setTimeout(() => { void connect(); }, Math.min(10000, 500 * 2 ** attempts++));
      }
    }
  };
  return {
    async attach() {
      if (port) throw new Error('resource_already_attached');
      const channel = new MessageChannel();
      port = channel.port1;
      port.onmessage = (event) => {
        const message = event.data;
        if (!message || typeof message !== 'object') return;
        if (message.type === 'download' && typeof message.download_id === 'string') {
          report({ type: 'download', url: resourceUrl(binding, `/downloads/${encodeURIComponent(message.download_id)}`) });
          return;
        }
        if (message.type !== 'command' || typeof message.id !== 'string') return;
        if (!socket || socket.readyState !== WebSocket.OPEN) {
          post({ type: 'result', id: message.id, ok: false, error: 'resource_disconnected' });
          return;
        }
        if (pending.size >= 64) {
          post({ type: 'result', id: message.id, ok: false, error: 'too_many_pending_commands' });
          return;
        }
        pending.add(message.id);
        socket.send(JSON.stringify({ id: message.id, action: message.action, params: message.params || {}, epoch: message.epoch }));
      };
      port.start();
      void connect();
      return channel.port2;
    },
    dispose() {
      failPending();
      stopped = true;
      controller.abort();
      if (reconnect) clearTimeout(reconnect);
      socket?.close();
      port?.close();
      port = null;
    },
  };
}
