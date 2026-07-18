/**
 * Shared SSE parser: yields each `data: {json}\n\n` frame one by one, ends on `[DONE]`,
 * and ignores heartbeat comment lines and non-JSON frames. Shared by SSE consumers such as the
 * autonomous loop (chat stream + lab panel), to avoid re-implementing the same reader/split/parse logic everywhere.
 */
export async function* parseSSE<T = unknown>(resp: Response): AsyncGenerator<T> {
  const reader = resp.body?.getReader();
  if (!reader) return;
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const chunks = buf.split('\n\n');
    buf = chunks.pop() || '';
    for (const chunk of chunks) {
      const line = chunk.split('\n').find((l) => l.startsWith('data:'));
      if (!line) continue;
      const data = line.slice(5).trim();
      if (data === '[DONE]') return;
      try {
        yield JSON.parse(data) as T;
      } catch {
        /* skip non-json (heartbeat comments) */
      }
    }
  }
}
