import { build } from 'esbuild';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const out = resolve('node_modules/.tmp/personal-chat-style');
await mkdir(out, { recursive: true });
await build({ entryPoints: ['scripts/personal-chat-style-fixture.tsx'], outfile: resolve(out, 'fixture.js'),
  external: ['/loader.gif', '/loader-done.png'], bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  loader: { '.woff2': 'file', '.woff': 'file', '.ttf': 'file', '.png': 'file', '.svg': 'file' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') } });
if (!process.argv.includes('--build-only')) createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"code":0,"data":{}}'); return; }
  if (url.pathname === '/mobile') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end('<body style="margin:0;background:#ddd"><iframe title="Mobile preview" src="/?typing" style="width:390px;height:844px;border:0"></iframe></body>');
    return;
  }
  if (url.pathname === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end('<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Personal Agent 对话样式预览</title><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script></html>');
    return;
  }
  const name = url.pathname.slice(1);
  if (!/^[a-zA-Z0-9._-]+$/.test(name)) { res.writeHead(404); res.end(); return; }
  try {
    const file = await readFile(resolve(out, name));
    res.writeHead(200, { 'Content-Type': name.endsWith('.css') ? 'text/css' : name.endsWith('.js') ? 'text/javascript' : 'application/octet-stream' });
    res.end(file);
  } catch { res.writeHead(404); res.end(); }
}).listen(4177, '0.0.0.0', () => console.log('Personal chat fixture: http://localhost:4177'));
