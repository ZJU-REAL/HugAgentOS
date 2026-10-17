import { defineConfig, loadEnv } from 'vite';
import { resolve } from 'node:path';
import { createDeveloperProxy } from './scripts/developer-proxy.mjs';

export default defineConfig(({ mode }) => {
 const env = { ...loadEnv(mode, process.cwd(), 'HUGAGENT_'), ...process.env };
 const host = env.HUGAGENT_DEV_HOST || '127.0.0.1';
 const settings = { host, port: Number(env.HUGAGENT_DEV_PORT || 4310), strictPort: true,
  allowedHosts: ['localhost'], cors: false, fs: { strict: true, allow: [process.cwd()] } };
 const originGuard = { name: 'developer-origin-guard', configureServer: guard, configurePreviewServer: guard };
 function guard(server) {
  server.middlewares.use((req, res, next) => {
   if (!/^\/(api(?:\/|$)|login(?:[?]|$))/.test(req.url || '')) return next();
   const origin = req.headers.origin;
   if (req.headers['sec-fetch-site'] === 'cross-site' || (origin && origin !== 'http://' + req.headers.host)) {
    res.statusCode = 403; res.end('Cross-origin developer API request refused'); return;
   }
   next();
  });
 }
 return {
  plugins: [originGuard],
  server: { ...settings, proxy: createDeveloperProxy(env) },
  preview: { host, port: Number(env.HUGAGENT_DEV_PORT || 4310), strictPort: true, proxy: createDeveloperProxy(env) },
  resolve: { dedupe: ['react', 'react-dom', '@hugagent/chat-sdk'] },
  build: { outDir: 'web-dist', sourcemap: false, rollupOptions: { input: {
   chat: resolve('index.html'), preview: resolve('preview.html'), debug: resolve('debug.html'), session: resolve('session.html'),
  } } },
 };
});
