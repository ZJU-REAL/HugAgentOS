import assert from 'node:assert/strict';
import { getKBChunks } from '../src/api';

const chunk = { chunk_id: 'c1', document_id: 'doc', chunk_index: 0, content: 'Indexed body', tags: [], questions: [] };
globalThis.fetch = async () => Response.json({ code: 10000, data: { items: [chunk] } });
assert.deepEqual(await getKBChunks('kb', 'doc'), [chunk]);
globalThis.fetch = async () => Response.json({ code: 10000, data: { items: [] } });
assert.deepEqual(await getKBChunks('kb', 'doc'), []);
for (const status of [404, 503]) {
  globalThis.fetch = async () => Response.json({ message: 'Chunk request failed' }, { status });
  await assert.rejects(() => getKBChunks('kb', 'doc'));
}
globalThis.fetch = async () => { throw new TypeError('Network unavailable'); };
await assert.rejects(() => getKBChunks('kb', 'doc'), /Network unavailable/);
console.log('KB chunks: content, successful empty, HTTP and network failures passed');