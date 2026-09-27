import assert from 'node:assert/strict';
import { clientDownloadTarget, latestClientDownloadUrl } from '../src/desktop/clientDownload';

globalThis.window = { location: { href: 'https://hugagent.quant-chi.com/' } } as unknown as Window & typeof globalThis;

assert.equal(clientDownloadTarget('Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Win32'), 'windows-x86_64');
assert.equal(clientDownloadTarget('Mozilla/5.0 (Macintosh; arm64 Mac OS X)', 'MacIntel'), 'darwin-aarch64');
assert.equal(clientDownloadTarget('Mozilla/5.0 (Macintosh; x86_64 Mac OS X)', 'MacIntel'), 'darwin-x86_64');
assert.equal(clientDownloadTarget('Mozilla/5.0 (X11; Linux x86_64)', 'Linux x86_64'), 'linux-x86_64');
assert.equal(clientDownloadTarget('Mozilla/5.0 (X11; Linux aarch64)', 'Linux aarch64'), 'linux-aarch64');
assert.equal(clientDownloadTarget('Mozilla/5.0 (iPhone; CPU iPhone OS 17 like Mac OS X)', 'iPhone'), null);
assert.equal(clientDownloadTarget('Mozilla/5.0 (Linux; Android 14)', 'Linux armv8l'), null);
assert.equal(clientDownloadTarget('Mozilla/5.0 (Macintosh; Intel Mac OS X)', 'MacIntel', 5), null);

const target = 'windows-x86_64';
const expected = 'https://hugagent.quant-chi.com/api/v1/desktop/download/client.exe';
const success = async (input: RequestInfo | URL) => {
  assert.equal(String(input), 'https://hugagent.quant-chi.com/api/v1/desktop/latest.json?target=windows&arch=x86_64');
  return Response.json({ platforms: { [target]: { url: expected } } });
};
assert.equal(await latestClientDownloadUrl(target, '/api', success as typeof fetch), expected);
assert.equal(await latestClientDownloadUrl(target, '/api', (async () => new Response(null, { status: 204 })) as typeof fetch), null);
await assert.rejects(
  latestClientDownloadUrl(target, '/api', (async () => Response.json({
    platforms: { [target]: { url: 'https://other.example/client.exe' } },
  })) as typeof fetch),
  /Invalid desktop download URL/,
);
console.log('client download checks passed');
