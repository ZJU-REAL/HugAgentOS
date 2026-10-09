import { openRunSubscription } from '../../runSubscriptionClient';
import type { ChatStreamState } from './state';

export class StreamDisconnectedError extends Error {
  constructor() { super('Run subscription disconnected'); }
}

/** The message reducer survives every connection; only byte framing restarts. */
export async function readRunTransport(
  initial: Response,
  state: ChatStreamState,
  signal: AbortSignal | undefined,
  consume: (bytes: Uint8Array) => void,
  reconnect = true,
) {
  let response = initial;
  let retries = 0;
  while (!state.streamEnded) {
    if (!response.body) throw new StreamDisconnectedError();
    const reader = response.body.getReader();
    const cancel = () => { void reader.cancel().catch(() => {}); };
    signal?.addEventListener('abort', cancel, { once: true });
    try {
      while (!signal?.aborted && !state.streamEnded) {
        let chunk: ReadableStreamReadResult<Uint8Array>;
        try { chunk = await reader.read(); } catch { break; }
        if (chunk.done) break;
        consume(chunk.value);
      }
    } finally {
      signal?.removeEventListener('abort', cancel);
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    if (signal?.aborted) {
      state.aborted = true;
      return;
    }
    if (state.streamEnded || !state.runId || !reconnect) return;
    // A truncated SSE frame belongs to the dead connection, not the next one.
    state.sseBuffer = '';
    state.decoder = new TextDecoder('utf-8');
    if (++retries > 6) throw new StreamDisconnectedError();
    await new Promise<void>((resolve) => {
      const timer = setTimeout(done, Math.min(200 * 2 ** (retries - 1), 5000));
      function done() { clearTimeout(timer); signal?.removeEventListener('abort', done); resolve(); }
      signal?.addEventListener('abort', done, { once: true });
    });
    if (signal?.aborted) { state.aborted = true; return; }
    try {
      response = await openRunSubscription(state.runId, signal, state.chatId);
    } catch {
      if (signal?.aborted) { state.aborted = true; return; }
      continue;
    }
    if (!response.ok) throw new StreamDisconnectedError();
  }
}
