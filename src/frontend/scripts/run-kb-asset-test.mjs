/**
 * Bundle + run `test-kb-asset-thumbs.ts`.
 *
 * A runner rather than a one-liner in package.json: this test renders real components,
 * so it needs the automatic JSX runtime, `import.meta.env` stubbed (no Vite here), a
 * `localStorage` shim for the stores that read it at module scope, and node_modules left
 * external (bundling antd/react-dom's CJS into ESM breaks on dynamic require). That does
 * not survive JSON-in-shell quoting.
 */
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { build } from 'esbuild';

const here = dirname(fileURLToPath(import.meta.url));
// Output next to node_modules so `--packages=external` imports still resolve.
const outfile = resolve(here, '../node_modules/.tmp/test-kb-asset-thumbs.mjs');
mkdirSync(dirname(outfile), { recursive: true });

await build({
  entryPoints: [resolve(here, 'test-kb-asset-thumbs.ts')],
  bundle: true,
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  packages: 'external',
  outfile,
  define: {
    'import.meta.env': JSON.stringify({ VITE_DEFAULT_LANGUAGE: 'zh-CN', VITE_API_BASE_URL: '' }),
  },
  banner: {
    js: [
      'const _m = new Map();',
      'globalThis.localStorage = {',
      '  getItem: (k) => (_m.has(k) ? _m.get(k) : null),',
      '  setItem: (k, v) => _m.set(k, String(v)),',
      '  removeItem: (k) => _m.delete(k),',
      '  clear: () => _m.clear(),',
      '};',
    ].join('\n'),
  },
});

await import(pathToFileURL(outfile).href);
