import { sseData } from '@hugagent/chat-sdk';

/** A single SSE frame larger than this means the stream is broken, not slow. */
const MAX_SSE_BUFFER = 4 * 1024 * 1024;

/**
 * Shared SSE parser for JSON event streams: frames are split on blank lines (LF or CRLF), each frame's
 * `data:` lines are joined by chat-sdk's `sseData` (the same framing the chat stream uses), the stream ends
 * on `[DONE]`, and heartbeat comments / non-JSON frames are skipped. A last frame without the trailing blank
 * line is still delivered. Leaving the loop early cancels the underlying response body.
 */
export async function* parseSSE<T = unknown>(resp: Response): AsyncGenerator<T> {
  const reader = resp.body?.getReader();
  if (!reader) return;
  const dec = new TextDecoder();
  let buf = '';
  let finished = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buf += done ? dec.decode() : dec.decode(value, { stream: true });
      if (buf.length > MAX_SSE_BUFFER) throw new Error('SSE frame exceeds 4 MiB');
      const blocks = buf.split(/\r?\n\r?\n/);
      buf = blocks.pop() || '';
      if (done && buf.trim()) { blocks.push(buf); buf = ''; }
      for (const block of blocks) {
        const data = sseData(block)?.trim();
        if (!data) continue;
        if (data === '[DONE]') return;
        let parsed: T;
        try {
          parsed = JSON.parse(data) as T;
        } catch {
          continue;
        }
        yield parsed;
      }
      if (done) { finished = true; return; }
    }
  } finally {
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
