import { createHash } from 'node:crypto';

/** A fixed upstream gateway; no client-supplied destination is accepted. */
export function createDeveloperProxy(env) {
 const raw = env.HUGAGENT_BACKEND_URL;
 if (!raw) return {};
 const target = new URL(raw);
 if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password ||
     target.search || target.hash || target.pathname !== '/') {
  throw new Error('HUGAGENT_BACKEND_URL must be an HTTP(S) origin, without a path, credentials, query or fragment.');
 }
 const upstreamCookie = env.HUGAGENT_SESSION_COOKIE || 'jx_session';
 if (!/^[A-Za-z0-9_-]+$/.test(upstreamCookie)) throw new Error('Invalid session cookie name');
 const localCookie = 'hugagent_dev_' + createHash('sha256').update(target.origin).digest('hex').slice(0, 16);
 const configure = proxy => {
  proxy.on('proxyReq', (outgoing, incoming) => {
   // Cookies are scoped to this upstream; never forward unrelated localhost cookies.
   const session = (incoming.headers.cookie || '').split(';').map(v => v.trim()).find(v => v.startsWith(localCookie + '='));
   outgoing.removeHeader('cookie');
   outgoing.removeHeader('authorization');
   if (session) outgoing.setHeader('cookie', upstreamCookie + '=' + session.slice(localCookie.length + 1));
   outgoing.setHeader('x-forwarded-host', incoming.headers.host || 'localhost');
   outgoing.setHeader('x-forwarded-proto', 'http');
  });
  proxy.on('proxyRes', response => {
   // Keep Secure, HttpOnly, SameSite and expiry. Only rewrite the development cookie name/domain.
   response.headers['set-cookie'] = (response.headers['set-cookie'] || [])
    .filter(cookie => cookie.startsWith(upstreamCookie + '='))
    .map(cookie => {
     const rewritten = cookie.replace(upstreamCookie + '=', localCookie + '=').replace(/;\s*Domain=[^;]*/gi, '');
     return /;\s*HttpOnly(?:;|$)/i.test(rewritten) ? rewritten : rewritten + '; HttpOnly';
    });
   delete response.headers['access-control-allow-origin'];
   delete response.headers['access-control-allow-credentials'];
  });
 };
 const options = { target: target.origin, changeOrigin: true, secure: true, xfwd: false, configure,
  proxyTimeout: 180000, timeout: 180000 };
 return {
  '^/api(?:/|$)': { ...options, rewrite: path => path.replace(/^\/api(?=\/|$)/, '') || '/' },
  '^/login(?:[?]|$)': options,
 };
}
