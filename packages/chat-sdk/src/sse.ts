import type { ChatEvent } from './types.js';

/** Shared framing seam; the host retains its own domain event reducer. */
export function sseData(block: string): string | null {
  const lines = block.split(/\r\n|\r|\n/).filter(line => line.startsWith('data:'));
  if (!lines.length) return null;
  return lines.map(line => line.slice(5).replace(/^ /, '')).join('\n');
}

/** A disconnect is not completion. Consumers can explicitly resume the saved run. */
export async function* readSSE(response: Response, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  if (!response.body) throw new Error('Empty SSE body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let terminal = false;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const { value, done } = await reader.read();
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (buffer.length > 4 * 1024 * 1024) throw new Error('SSE frame exceeds 4 MiB');
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || '';
      if (done && buffer.trim()) { blocks.push(buffer); buffer = ''; }
      for (const block of blocks) {
        const data = sseData(block);
        if (data === null) continue;
        if (data.trim() === '[DONE]') { terminal = true; return; }
        const parsed: unknown = JSON.parse(data);
        if (!parsed || typeof parsed !== 'object' || typeof (parsed as ChatEvent).type !== 'string') {
          throw new Error('Invalid SSE event');
        }
        const event = parsed as ChatEvent;
        yield event;
        if (event.type === 'run_terminal' || event.type === 'end' ||
          (event.type === 'run_snapshot' && !!event.state && typeof event.state === 'object' && 'terminal' in event.state && event.state.terminal === true)) { terminal = true; return; }
      }
      if (done) break;
    }
    if (!terminal) throw new Error('SSE disconnected before completion; resume the run');
  } finally {
    signal?.removeEventListener('abort', abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
