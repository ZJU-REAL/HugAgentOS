import { authFetch, getApiUrl, LOCAL_TARGET_HEADER } from '../../../api';
import { parseSSE } from '../../../utils/sse';

type LinkSignal = { type: 'ready' } | { type: 'changed' };

/**
 * Hear this computer's link status change, from the local backend the desktop app started. `onChange` runs on
 * every (re)connection and on every change; the caller reads the status itself. Returns the stop function.
 */
export function followLocalLink(onChange: () => void): () => void {
  const controller = new AbortController();
  void (async () => {
    let failures = 0;
    while (!controller.signal.aborted) {
      try {
        const response = await authFetch(getApiUrl() + '/v1/device-link/events', {
          signal: controller.signal, headers: { Accept: 'text/event-stream', [LOCAL_TARGET_HEADER]: 'local' },
        });
        if (!response.ok) throw new Error('device-link events: ' + response.status);
        for await (const signal of parseSSE<LinkSignal>(response)) {
          if (signal.type === 'ready') failures = 0;
          onChange();
        }
      } catch { /* reconnect below */ }
      if (controller.signal.aborted) return;
      // The local backend restarts with the app: come back sooner after a brief drop than after repeated failures.
      const delay = Math.min(1000 * 2 ** failures++, 30_000);
      await new Promise<void>(resolve => {
        const timer = setTimeout(done, delay);
        function done() { clearTimeout(timer); controller.signal.removeEventListener('abort', done); resolve(); }
        controller.signal.addEventListener('abort', done, { once: true });
      });
    }
  })();
  return () => controller.abort();
}
